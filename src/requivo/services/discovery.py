"""DiscoveryService — the provider-backed application orchestration, shared by every interface.

The Core is provider-free, and the CLI and Web must not each re-orchestrate "call the provider, then
apply through SessionService". This service *is* that orchestration, in one place: it holds a
`ReasoningProvider` plus the session/artifact services and exposes interface-neutral operations —
start a discovery, fold in answers, generate an artifact. The terminal CLI and the local Web are thin
callers over it, so there is exactly one place that turns a provider reply into a validated, versioned
model change.

It talks to the provider through the protocol only (`analyze` / `generate` / `provenance`), never to a
vendor's functions directly. That is what keeps the seam real rather than decorative: this service
takes a `ReasoningProvider` and nothing else, so *pointing it at* a second implementation is a
constructor argument, and the provenance stamped on each revision comes from the provider itself
instead of a hard-coded `"anthropic"` string.

That is the cost of the swap, and it used to be written here as though it were the whole cost.
*Writing* the second implementation is not a constructor argument: roughly 400 lines that have
nothing to do with any vendor -- the per-operation message builders, the generator tables,
`prompt_version()`, the JSON extraction and the corrective-nudge retry loop -- are packaged under
`providers/anthropic` today, so a second provider re-implements or copies them. Extracting them is
decided work, deferred with a written trigger: `decision: deferring-the-neutral-provider-layer`.

It never touches the filesystem or `model.json` directly — every write goes through `SessionService`
(validate → diff → propagate → revision → stale-flag) and `ArtifactService` (save with source
revision), so revision handling and staleness are identical to every other surface.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, Literal, NamedTuple, TypeVar, cast, overload

from requivo.core.context import card_summaries, resolve_cards
from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    ContextDecision,
    ContextJudgment,
    EngineOutput,
    Epic,
    EstimateDraft,
    GoToMarketPlan,
    PerimeterDecision,
    PerimeterJudgment,
    ReleaseNotes,
    Stories,
)
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import (
    AmbiguousPerimeterError,
    ArtifactTypeNotOwnedError,
    ArtifactWriteFailedError,
    InvalidSlugError,
    RequivoError,
    RevisionConflictError,
    SessionLockedError,
    SessionUnreadableError,
)
from requivo.core.perimeters import (
    DEFAULT_PERIMETER,
    get_perimeter,
    known_perimeter_ids,
    perimeter_summaries,
    resolve_perimeter,
)
from requivo.core.persistence import (
    ArtifactStatus,
    Store,
    _refuse_new_reserved_slug,
    _slug_shape,
    artifact_path,
    is_contained,
)
from requivo.core.selectors import display_text
from requivo.core.validation import require_input_within_bounds
from requivo.paths import workspace_root
from requivo.providers.base import ContextJudge, PerimeterJudge
from requivo.render.markdown import (
    brief_markdown,
    criteria_markdown,
    epic_markdown,
    estimate_markdown,
    gtm_plan_markdown,
    prd_markdown,
    release_markdown,
    stories_markdown,
)
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService, SessionSnapshot, UpdateResult
from requivo.usage import SpendPolicy, current_ledger

logger = logging.getLogger(__name__)

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

def _estimate_document(estimate: tuple[EstimateDraft, list[str], str]) -> str:
    """The estimate's writer, over the triple the provider hands out — `(draft, soft, confidence)`,
    the last two computed in core from the same model — so the registry stays one-argument."""
    draft, soft, confidence = estimate
    return estimate_markdown(draft, soft, confidence)


# artifact type → the writer that turns its contract into the Markdown that gets saved. This is the
# vocabulary of "things a generation produces a document for", in the order a user meets them.
# `stories` and `estimate` were absent until #519 (`decision: the-estimate-graduates`): both are
# saved now, and the estimate's generation is the one two-call branch of `generate()` below, because
# it is reasoned against the stories it saves beside itself.
#
# The annotation is load-bearing: dropping it makes pyright infer a union of narrow callables that
# no argument satisfies. `decision: typed-generation-seam`
_WRITERS: dict[str, Callable[[Any], str]] = {
    "prd": prd_markdown,
    "stories": stories_markdown,
    "criteria": criteria_markdown,
    "estimate": _estimate_document,
    "epic": epic_markdown,
    "release": release_markdown,
}

# Everything `generate()` can produce, in the order a user meets them. This is the source every
# interface asks — the CLI's verbs, the Web's buttons — so a new generator becomes available
# everywhere by being registered here, rather than by each surface keeping its own list and drifting.
# `gtm_plan` (#609) is not in `_WRITERS`: it takes the same reasoning-absorbing path `brief` does
# (see `_ASSESSMENT_ARTIFACTS` below), never the generic model→writer dispatch `_WRITERS` serves.
GENERATABLE: tuple[str, ...] = ("brief", "gtm_plan", *_WRITERS)

_A = TypeVar("_A")


@dataclass
class SavedEstimate:
    """What `generate(slug, "estimate")` hands back as its artifact: the estimate as `reason_from`
    returns it — the provider's draft plus the soft slots and confidence computed in core — and the
    stories it was reasoned against, with their own saved status.

    The stories ride along because they are half of the estimate's basis (#135: one snapshot, two
    calls) and were saved from the same snapshot against the same revision (#519, invariant 6). A
    caller rendering the estimate has both halves and both provenance rows without a second read."""

    draft: EstimateDraft
    soft: list[str]
    confidence: str
    stories: Stories
    stories_status: ArtifactStatus


@dataclass
class Generated(Generic[_A]):
    """What one generation produced. `status` is the saved artifact's provenance; `artifact` is the
    typed contract behind it, so a caller can render its own view (the CLI's terminal layout, the epic
    exports) without paying for a second provider call; `model` is the model it was rendered from —
    which for the assessment is the *post-absorption* model, not the one read at the start.

    Generic, and the type parameter is resolved by `generate()`'s overloads rather than here — a bare
    `object` costs every caller a cast and a plain `Union` moves it rather than removing it.
    `decision: typed-generation-seam`"""

    status: ArtifactStatus
    artifact: _A
    model: EngineOutput


def _require_revision_zero(slug: str, revision: int) -> None:
    """A first discovery may only land on a session that has no model yet.

    Discovery *replaces* the model — it reasons from the request alone, without the current model —
    so running it on a session already at revision N does not refine that understanding, it discards
    it and writes a naive first-turn one over the top. The optimistic lock does not catch this: the
    call reads revision N and writes against revision N, so the precondition is satisfied while the
    content is a regression. The revision itself has to be the rule, and it cannot live in an
    interface (the Web only shows the button at revision 0) — a business rule enforced by a hidden
    button is not enforced."""
    if revision > 0:
        raise RevisionConflictError(
            f"session '{slug}' already carries a model (revision {revision}) — a fresh discovery "
            f'would replace it. Refine it instead (`requivo answer {slug} "…"`), or run this '
            "discovery under another slug.",
            details={"slug": slug, "expected": 0, "actual": revision})



def _require_no_conflict_yet(slug: str, expected_revision: int | None, snap: SessionSnapshot) -> None:
    """A conflict that is already certain is refused before the paid call, not after it (#205).

    `answer()` takes the revision the caller's form was rendered at and passes it to `update_model`
    as an optimistic-locking precondition — which fires *after* the provider has reasoned a full
    turn. But the snapshot read at the top of `answer()` already knows the session's current
    revision, so when the two disagree the apply is guaranteed to fail and the turn is guaranteed to
    be discarded: a second tab, the CLI or a back-button submit had moved the session on, and the
    user was billed minutes of analysis for a result nothing would ever read.

    This is invariant 13's own principle — the check is cheap and the call is not — applied to the
    second gate that needed it rather than only to the revision-zero one. It does not replace the
    precondition on the apply: the session can still move *during* the call, which is what
    `expected_revision` on `update_model` is for. It removes the case where it had already moved
    *before* it.

    A caller that passes `None` (the CLI, which is single-user and holds no rendered form) is
    unaffected: it has stated no expectation, so there is nothing to be stale.

    Pinned by `test_a_stale_answers_form_is_refused_before_the_provider_is_paid`, whose assertion is
    the provider call count — the 409 already happened before this gate existed, so a test asserting
    only the refusal was green on the defect. `test_a_matching_answers_form_still_reaches_the_provider`
    is the must-fire control.
    """
    if expected_revision is not None and expected_revision != snap.revision:
        raise RevisionConflictError(
            f"session '{slug}' is at revision {snap.revision}, not the expected "
            f"{expected_revision} — reload the page and re-submit your answers",
            details={"slug": slug, "expected": expected_revision, "actual": snap.revision})


def _require_owned_artifact_type(perimeter: str, artifact_type: str) -> None:
    """A generation call must name an artifact type its own perimeter actually produces (#608).

    Software's seven types are refused on a go-to-market session and `gtm_plan` (#609) is refused
    on a software one -- every `generate()` or `reason()` call refuses here, with the perimeter
    named, rather than validating the reply against the wrong schema and surfacing a confusing
    Pydantic error two layers down.

    Raises `ArtifactTypeNotOwnedError`, a structured `RequivoError` (409, `http.py`) -- not the bare
    `ValueError` this used to be. A `ValueError` reaches no `RequivoError` handler: on the CLI it
    tracebacks past `app()`'s `except RequivoError` arm (`requivo brief` on a go-to-market session);
    on the Web it reaches the generic `Exception` handler and renders as an ordinary click's 500
    (found by Codex reviewing #609: `GENERATABLE` with no perimeter filter offered every session
    every type, so a stray click was the reachable path, not a hypothetical one)."""
    owned = get_perimeter(perimeter).artifact_types
    if artifact_type not in owned:
        raise ArtifactTypeNotOwnedError(
            f"{artifact_type!r} is not produced by the {perimeter!r} perimeter this session runs "
            f"under -- it can produce: {sorted(owned) or '(none yet)'}",
            details={"artifact_type": artifact_type, "perimeter": perimeter, "owned": sorted(owned)})


def _require_a_model(slug: str, snap: SessionSnapshot) -> EngineOutput:
    """Generation may only run on a session that *has* a model — the mirror of the rule above.

    Without it an unchecked `snap.model` reaches the prompt assembly and the user gets an
    `AttributeError` traceback instead of a structured refusal. Returns the model rather than `None`
    so the narrowing is in the type too, which is what stops a new call site forgetting the guard.
    Pinned by `test_generating_from_a_session_with_no_model_is_refused_before_the_provider`."""
    if snap.model is None:
        raise RevisionConflictError(
            f"session '{slug}' has no model yet (revision 0) — there is nothing to generate from. "
            "Run `requivo discover` on it.",
            details={"slug": slug, "expected": 1, "actual": snap.revision})
    return snap.model


def absorb_reasoning(out: EngineOutput, brief) -> None:
    """Persist the assessment's reasoning (decisions, challenges, opportunities, exclusions,
    thresholds) into the model so every generator inherits it, not just the facts. Called wherever
    the assessment is produced, before the model is applied — the single definition, shared by the
    CLI and the Web."""
    out.decisions = brief.decisions
    out.challenges = brief.challenges
    out.opportunities = brief.opportunities
    # #600: the compression's cuts land in the model the same way its three siblings do, so
    # `model.json` carries them (#599's typed item), not only the rendered brief; #604 does the
    # same for a decision threshold -- both guarded by
    # test_a_generated_briefs_reasoning_items_are_absorbed_into_the_persisted_model.
    out.exclusions = brief.exclusions
    out.thresholds = brief.thresholds


def absorb_gtm_reasoning(out: EngineOutput, brief: GoToMarketPlan) -> None:
    """Persist the go-to-market plan's reasoning into the model (#609) -- the same role
    `absorb_reasoning` plays for the software brief, narrowed to the two typed items
    `GoToMarketPlan` carries: `exclusions` (#599) and `thresholds` (#604). It has no
    decisions/challenges/opportunities of its own (see the contract's own docstring)."""
    out.exclusions = brief.exclusions
    out.thresholds = brief.thresholds


@dataclass(frozen=True)
class _AssessmentArtifact:
    """One artifact type whose generation absorbs reasoning back into the model before it is saved
    -- what `generate()` below used to special-case for `"brief"` alone until #609 gave the
    perimeter mechanism its second instance of the same shape. `cli_verb`/`label` are what the
    revision-conflict messages in `generate()` need: the retry command to name and the reader-facing
    noun for the artifact."""
    writer: Callable[[EngineOutput, Any], str]
    absorb: Callable[[EngineOutput, Any], None]
    cli_verb: str
    label: str


# Every artifact type that takes the reasoning-absorbing generation path, keyed the same way every
# other registry in this module is (see CLAUDE.md's "Adding a generator" checklist). `brief` is
# software's; `gtm_plan` is go-to-market's (#609) -- a second instance is what tells this
# generalisation apart from an accident of "brief" being the first and only one.
_ASSESSMENT_ARTIFACTS: dict[str, _AssessmentArtifact] = {
    "brief": _AssessmentArtifact(brief_markdown, absorb_reasoning, "brief", "decision brief"),
    "gtm_plan": _AssessmentArtifact(gtm_plan_markdown, absorb_gtm_reasoning, "gtm_plan",
                                     "go-to-market plan"),
}


def _discovery_guard_path(slug: str, store: Store) -> Path:
    """The in-flight first-discovery guard for `slug`: `<workspace>/.requivo/locks/<slug>.discovering`.

    A sibling of `core.persistence.Store.lock_path`, deliberately a *different* file: that lock covers
    a compound write and is released *before* a provider call starts, which is exactly the window two
    concurrent first-discovery requests can both walk into (#209) --
    `test_a_concurrent_first_discovery_is_refused_before_any_provider_call`.

    `store` is the caller's own repository, resolved by `DiscoveryService._store_for_repo`, never the
    ambient default (#272) -- `test_the_discovery_guard_addresses_an_explicitly_rooted_repositorys_own_workspace`.

    Validated exactly as `lock_path` validates its own -- the shape unconditionally, the reserved
    Windows device name only when nothing already occupies the slug -- because the slug reaches here
    from the service layer, which invariant 14 says an external consumer may call directly. This
    function was added one commit before that conditional form existed and was missed when the
    sibling functions were swept onto it (#390), leaving a session already on disk under a reserved
    name reachable by every read verb except this guard, which alone kept refusing it --
    `test_a_reserved_slug_the_sweep_one_commit_later_missed_reaches_the_discovery_guard`."""
    root = store.lock_root()
    slug = _slug_shape(slug)
    # Checked against the *session* root, never against `root` above -- `lock_path` carries the long
    # form of why, and it is the same argument here: a `<slug>.discovering` file is not a session, and
    # what decides whether #221's creation refusal still applies is whether a session already claims
    # this name. On a first discovery of a genuinely new reserved slug nothing does, so the refusal
    # still fires -- but that case is unreachable anyway, since `run_discovery` needs a session that
    # `create_session` (which does refuse) already made.
    _refuse_new_reserved_slug(slug, store.session_root() / slug)
    p = root / (slug + ".discovering")
    if not is_contained(p, root):
        raise InvalidSlugError(f"slug {slug!r} does not resolve to a lock file inside {root}",
                               details={"slug": slug})
    return p


@contextmanager
def _discovery_guard(slug: str, store: Store) -> Iterator[None]:
    """Refuse a second, concurrent first-discovery on `slug` before it can pay for anything.

    Held for exactly the span a paid provider call plus its one write can take. Non-blocking and not
    re-entrant, both deliberately; without it two concurrent callers both pass
    `_require_revision_zero` and both pay. Pinned by
    `test_a_concurrent_first_discovery_is_refused_before_any_provider_call`, whose docstring carries
    the three shape decisions, with `test_run_discovery_still_succeeds_once_the_guard_is_free` as the
    must-fire control.

    `store` is the one `DiscoveryService._store_for_repo()` resolved, so two services over two
    explicitly-rooted repositories serialise independently rather than contending on one ambient
    guard file neither may be addressing. Pinned by
    `test_the_discovery_guard_addresses_an_explicitly_rooted_repositorys_own_workspace`, which
    asserts the guard file lands under the repository's own root *and* that the ambient one is never
    touched -- the negative half is what makes it fire when this resolution goes back to ambient.
    """
    p = _discovery_guard_path(slug, store)
    store.ensure_store_dir(p.parent)
    try:
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise SessionUnreadableError(
            f"could not open the discovery guard for session '{slug}': {e}",
            details={"slug": slug}) from e
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SessionLockedError(
                    f"a discovery is already running for session '{slug}'; wait for it to finish "
                    "and reload",
                    details={"slug": slug}) from None
        elif msvcrt is not None:  # pragma: no cover - Windows
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SessionLockedError(
                    f"a discovery is already running for session '{slug}'; wait for it to finish "
                    "and reload",
                    details={"slug": slug}) from None
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _usage_since(before: int) -> dict:
    """The token/rate provenance for however many provider calls a `DiscoveryService` operation made
    since `before` (a `len(ledger.calls)` saved right before the call), shaped as the extra
    `RevisionRecord` fields `_provenance` merges in.

    `{}` — never zero-filled — when there is no active ledger or it recorded no calls in the span:
    both are "nothing to report", never "spent nothing", which is invariant 6 applied to this ledger.
    A span sums its calls and stamps a rate only when they agree on one. Pinned by
    `test_a_provider_backed_apply_stamps_token_and_rate_provenance_onto_its_revision`, with
    `test_a_provider_call_made_with_no_active_ledger_still_leaves_usage_absent` for the absent
    case."""
    ledger = current_ledger()
    if ledger is None:
        return {}
    calls = ledger.calls[before:]
    if not calls:
        return {}
    usage: dict = {
        "usage_input_tokens": sum(c.input_tokens for c in calls),
        "usage_output_tokens": sum(c.output_tokens for c in calls),
        "usage_cache_read_tokens": sum(c.cache_read_tokens for c in calls),
        "usage_cache_write_tokens": sum(c.cache_write_tokens for c in calls),
    }
    rates = {c.rate_per_mtok for c in calls}
    dates = {c.priced_as_of for c in calls}
    if len(rates) == 1 and len(dates) == 1:
        (rate,), (as_of,) = rates, dates
        if rate is not None and as_of is not None:
            usage["usage_rate_per_mtok"] = rate
            usage["usage_priced_as_of"] = as_of
    return usage


class Grounding(NamedTuple):
    """The answer to *is this session grounded in anything that knows its domain?*, and the third
    state that makes it honest: `judgment is None` means nobody looked, and `why_not` says why.

    A caller that renders `judgment is None` the same as `ContextDecision.none` has turned "we did
    not ask" into "nothing is needed", which is the exact shape #492 refused to ship."""

    judgment: ContextJudgment | None
    why_not: str


class Routing(NamedTuple):
    """The answer to *which installed perimeter does this request's shape belong to?* (#601) --
    `Grounding`'s own shape, one question over: `judgment is None` means nobody looked, and
    `why_not` says why. A caller that reads that the same as a `none` verdict has made the identical
    mistake `Grounding`'s docstring warns against, one layer up."""

    judgment: PerimeterJudgment | None
    why_not: str


class Reclaim(NamedTuple):
    """What `_reclaim_under` actually did, as three independent facts -- a single boolean cannot
    carry all three, and reusing one flag for two of them produced a defect for each of the two
    questions it was asked in a row (#601, Codex review rounds two and three): a bare `True`
    mistaken for ownership authorised deleting a session this call did not create, and `created`
    read as "did the identity move" mis-reported an idempotent re-entry onto the *correct* identity
    as though nothing had happened. Every caller now reads the fact it needs off this, never one
    inferred from another:

    - **`meta`** -- the session to discover against, always the truth about where the claim is now,
      whichever of the three outcomes below produced it.
    - **`landed`** -- did the identity actually move to what was asked for: `_delete_if_safe`
      authorised the delete and the recreate that followed completed, freshly or by matching an
      **existing** session under the exact same identity (`create_session_report` never returns a
      session under any other identity than the one requested -- it raises instead). `False` only
      when the delete itself was refused, in which case `meta` is the untouched prior claim. This is
      the fact a renderer, or a caller reporting the resulting cards/perimeter, must read -- never
      `created`.
    - **`created`** -- did *this call* create the session `meta` now names, as opposed to landing on
      one that already existed under that identity. The one fact a *later* delete's authorisation
      (the third of #593's four preconditions) may read; never a proxy for `landed`.
    """

    meta: Any
    landed: bool
    created: bool


class ClaimAndGround(NamedTuple):
    """What `DiscoveryService.claim_and_ground` produced (#601): the session to discover against,
    what context grounding found, the card selection to reason with, and what the router found.

    **Read it by attribute** (`.meta`, `.grounding`, `.cards`, `.routing`), not by position.
    Positional unpacking still works today — a `NamedTuple` is a tuple — but it is not the
    supported form: this shape grew from three fields to four once already (#601 joined `.routing`
    to #593's three), it is part of the declared Python import seam (`docs/compatibility.md`), and
    a fact added later is a new field here rather than another arity break for every caller that
    unpacks. `docs/compatibility.md` declares the 3→4 break this NamedTuple lands on top of."""

    meta: Any
    grounding: Grounding
    cards: list[str] | None
    routing: Routing


class DiscoveryService:
    """Provider-backed orchestration over the session/artifact services.

    The provider is built lazily, so constructing the service never needs an API key — only the
    operations that actually reason do (consulting an existing session needs none). Inject a
    `ReasoningProvider` to swap the reasoning backend; `client=` is the shorthand for "the default
    provider over this SDK client", which is what the tests and the CLI use.
    """

    def __init__(self, provider=None, *, client=None, sessions: SessionService | None = None,
                 artifacts: ArtifactService | None = None, repo=None,
                 spend_policy: SpendPolicy | None = None):
        self._provider = provider
        self._client = client
        self._spend_policy = spend_policy
        self.sessions = sessions or SessionService(repo)
        # The artifact service defaults to *this service's* storage, not to the process default. On a
        # file backing the two were indistinguishable — both resolve to the same workspace — which is
        # what hid the bug: constructing `DiscoveryService(sessions=SessionService(postgres_repo))`
        # sent the sessions to Postgres and the artifacts to the local filesystem, and every call
        # succeeded. One repository per service, chosen once, is the only shape that cannot split.
        self.artifacts = artifacts or ArtifactService(self.sessions.repo)

    def _store_for_repo(self) -> Store:
        """The `core.persistence.Store` backing `self.sessions.repo`, for the two ambient reads that
        live outside any repository method: the first-discovery guard and the reserved-slug probe
        inside it.

        Duck-typed against `self.sessions.repo.store()` rather than added to `SessionRepository`'s
        protocol -- a Postgres backing has no filesystem root to hand back -- and falls back to the
        ambient default only when there is none to reach for, which was every caller's behaviour
        unconditionally before #272:
        `test_the_discovery_guard_addresses_an_explicitly_rooted_repositorys_own_workspace`."""
        get_store = getattr(self.sessions.repo, "store", None)
        return cast(Store, get_store()) if callable(get_store) else Store(workspace_root())

    def _need_provider(self):
        """The reasoning provider, built on first use so a key is only required for provider actions.
        The default is imported here rather than at module scope: the service depends on the protocol,
        and only the fallback construction knows which implementation is the default one."""
        if self._provider is None:
            from requivo.providers.anthropic import AnthropicProvider
            self._provider = AnthropicProvider(self._client)
        return self._provider

    def _check_spend(self) -> None:
        """Consult the injected `SpendPolicy`, if any, immediately before a provider call (#427).

        Called at every `provider.analyze`/`provider.generate` call site in this class, never once
        at a method's entry -- an operation that makes more than one call (`start(finalize=True)`,
        `generate("brief")`) must have the second refused too, the moment the first alone reaches
        the ceiling. No policy injected is a no-op: `self._spend_policy is None` is the default, and
        a `DiscoveryService` built that way behaves exactly as it did before this existed."""
        if self._spend_policy is not None:
            self._spend_policy.check(current_ledger())

    @contextmanager
    def _provider_call(self, operation: str) -> Iterator[None]:
        """Log a provider call's start and finish (or failure), with the operation and its duration
        -- the orchestration-level seam `docs/cloud-boundary.md` §6 promises for
        `requivo.services.discovery`: DEBUG on start, INFO on a clean finish, WARNING (with the
        exception re-raised unchanged) on failure. Wraps every provider call site in this class.

        Service-level wall-clock duration, deliberately not the attempts/tokens `CallRecord` carries
        per HTTP call -- those belong to `completion.py`'s own logger, the one place attempts are
        known. Silent unless a caller attaches a handler (invariant 7). Pinned by
        `test_a_successful_provider_call_logs_started_and_finished` and
        `test_a_failed_provider_call_logs_a_warning_and_still_raises`, with
        `test_default_run_leaves_the_conflict_refused_warning_off_every_stream` for the silence."""
        started = time.perf_counter()
        logger.debug("provider call started: operation=%s", operation)
        try:
            yield
        except Exception:
            logger.warning("provider call failed: operation=%s duration_ms=%d",
                          operation, int((time.perf_counter() - started) * 1000))
            raise
        else:
            logger.info("provider call finished: operation=%s duration_ms=%d",
                       operation, int((time.perf_counter() - started) * 1000))

    def _provenance(self, op: str, *, cards: list[str] | None, surface: str,
                    usage: dict | None = None, perimeter: str = DEFAULT_PERIMETER) -> dict:
        """The provenance for a revision: what the provider says about itself, which of our surfaces
        asked for it (the one thing the provider cannot know), and — when the caller has it — what
        the call(s) behind this apply actually spent (`_usage_since`, #292)."""
        prov = {**self._need_provider().provenance(op, only=cards, perimeter=perimeter),
               "surface": surface}
        if usage:
            prov.update(usage)
        return prov

    # ── discovery ────────────────────────────────────────────────────────────────
    def create_only(self, request: str, *, cards: list[str] | None = None,
                    slug: str | None = None, perimeter: str = DEFAULT_PERIMETER) -> str:
        """Persist a request as a session with no model yet — no LLM call. The 'Create session only'
        path: capture the request now, run discovery later. `perimeter` (#608) is frozen here, the
        one moment a session's decision structure is chosen -- defaults to software, the only one
        before #608."""
        return self.sessions.create_session(request, context_cards=cards, slug=slug,
                                            perimeter=perimeter).slug

    def judge_grounding(self, request: str, *, cards: list[str] | None) -> Grounding:
        """Ask whether any installed context card describes this request's domain.

        **Report-only in this slice, and that is a scoping decision rather than the finished
        feature** (#593, `decision: the-engine-writes-the-missing-card`): the verdict is handed to
        the caller to render and changes no card selection. Acting on `installed` would narrow the
        selection, and the selection is half a session's identity (invariant 11), so it cannot be
        changed after `claim_session` has already claimed the slug -- see the issue for the three
        ways out and which one the record picked.

        Three things make this return rather than raise, each a state a caller must be able to tell
        from the others:

        - **Not asked, because the user chose.** An explicit `--context` is a human decision; paying
          to second-guess it would either agree at cost or disagree with nothing to do about it.
        - **Not asked, because this provider cannot.** `ContextJudge` is a protocol a provider may
          not implement, and a stub in a test is the common case. *Nobody looked* must never render
          as *no card is needed* -- that is the silent verdict #492 refused a status for.
        - **Asked, and here is the verdict.**

        Pinned by `test_an_explicit_card_selection_is_not_second_guessed`,
        `test_a_provider_that_cannot_judge_reports_not_asked_rather_than_no_card_needed` and
        `test_the_judgment_reaches_the_provider_with_one_line_per_installed_card`."""
        if cards:
            return Grounding(None, "the cards for this session were chosen with --context")
        provider = self._need_provider()
        if not isinstance(provider, ContextJudge):
            return Grounding(None, f"the {getattr(provider, 'name', 'current')} provider does not "
                                   f"answer grounding questions")
        summaries = card_summaries()
        if not summaries:
            # `load_context` refuses this install outright a moment later, and with a remedy this
            # method has no better version of. Saying "no card is needed" here would be the one
            # wrong answer. `test_an_install_with_no_cards_is_not_judged_as_needing_none`.
            return Grounding(None, "this install has no context cards to judge against")
        return Grounding(provider.judge_context(request, cards=summaries), "")

    def route_perimeter(self, request: str, *, perimeter: str | None) -> Routing:
        """Ask which installed perimeter, if any, this request's shape belongs to (#601) --
        `judge_grounding`'s own three-state honesty, one question over: `judgment is None` means
        nobody looked, never *no perimeter needed*.

        - **Not asked, because the user chose.** An explicit `--perimeter` is a human decision,
          exactly as an explicit `--context` suppresses `judge_grounding` — paying to second-guess
          it would either agree at cost or disagree with nothing the service is allowed to do about
          it.
        - **Not asked, because there is nothing to route between.** One installed perimeter is not a
          routing decision.
        - **Not asked, because this provider cannot.** `PerimeterJudge` is a protocol a provider may
          not implement.
        - **Asked, and here is the verdict.**

        A second, independent standalone call, deliberately not folded into `judge_grounding`'s own
        reply -- `decision: two-judgment-calls-not-one`.

        Pinned by `test_an_explicit_perimeter_is_not_second_guessed`,
        `test_a_single_installed_perimeter_is_not_judged`,
        `test_a_provider_that_cannot_route_reports_not_asked` and
        `test_the_routing_judgment_reaches_the_provider_with_one_line_per_installed_perimeter`."""
        if perimeter is not None:
            return Routing(None, "the perimeter for this session was chosen with --perimeter")
        if len(known_perimeter_ids()) <= 1:
            return Routing(None, "this install has only one perimeter")
        provider = self._need_provider()
        if not isinstance(provider, PerimeterJudge):
            return Routing(None, f"the {getattr(provider, 'name', 'current')} provider does not "
                                 f"route perimeters")
        self._check_spend()
        return Routing(provider.judge_perimeter(request, perimeters=perimeter_summaries()), "")

    def _delete_if_safe(self, meta, *, created: bool) -> bool:
        """The fourth of #593's four preconditions that authorise deleting an empty first-discovery
        claim, factored out so #601's router shares this exact check rather than a second copy of
        it (the issue's own instruction: ride the seam, do not duplicate the preconditions).

        The first three — the verdict warrants acting, the caller named nothing overriding it — are
        the caller's to have already established; this one is re-read fresh under the lock, because
        the judgment that produced the verdict took real time and a stale authorisation is how a
        delete stops being safe (invariant 9). Returns whether it actually deleted."""
        if not created:
            return False
        with self.sessions.repo.lock(meta.slug):
            if self.sessions.repo.read_meta(meta.slug).current_revision != 0:
                return False
            self.sessions.delete_session(meta.slug)
        return True

    def _reclaim_under(self, meta, *, request: str, slug: str | None, cards: list[str] | None,
                       perimeter: str, created: bool, provider) -> Reclaim:
        """Delete the empty session `meta` names and recreate it under a narrower identity — cards,
        perimeter, or both call this, so the destructive step has exactly one implementation
        (invariant 14, and #601's instruction not to duplicate #593's four preconditions).

        Returns a `Reclaim` -- see its own docstring for why a single boolean stopped being enough.
        `landed` is `False` only when `_delete_if_safe` found the fourth precondition no longer held
        (`meta` handed back exactly as given, nothing touched); otherwise the recreate always lands
        under the requested identity, freshly (`created=True`) or by matching an existing session
        that already had it (`created=False`) -- `create_session_report`'s own boolean, not
        asserted, because the caller must never read an idempotent re-entry as a delete this call is
        authorised to make (#601 P1). Pinned by
        `test_reclaiming_onto_a_pre_existing_session_never_authorises_deleting_it`."""
        if not self._delete_if_safe(meta, created=created):
            return Reclaim(meta, False, False)
        meta, recreated = self.sessions.create_session_report(
            request, context_cards=cards, slug=slug,
            provider=provider.name, model_name=provider.model_name(), perimeter=perimeter)
        _require_revision_zero(meta.slug, meta.current_revision)
        return Reclaim(meta, True, recreated)

    def claim_and_ground(self, request: str, *, cards: list[str] | None, slug: str | None,
                         perimeter: str | None = None
                         ) -> ClaimAndGround:
        """Claim the session, route it to the perimeter its shape fits (#601), judge its context
        grounding (#593), and act on each judgment when acting is safe.

        The whole sequence lives here rather than in a surface, because acting on either verdict
        means *deleting a session*, and a destructive step on the discovery path must have one
        implementation with one set of preconditions (invariant 14) — `_reclaim_under`/
        `_delete_if_safe` are that implementation, shared by both judgments below.

        **Resolve an existing session before claiming or routing at all.** A repeat of a request
        already routed to a non-default perimeter has an identity the *default*-perimeter claim
        never matches — claiming under software first, when an earlier call already landed this
        exact request on go-to-market, creates a fresh, unrelated placeholder that passes the
        revision-zero gate, and the router gets billed before the existing session is ever found
        (#601, Codex review round four: the free refusal invariant 13 promises slipped past on
        exactly the call it exists to save). So when the caller named no `--perimeter`,
        `find_existing_session` is asked once per *installed* perimeter, free, before anything is
        claimed; a match fixes `perimeter` to the one it was found under, exactly as if the caller
        had named it, and every step below proceeds unchanged from there.

        **Claim first, under the resolved perimeter** — the free gate stays ahead of every paid
        call, so a repeat discovery is refused before either judgment is billed, not after
        (invariant 13, #133); the router's own call is exactly as paid as the grounding judgment's,
        so it earns no exception. `perimeter=None` reaching this point (no explicit choice, no
        existing session found) is a genuinely first-time request — `route_perimeter` is what turns
        that into a claimable default, the same way `cards=None` already means *every card* until
        `judge_grounding` narrows it.

        **Route, and re-claim only when the verdict `fits` a perimeter other than the one claimed**,
        under the identical preconditions #593's own re-claim relies on. An `ambiguous` verdict
        deletes the same empty claim, under the same preconditions, and refuses with
        `AmbiguousPerimeterError` naming the candidates — before any model is reasoned, the
        invariant-13 shape applied to a second cause. A `none` verdict leaves the session under the
        default perimeter, stated rather than assumed, and the session continues.

        **Then judge grounding, and re-claim under narrowed cards** exactly as before #601 — only
        which perimeter it recreates under can have moved, since routing may already have re-claimed
        once.

        Returns a `ClaimAndGround` -- see its own docstring for the shape and why attribute access,
        not positional unpacking, is the supported form. Pinned by
        `test_a_narrowing_verdict_reclaims_under_the_narrowed_identity`,
        `test_a_session_this_call_did_not_create_is_never_deleted_by_a_verdict`,
        `test_a_session_that_moved_off_revision_zero_during_the_judgment_is_left_alone`,
        `test_a_fitting_perimeter_verdict_reroutes_and_reclaims`,
        `test_an_ambiguous_verdict_refuses_before_any_model_is_reasoned`,
        `test_an_explicit_perimeter_is_never_overridden_by_the_router`,
        `test_a_none_verdict_continues_under_the_default_perimeter_named`,
        `test_an_idempotent_reclaim_onto_the_correct_identity_reports_that_identity_not_the_old_one`,
        `test_claim_and_ground_resolves_an_existing_non_default_perimeter_session_before_routing`
        and `test_a_failed_routing_call_does_not_lock_the_retry_into_the_default_perimeter`."""
        provider = self._need_provider()
        if perimeter is None:
            for pid in known_perimeter_ids():
                existing = self.sessions.find_existing_session(
                    request, context_cards=cards, perimeter=pid, slug=slug)
                if existing is not None:
                    perimeter = pid
                    break
        claim_perimeter = perimeter or DEFAULT_PERIMETER
        meta, created = self.sessions.create_session_report(
            request, context_cards=cards, slug=slug,
            provider=provider.name, model_name=provider.model_name(), perimeter=claim_perimeter)
        _require_revision_zero(meta.slug, meta.current_revision)

        try:
            routing = self.route_perimeter(request, perimeter=perimeter)
        except (RequivoError, KeyboardInterrupt):
            # The routing *call* itself did not complete -- a transport failure, a retry-exhausted
            # reply, an interrupt, a spend refusal. Left as it was, this placeholder (claimed under
            # a guessed perimeter, never asked) would be indistinguishable from a session whose
            # routing genuinely landed on that perimeter -- and a repeat's own `find_existing_session`
            # (this method's own first move, above) would find it and read the guess as a decision,
            # never asking the router again (#601 P2, Codex review round five: round four traded
            # "pays before refusing" for "never routes again", which is worse -- the first costs a
            # call, the second silently reasons the whole session under the wrong vocabulary and
            # tells the user they chose it). Cleaned up here rather than marked, deliberately: a
            # persisted "routing completed" field would touch the public session format (invariant
            # 8) for every session, including the ones that never route at all (an explicit
            # `--perimeter`, a single-perimeter install), and would still need to encode *why* a
            # session sits at the default to avoid re-deriving this same ambiguity one field later --
            # where the empty claim this call just made is disposable by construction, the same rule
            # the ambiguous-verdict branch below already lives by. Pinned by
            # `test_a_failed_routing_call_does_not_lock_the_retry_into_the_default_perimeter`.
            self._delete_if_safe(meta, created=created)
            raise
        route_judgment = routing.judgment
        if route_judgment is not None:
            if route_judgment.decision is PerimeterDecision.ambiguous:
                self._delete_if_safe(meta, created=created)
                # `reason` is LLM-authored prose over an untrusted request (SECURITY.md): escaped
                # for the message a human reads (the CLI writes `str(error)` to stderr verbatim,
                # with no renderer between this raise and the terminal), raw in `details` for a
                # `--json` consumer (#601 P2: a forged reason containing a control sequence must
                # not reach the terminal unescaped, the same rule invariant 14 holds for a stored
                # name forging a line of `doctor`).
                raise AmbiguousPerimeterError(
                    f"more than one installed perimeter could fit this request -- "
                    f"{display_text(route_judgment.reason)} Name one explicitly, e.g. --perimeter "
                    f"{route_judgment.candidates[0]} (candidates: "
                    f"{', '.join(route_judgment.candidates)}).",
                    details={"candidates": route_judgment.candidates,
                             "reason": route_judgment.reason})
            if (route_judgment.decision is PerimeterDecision.fits
                    and route_judgment.perimeter != claim_perimeter):
                reclaim = self._reclaim_under(
                    meta, request=request, slug=slug, cards=cards,
                    perimeter=route_judgment.perimeter, created=created, provider=provider)
                # `meta`/`claim_perimeter` are read off `reclaim.meta` unconditionally -- it is
                # always the truth about where the claim now is, landed or not (#601 P2, round
                # three: `claim_perimeter` used to be left unmoved only in the `else` arm below,
                # which happened to be correct only because `meta` was *also* unchanged there; this
                # reads it the same way in both arms instead of by coincidence). `created` is
                # `reclaim.created`, never `reclaim.landed` -- the ownership fact the *next*
                # reclaim's own precondition check needs, not whether this one took effect.
                meta, created = reclaim.meta, reclaim.created
                claim_perimeter = resolve_perimeter(meta.perimeter)
                if not reclaim.landed:
                    # The route could not be applied -- a prior claim under `claim_perimeter`
                    # already existed (#601 P2: a session left behind by an interrupted or failed
                    # earlier judgment is the reachable case) or the fourth precondition was gone by
                    # the time we acted. The rendered verdict must say so rather than announce a
                    # route that did not land -- reasoning under one perimeter while the screen
                    # names another is a confident wrong answer, the exact failure this router
                    # exists to remove. Pinned by
                    # `test_a_route_that_cannot_land_is_not_announced_as_though_it_did`.
                    routing = Routing(
                        None,
                        f"the router found {route_judgment.perimeter!r} a better fit, but this "
                        f"session was already claimed under {claim_perimeter!r} and could not be "
                        f"moved there")

        grounding = self.judge_grounding(request, cards=cards)
        judgment = grounding.judgment
        if judgment is None or judgment.decision is not ContextDecision.installed:
            return ClaimAndGround(meta, grounding, cards, routing)
        narrowed = resolve_cards(judgment.cards)
        if cards or not created or not narrowed or narrowed == cards:
            return ClaimAndGround(meta, grounding, cards, routing)

        reclaim = self._reclaim_under(
            meta, request=request, slug=slug, cards=narrowed, perimeter=claim_perimeter,
            created=created, provider=provider)
        # `reclaim.meta.context_cards` is the truth about what this session now records, landed or
        # not -- reading it directly, rather than inferring the cards from `reclaim.created`, is
        # exactly the fix for #601 P2 round three: an idempotent re-entry onto a session that
        # already had the narrowed identity used to report the *pre-narrowing* cards, so `start()`
        # went on to reason over every card against a session whose own `session.json` said
        # otherwise.
        return ClaimAndGround(reclaim.meta, grounding, reclaim.meta.context_cards, routing)

    def claim_session(self, request: str, *, cards: list[str] | None, slug: str | None,
                      perimeter: str = DEFAULT_PERIMETER):
        """Create (or reuse) the session a first discovery will land on, and hold it to revision 0.

        Idempotent creation and "a discovery replaces the model" are each reasonable alone and unsafe
        together: the second `discover` of the same request lands on the first one's session. This is
        the single gate, so every entry point — `start`, `finalize_discovery`, the CLI's interactive
        loop — refuses the same case in the same words.

        **Public because a surface that owns its own loop has to be able to take the gate itself.**
        `start()` claims before it reasons; the CLI's interactive branch could only reach this through
        `finalize_discovery`, which runs *after* up to nine provider calls, so the invariant held on
        the path that documents it and not on the one a person uses at a terminal (#133). Pinned by
        `test_both_discover_entry_points_refuse_a_refined_session_before_paying`."""
        provider = self._need_provider()
        meta = self.sessions.create_session(
            request, context_cards=cards, slug=slug,
            provider=provider.name, model_name=provider.model_name(), perimeter=perimeter)
        _require_revision_zero(meta.slug, meta.current_revision)
        return meta

    def finalize_discovery(self, request: str, out: EngineOutput, *, cards: list[str] | None = None,
                           slug: str | None = None, brief=None, surface: str = "discover",
                           usage: dict | None = None, perimeter: str = DEFAULT_PERIMETER) -> str:
        """Create the session and apply a discovered model through the validated path. When a `brief` is
        given (a finalized discovery), its reasoning is absorbed into the model first. Shared by the
        CLI's interactive loop (which produced `out` itself) and `start()`.

        A first discovery lands on revision 0 and nothing else: creation is idempotent, so without
        that precondition a re-run silently replaces a model refined over several turns with a naive
        first-turn one. A `revision_conflict` is recoverable; a silent replacement is not. Pinned by
        `test_both_discover_entry_points_refuse_a_refined_session_before_paying`.

        `perimeter` (#608) must be the one `out` was actually reasoned against -- the interactive loop
        passes the perimeter it drove `draft_turn` with, `start()` its own.

        `usage` is threaded through rather than computed here — this method makes no provider call of
        its own, so it has no `before` index to measure from. A caller that passes none produces a
        revision with no usage provenance: absent rather than wrong, per invariant 6."""
        meta = self.claim_session(request, cards=cards, slug=slug, perimeter=perimeter)
        if brief is not None:
            absorb_reasoning(out, brief)
        self.sessions.update_model(
            meta.slug, out.model_dump_json(), expected_revision=0,
            provenance=self._provenance("analyze", cards=cards, surface=surface, usage=usage,
                                        perimeter=perimeter))
        return meta.slug

    def start(self, request: str, *, cards: list[str] | None = None, slug: str | None = None,
              finalize: bool = False, surface: str = "discover",
              perimeter: str = DEFAULT_PERIMETER) -> str:
        """Run one discovery turn on a fresh request and apply it, returning the session slug. With
        `finalize`, also produce and absorb the solution assessment's reasoning.

        The session is claimed *before* the provider is called -- refusing a re-discovery after the
        call means having paid for reasoning that can only be thrown away.

        Claiming is not the sole guarantee (#209): two callers of the same request can both pass the
        revision-zero check before either has paid for anything, so `_discovery_guard` below decides
        which proceeds, and the revision is re-checked fresh immediately after winning the guard
        rather than trusted from the check above --
        `test_a_concurrent_first_discovery_is_refused_before_any_provider_call` and
        `test_a_late_caller_of_start_with_a_stale_outer_check_still_pays_nothing`.

        **`finalize` used to reason both calls before writing either (#467).** A refused or failed
        brief call discarded the already-billed `analyze()` result every time, with the session left
        at revision 0 as though nothing had been paid for. `finalize_discovery` now runs immediately
        after `analyze()`, landing revision 1 before the brief is even attempted, and the brief is
        folded in through the ordinary `generate(slug, "brief")` path -- never a total loss of the
        `analyze()` spend. Pinned by `test_a_failed_brief_leaves_the_analyzed_discovery_applied_467`."""
        provider = self._need_provider()
        meta = self.claim_session(request, cards=cards, slug=slug, perimeter=perimeter)
        with _discovery_guard(meta.slug, self._store_for_repo()):
            _require_revision_zero(meta.slug, self.sessions.repo.read_meta(meta.slug).current_revision)
            ledger = current_ledger()
            before = len(ledger.calls) if ledger is not None else 0
            self._check_spend()
            with self._provider_call("analyze"):
                out = provider.analyze(request, only=cards, perimeter=perimeter)
            slug_out = self.finalize_discovery(request, out, cards=cards, slug=meta.slug,
                                               surface=surface, usage=_usage_since(before),
                                               perimeter=perimeter)
            if finalize:
                self.generate(slug_out, "brief", surface=surface)
            return slug_out

    # ── interactive drafting (before there is a session) ─────────────────────────
    # An interactive surface reasons several turns against a request that has not been persisted
    # yet, then claims a session and applies the result. The operations below are that loop's
    # provider calls, so a surface owns the *loop* and never a client -- the arrow
    # `tests/test_boundaries.py` guards from both ends, via
    # `test_the_surfaces_reach_the_provider_only_through_the_named_surface_concerns` and
    # `test_the_loop_reasons_through_the_service_and_carries_the_model_not_a_transcript`.
    #
    # Not a callback and not a generator: the service is handed state and returns a result. A seam
    # that reached back into the caller to ask a question would move the coupling rather than remove
    # it, and `DiscoveryService` would be the layer that knows a terminal exists.
    #
    # Nothing here writes, so there is no revision, provenance or lock to get wrong.

    def draft_turn(self, request: str, *, current_model: EngineOutput | None = None,
                   answers: str | None = None, cards: list[str] | None = None,
                   perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
        """One un-persisted discovery turn: the request alone on the first call, then the model so far
        plus the answers just given.

        The model *is* the accumulated state — a turn needs the original request for context, the
        current model, and the new answers, and nothing else — which is what lets the same operation
        serve a blocking TTY loop, a web form and a Claude Code turn.

        `reuse_system=True` because this is the one operation here a caller repeats, so the cache
        breakpoint is genuinely read back and earns its 1.25x write; every other operation is one
        call per invocation and says the opposite. Pinned by
        `test_the_loop_declares_its_repeated_prompt_at_the_seam`, which carries its own must-fire
        control.

        The size cap runs here too, not only where a session is finally created: this turn is
        un-persisted and resends the request every call, so `create_session`'s check alone would let
        a wide request pay for a whole loop of billed calls before `finalize_discovery` is reached.
        `answers` is checked only when a caller supplied one. Pinned by
        `test_draft_turn_refuses_an_oversized_request_before_reasoning` and
        `test_draft_turn_refuses_oversized_answers_before_reasoning`."""
        require_input_within_bounds(request, field="request")
        if answers is not None:
            require_input_within_bounds(answers, field="answers")
        self._check_spend()
        with self._provider_call("analyze"):
            return self._need_provider().analyze(
                request, current_model=current_model, answers=answers, only=cards, reuse_system=True,
                perimeter=perimeter)

    def run_discovery(self, slug: str, *, surface: str = "discover") -> UpdateResult:
        """Run the first discovery turn on an already-created session (the 'create session only' path
        run later): read its stored request + cards, reason, and apply the model as revision 1.

        Held to revision 0 like every other first discovery, and held *before* the provider call:
        this reasons from the request alone — it never sees the current model — so on a session that
        has been refined it would write a naive first-turn model over that work, with the optimistic
        lock satisfied throughout (it reads revision N and writes against N). The `POST
        /sessions/{slug}/discover` route reaches this directly; the Web only offers the button at
        revision 0, but that is a rendering decision, not a rule.

        `_discovery_guard` is what actually serialises two concurrent callers of this route —
        `_require_revision_zero` cannot, since both read the same revision-0 snapshot before either
        has written. And the pre-guard check is a fast-fail, not the guarantee: the snapshot is
        re-taken *inside* the guard, so a caller merely slow to reach it cannot acquire it
        uncontended on a stale belief and pay for a call it was always going to lose. Pinned by
        `test_a_concurrent_first_discovery_is_refused_before_any_provider_call` and
        `test_a_late_caller_with_a_stale_outer_check_still_pays_nothing`."""
        self.sessions.ensure_canonical(slug)
        snap = self.sessions.snapshot(slug)
        _require_revision_zero(slug, snap.revision)
        with _discovery_guard(slug, self._store_for_repo()):
            # Fresh, not the snapshot above -- see the guard note in this method's own docstring.
            snap = self.sessions.snapshot(slug)
            _require_revision_zero(slug, snap.revision)
            ledger = current_ledger()
            before = len(ledger.calls) if ledger is not None else 0
            self._check_spend()
            with self._provider_call("analyze"):
                out = self._need_provider().analyze(snap.request, only=snap.context_cards,
                                                    perimeter=snap.perimeter)
            return self.sessions.update_model(
                slug, out.model_dump_json(), expected_revision=snap.revision,
                provenance=self._provenance("analyze", cards=snap.context_cards, surface=surface,
                                            usage=_usage_since(before), perimeter=snap.perimeter))

    # ── refinement ───────────────────────────────────────────────────────────────
    def answer(self, slug: str, answers: str, *, expected_revision: int | None = None,
               surface: str = "answer") -> UpdateResult:
        """Fold the user's answers into a session's model as a new revision.

        A turn has the same seam as a generation: the provider reasons over the model as it was, and the
        session can move meanwhile. So the precondition defaults to the revision this turn actually read
        — a caller that knows better (the Web, which carries the revision the user saw in the form) can
        still pass its own. The turn reasons from one coherent `SessionSnapshot` — the revision it will
        be held to and the model it reasoned over are the same read, not two. A legacy `out/` session is
        migrated first, so there is always a real revision to hold it to.

        A caller-supplied precondition that is *already* stale against the snapshot is refused here,
        before the call — see `_require_no_conflict_yet` (#205).

        The size cap on `answers` runs first, before any of the above: a caller past the Web's own
        friendly re-render (invariant 14) still needs the refusal, and it costs nothing to check
        before a snapshot read or a revision comparison that an oversized answer would waste (#255).

        A session at revision 0 has no model to fold anything into — `_require_a_model` refuses it
        before the provider is ever built (#421, the mirror of #152 one write verb over). Without the
        gate `snap.model` is `None` and the provider's `analyze()` falls through to its own
        first-discovery branch: the answers the caller typed appear in no kwarg of the call, the reply
        is applied as revision 1 with `cli-answer`/`web-answer` provenance regardless, and the write
        bypasses `run_discovery`'s own double-submission guard. Pinned by
        `test_answer_refuses_a_session_that_has_no_model_yet` (zero provider calls); `answer` still
        working at revision >= 1 is the existing control, `test_an_answers_turn_holds_the_revision_it_read`.
        """
        require_input_within_bounds(answers, field="answers")
        self.sessions.ensure_canonical(slug)
        snap = self.sessions.snapshot(slug)
        _require_no_conflict_yet(slug, expected_revision, snap)
        model = _require_a_model(slug, snap)
        ledger = current_ledger()
        before = len(ledger.calls) if ledger is not None else 0
        self._check_spend()
        with self._provider_call("analyze"):
            out = self._need_provider().analyze(
                snap.request, current_model=model, answers=answers, only=snap.context_cards,
                perimeter=snap.perimeter)
        return self.sessions.update_model(
            slug, out.model_dump_json(),
            expected_revision=expected_revision if expected_revision is not None else snap.revision,
            provenance=self._provenance("analyze", cards=snap.context_cards, surface=surface,
                                        usage=_usage_since(before), perimeter=snap.perimeter))

    # ── generation ───────────────────────────────────────────────────────────────
    def reason(self, slug: str, artifact_type: str, **kwargs):
        """Produce an artifact's typed contract without saving anything — an analysis a caller wants
        to read rather than file. `stories` and `estimate` were terminal-only and reached the CLI
        through here until #519; both are saveable through `generate()` now, and this stays for the
        caller that wants the contract and no write. Still goes through the
        provider seam, so no interface reaches past it to a vendor's functions -- `cli.py` built its
        own second client for this exact call until #77:
        `test_the_surfaces_reach_the_provider_only_through_the_named_surface_concerns`. Nothing is
        written, so there is no provenance to get wrong — but the model and the cards it is read
        against still come from one snapshot, so the analysis is of a session state that actually
        existed.

        `**kwargs` is what an analysis needs beyond the model: `estimate` is read against the
        `stories` a previous call produced."""
        return self.reason_from(self.sessions.snapshot(slug), artifact_type, **kwargs)

    def reason_from(self, snap: SessionSnapshot, artifact_type: str, **kwargs):
        """The same analysis, from a snapshot the caller already holds.

        For the one analysis that is *two* calls: `estimate` is read against the `stories` a previous
        call produced, and taking a snapshot per call let the two be read against two revisions — the
        "two reads, two instants" invariant 12 is written about. Nothing here is written, so no
        provenance can be a lie; what drifts is the answer, which shows both halves side by side and
        names no revision. A caller that renders between the two calls needs the snapshot rather than
        a combined operation, and the snapshot carries its own slug so the two cannot disagree (#135).
        Pinned by `test_the_estimate_verb_reads_stories_and_estimate_from_one_snapshot`."""
        _require_owned_artifact_type(snap.perimeter, artifact_type)
        model = _require_a_model(snap.slug, snap)
        self._check_spend()
        with self._provider_call(artifact_type):
            return self._need_provider().generate(artifact_type, model, only=snap.context_cards,
                                                  **kwargs)

    # `generate()`'s public signature is these nine overloads, not the implementation below. Eight
    # are `Literal`-keyed so a call site written with a literal string gets that type's contract back;
    # the ninth takes a plain `str` for a caller holding the name in a variable (a route parameter,
    # e.g. `web/routes/artifacts.py`'s `generate_artifact`). `decision: typed-generation-seam`
    # `estimate` is the one whose extra keyword is not forwarded to the provider: `on_stories` is
    # the caller's hook for the first of its two calls (see `_generate_estimate`).
    @overload
    def generate(self, slug: str, artifact_type: Literal["brief"], *, surface: str = "generate",
                **kwargs) -> Generated[Brief]: ...
    @overload
    def generate(self, slug: str, artifact_type: Literal["gtm_plan"], *, surface: str = "generate",
                **kwargs) -> Generated[GoToMarketPlan]: ...
    @overload
    def generate(self, slug: str, artifact_type: Literal["prd"], *, surface: str = "generate",
                **kwargs) -> Generated[PRD]: ...
    @overload
    def generate(self, slug: str, artifact_type: Literal["stories"], *, surface: str = "generate",
                **kwargs) -> Generated[Stories]: ...
    @overload
    def generate(self, slug: str, artifact_type: Literal["criteria"], *, surface: str = "generate",
                **kwargs) -> Generated[AcceptanceCriteria]: ...
    @overload
    def generate(self, slug: str, artifact_type: Literal["estimate"], *, surface: str = "generate",
                on_stories: Callable[[Stories], None] | None = None) -> Generated[SavedEstimate]: ...
    @overload
    def generate(self, slug: str, artifact_type: Literal["epic"], *, surface: str = "generate",
                **kwargs) -> Generated[Epic]: ...
    @overload
    def generate(self, slug: str, artifact_type: Literal["release"], *, surface: str = "generate",
                **kwargs) -> Generated[ReleaseNotes]: ...
    @overload
    def generate(self, slug: str, artifact_type: str, *, surface: str = "generate",
                **kwargs) -> Generated[object]: ...

    def generate(self, slug: str, artifact_type: str, *, surface: str = "generate", **kwargs):
        """Generate an artifact through the provider and save it against the session with its source
        revision. Every interface goes through here, so a given artifact is produced, saved and tracked
        identically whether it was asked for from the terminal, the browser, or Claude Code.

        `brief` and `gtm_plan` (#609) are the ones with an extra step, one per perimeter: each
        artifact's reasoning is absorbed back into the model as a revision (`_ASSESSMENT_ARTIFACTS`),
        so downstream artifacts inherit the decisions/exclusions/thresholds, not just the facts.
        `estimate` is the one with two calls — see `_generate_estimate`.

        **Generation is not atomic.** A provider call runs for seconds to minutes, and the session can
        move underneath it — a second browser tab folding in answers, a CLI apply, a Claude Code turn.
        So the revision the model was read at is captured *before* the call and carried through both
        writes: as the optimistic-lock precondition on any apply (a concurrent change becomes a clean
        conflict instead of silently overwriting that revision) and as the artifact's recorded source
        (so a document written from revision 1 is never filed as if it came from revision 2).

        The revision and the model come from one `SessionSnapshot`, because reading them separately
        made the provenance a lie in the other direction: a write landing between the two reads gave
        revision N with the model of N+1, and the artifact was generated from the newer model and
        filed against the older revision — a mismatch nothing downstream can detect, since the number
        is perfectly plausible."""
        self.sessions.ensure_canonical(slug)  # migrate a legacy session before its first artifact write
        snap = self.sessions.snapshot(slug)
        _require_owned_artifact_type(snap.perimeter, artifact_type)
        source_revision, cards = snap.revision, snap.context_cards
        out = _require_a_model(slug, snap)
        provider = self._need_provider()

        if artifact_type in _ASSESSMENT_ARTIFACTS:
            spec = _ASSESSMENT_ARTIFACTS[artifact_type]
            ledger = current_ledger()
            before = len(ledger.calls) if ledger is not None else 0
            self._check_spend()
            with self._provider_call(artifact_type):
                brief = provider.generate(artifact_type, out, only=cards)
            spec.absorb(out, brief)
            usage = _usage_since(before)
            # `out` is the revision-N model plus the reasoning just derived from it. Applying it without
            # the precondition would discard any revision that landed while the provider was reasoning.
            try:
                applied = self.sessions.update_model(
                    slug, out.model_dump_json(), expected_revision=source_revision,
                    provenance=self._provenance(artifact_type, cards=cards, surface=surface,
                                                usage=usage, perimeter=snap.perimeter))
            except RevisionConflictError as e:
                # The paid assessment is not thrown away merely because the apply lost the race:
                # filing it against an older source revision, flagged stale, is legal by invariant 2.
                # What genuinely did not happen is the reasoning's absorption, so both facts and the
                # remedy go in one message — a caller reading only `.message` gets the whole story
                # with no special-casing. Pinned by
                # `test_a_brief_lost_to_a_revision_conflict_is_still_saved_stale_not_discarded`.
                try:
                    status = self._save_generated(
                        slug, artifact_type, spec.writer(out, brief), source_revision)
                except ArtifactWriteFailedError as write_err:
                    # Two failures at once: the apply lost the race AND the fallback save that was
                    # meant to preserve the paid content also failed at the filesystem. Both facts are
                    # stated, not just one, and this is chained from the write failure rather than
                    # re-raised as the conflict, since the write failure is the more urgent, unresolved
                    # one -- `test_a_conflict_plus_a_secondary_write_failure_states_both_not_just_one`.
                    raise ArtifactWriteFailedError(
                        f"{write_err.message} This session also lost a revision race in the same "
                        f"call: {e.message}. The {spec.label}'s reasoning was NOT absorbed into the "
                        "model either way.",
                        details={**write_err.details, "revision_conflict": True,
                                 "revision_conflict_message": e.message}) from e
                raise RevisionConflictError(
                    f"{e.message}. The {spec.label} was still generated and saved against revision "
                    f"{source_revision} (now flagged stale); its reasoning was NOT absorbed into the "
                    f"model. `requivo {spec.cli_verb} {slug}` (or the Web's Regenerate) will refresh "
                    "both.",
                    details={**e.details, "artifact_saved": True, "artifact_type": artifact_type,
                             "artifact_stale": status.stale}) from e
            # The assessment renders exactly the model that apply just wrote, so it belongs to that revision.
            status = self._save_generated(slug, artifact_type, spec.writer(out, brief), applied.revision)
            return Generated(status=status, artifact=brief, model=out)

        if artifact_type == "estimate":
            return self._generate_estimate(slug, out, cards, source_revision, provider, **kwargs)

        try:
            writer = _WRITERS[artifact_type]
        except KeyError as e:
            raise ValueError(f"{artifact_type!r} has no saveable document — use `reason()`") from e
        self._check_spend()
        with self._provider_call(artifact_type):
            artifact = provider.generate(artifact_type, out, only=cards, **kwargs)
        status = self._save_generated(slug, artifact_type, writer(artifact), source_revision)
        return Generated(status=status, artifact=artifact, model=out)

    def _generate_estimate(self, slug: str, out: EngineOutput, cards: list[str] | None,
                           source_revision: int, provider, *,
                           on_stories: Callable[[Stories], None] | None = None,
                           **kwargs) -> Generated[SavedEstimate]:
        """The one generation that is two calls, and saves two files against one revision (#519).

        The estimate is reasoned *against a stories draft* — `estimate.md`'s prompt takes the stories,
        not the model — so its basis is the model at `source_revision` **and** those stories. Saving
        the estimate alone would record half of that (invariant 6: provenance real or absent), which
        is why the stories are reasoned here, from the same snapshot `generate()` already took (#135,
        invariant 12), and saved beside it with the same `source_revision` before the second call is
        made. Pinned by
        `test_generating_the_estimate_saves_the_stories_it_was_reasoned_against_from_one_snapshot`.

        The stories are saved *before* they are handed to `on_stories`, and before the estimate is
        paid for: a caller that renders them (the CLI, so they appear while the estimate runs) can
        die on a console that cannot encode them, and the file has to be on disk by then rather than
        after; and a provider failure on the second call leaves the first call's document filed,
        stale-tracked, rather than a paid reply thrown away.

        No other keyword is accepted — in particular not `stories`. A caller-supplied draft would file
        an estimate whose recorded basis is not the file beside it, the exact half-truth this branch
        exists to prevent. Pinned by
        `test_the_estimate_generation_refuses_a_caller_supplied_stories_draft`."""
        if kwargs:
            raise TypeError(
                f"generate('estimate') reasons its own stories and takes no provider keyword; got "
                f"{sorted(kwargs)}. Use `reason_from(snap, 'estimate', stories=...)` for an unsaved "
                "estimate over stories you already hold.")
        self._check_spend()
        with self._provider_call("stories"):
            stories = cast(Stories, provider.generate("stories", out, only=cards))
        stories_status = self._save_generated(slug, "stories", _WRITERS["stories"](stories),
                                              source_revision)
        if on_stories is not None:
            on_stories(stories)
        self._check_spend()
        with self._provider_call("estimate"):
            estimate = provider.generate("estimate", out, only=cards, stories=stories)
        status = self._save_generated(slug, "estimate", _WRITERS["estimate"](estimate),
                                      source_revision)
        draft, soft, confidence = estimate
        return Generated(status=status, model=out,
                         artifact=SavedEstimate(draft=draft, soft=soft, confidence=confidence,
                                                stories=stories, stories_status=stories_status))

    def _save_generated(self, slug: str, artifact_type: str, content: str, source_revision: int):
        """Save a generated artifact against the revision it was actually produced from.

        An artifact written from revision 1 while revision 2 was landing must not inherit revision 2's
        freshness — that is the one case where a stale document reports itself as up to date. This used
        to be handled here, by re-diffing after the write and replaying the change through the graph.
        It now belongs to `ArtifactService.save`, which does it for *every* caller rather than only the
        provider path: the same hazard reaches a Claude Code turn saving a document it wrote earlier.
        Passing the honest source revision is the whole contribution this layer needs to make.

        And the one place every generated artifact's write is caught. The content reaching here was
        already paid for, so a filesystem failure must not surface as a bare traceback out from under
        that call: `ArtifactWriteFailedError` names what was lost and where it was going. The caller
        still has to regenerate — the content was never handed back to be retried. Pinned by
        `test_an_oserror_writing_a_generated_artifact_is_a_structured_refusal_not_a_traceback`, and
        `test_a_conflict_plus_a_secondary_write_failure_states_both_not_just_one` for the case where
        both go wrong at once."""
        try:
            return self.artifacts.save(slug, artifact_type, content, source_revision=source_revision)
        except OSError as e:
            filename = ARTIFACT_FILENAMES.get(artifact_type)
            target = artifact_path(slug, filename) if filename else None
            raise ArtifactWriteFailedError(
                f"{artifact_type!r} was generated for session '{slug}' but could not be saved"
                f"{f' to {target}' if target else ''}: {e}",
                details={"slug": slug, "type": artifact_type,
                         "path": str(target) if target else None,
                         "cause": f"{type(e).__name__}: {e}"}) from e
