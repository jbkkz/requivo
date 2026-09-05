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
from typing import Any, Callable, Generic, Literal, TypeVar, cast, overload

from requivo.core.contracts import PRD, AcceptanceCriteria, Brief, EngineOutput, Epic, ReleaseNotes
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import (
    ArtifactWriteFailedError,
    InvalidSlugError,
    RevisionConflictError,
    SessionLockedError,
    SessionUnreadableError,
)
from requivo.core.persistence import (
    ArtifactStatus,
    Store,
    _refuse_new_reserved_slug,
    _slug_shape,
    artifact_path,
    is_contained,
)
from requivo.core.validation import require_input_within_bounds
from requivo.paths import workspace_root
from requivo.render.markdown import brief_markdown, criteria_markdown, epic_markdown, prd_markdown, release_markdown
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

# artifact type → the writer that turns its contract into the Markdown that gets saved. This is the
# vocabulary of "things a generation produces a document for"; `stories` and `estimate` are absent on
# purpose — they are terminal analyses that feed the estimate pipeline, not deliverables with a file.
#
# The annotation is load-bearing: dropping it makes pyright infer a union of four narrow callables
# that no argument satisfies. `decision: typed-generation-seam`
_WRITERS: dict[str, Callable[[Any], str]] = {
    "prd": prd_markdown,
    "criteria": criteria_markdown,
    "epic": epic_markdown,
    "release": release_markdown,
}

# Everything `generate()` can produce, in the order a user meets them. This is the source every
# interface asks — the CLI's verbs, the Web's buttons — so a new generator becomes available
# everywhere by being registered here, rather than by each surface keeping its own list and drifting.
GENERATABLE: tuple[str, ...] = ("brief", *_WRITERS)

_A = TypeVar("_A")


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
    """Persist the assessment's reasoning (decisions, challenges, opportunities) into the model so every
    generator inherits it, not just the facts. Called wherever the assessment is produced, before the
    model is applied — the single definition, shared by the CLI and the Web."""
    out.decisions = brief.decisions
    out.challenges = brief.challenges
    out.opportunities = brief.opportunities


def _discovery_guard_path(slug: str, store: Store) -> Path:
    """The in-flight first-discovery guard for `slug`: `<workspace>/.requivo/locks/<slug>.discovering`.

    A sibling of `core.persistence.Store.lock_path`, deliberately a *different* file (#209). That
    lock covers a compound write and is released **before** a provider call starts — a call runs
    seconds to minutes and cannot hold a write lock open that long, by that lock's own docstring —
    which is exactly the window two concurrent first-discovery requests can both walk into: both read
    revision 0, both pass `_require_revision_zero`, and both are free to pay for a provider call
    before either has written anything. This file exists to serialise *that* window, without touching
    the write lock at all.

    **`store` names which workspace this guard addresses** (#272's scope amendment) — it used to read
    `lock_root()`/`session_root()` ambiently, which is exactly the leak this issue closes; the caller
    resolves it from its own repository (`DiscoveryService._store_for_repo`), so a discovery guard for
    an explicitly-rooted session no longer silently reads a different root than the one it is guarding.

    Validated exactly as `lock_path` validates its own -- the shape unconditionally, the reserved
    Windows device name only when nothing already occupies the slug (#372's creation/read split).
    The slug reaches here from the service layer and, under invariant 14, an external consumer may
    call this layer directly.

    **This function was written one commit before that split and was missed by it** (#390). `e03aa47`
    added it calling `validate_slug`; `3fa1423`, the very next commit, swept `_child_of` and
    `lock_path` onto the conditional form -- and a sweep cannot see a sibling that did not exist when
    it was written. The cost was a session already on disk under a reserved name that every read verb
    could reach, and that `run_discovery` alone refused: the guard meant to serialise a paid call
    turned into the one thing standing between that session and being worked on at all. Pinned by
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
    `test_snapshot_names_the_root_of_an_explicitly_rooted_repository_not_the_ambient_one`.
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
        live outside any repository method: the first-discovery guard (`_discovery_guard`,
        `_discovery_guard_path`) and the reserved-slug probe inside it (#272's scope amendment).

        `SessionRepository` is deliberately backing-neutral and carries no `store()` of its own in its
        protocol — a Postgres backing has no filesystem root to hand back — so this reaches for one by
        duck typing rather than by widening the protocol, and falls back to the ambient default when
        there is none to reach for. That fallback is not a compromise unique to this method: it is the
        exact behaviour every caller of these two functions had *unconditionally*, before #272, since
        neither read a repository at all. A backing with a `store()` (today, only `FileSessionRepository`)
        gets addressed correctly; anything else gets what it already had."""
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
                    usage: dict | None = None) -> dict:
        """The provenance for a revision: what the provider says about itself, which of our surfaces
        asked for it (the one thing the provider cannot know), and — when the caller has it — what
        the call(s) behind this apply actually spent (`_usage_since`, #292)."""
        prov = {**self._need_provider().provenance(op, only=cards), "surface": surface}
        if usage:
            prov.update(usage)
        return prov

    # ── discovery ────────────────────────────────────────────────────────────────
    def create_only(self, request: str, *, cards: list[str] | None = None,
                    slug: str | None = None) -> str:
        """Persist a request as a session with no model yet — no LLM call. The 'Create session only'
        path: capture the request now, run discovery later."""
        return self.sessions.create_session(request, context_cards=cards, slug=slug).slug

    def claim_session(self, request: str, *, cards: list[str] | None, slug: str | None):
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
            provider=provider.name, model_name=provider.model_name())
        _require_revision_zero(meta.slug, meta.current_revision)
        return meta

    def finalize_discovery(self, request: str, out: EngineOutput, *, cards: list[str] | None = None,
                           slug: str | None = None, brief=None, surface: str = "discover",
                           usage: dict | None = None) -> str:
        """Create the session and apply a discovered model through the validated path. When a `brief` is
        given (a finalized discovery), its reasoning is absorbed into the model first. Shared by the
        CLI's interactive loop (which produced `out` itself) and `start()`.

        A first discovery lands on revision 0 and nothing else: creation is idempotent, so without
        that precondition a re-run silently replaces a model refined over several turns with a naive
        first-turn one. A `revision_conflict` is recoverable; a silent replacement is not. Pinned by
        `test_both_discover_entry_points_refuse_a_refined_session_before_paying`.

        `usage` is threaded through rather than computed here — this method makes no provider call of
        its own, so it has no `before` index to measure from. A caller that passes none produces a
        revision with no usage provenance: absent rather than wrong, per invariant 6."""
        meta = self.claim_session(request, cards=cards, slug=slug)
        if brief is not None:
            absorb_reasoning(out, brief)
        self.sessions.update_model(
            meta.slug, out.model_dump_json(), expected_revision=0,
            provenance=self._provenance("analyze", cards=cards, surface=surface, usage=usage))
        return meta.slug

    def start(self, request: str, *, cards: list[str] | None = None, slug: str | None = None,
              finalize: bool = False, surface: str = "discover") -> str:
        """Run one discovery turn on a fresh request and apply it, returning the session slug. With
        `finalize`, also produce and absorb the solution assessment's reasoning.

        The session is claimed *before* the provider is called. Creation is idempotent, so re-running
        a discovery whose session already carries a model is refused — and refusing it after the call
        means having paid for reasoning (twice, when finalizing) that can only be thrown away. The
        check is cheap and the call is not.

        **And claiming is not the only race (#209).** Two callers of this same request — a second
        browser tab, a refresh-and-resubmit — both idempotently reuse the session `claim_session`
        returns and both pass its revision-zero check before either has paid for anything. The
        `_discovery_guard` below is what actually decides which of them proceeds: the loser is
        refused immediately, before it ever reaches the provider.

        **And the guard alone is not quite the guarantee either (found in review) — the revision is
        re-checked fresh, immediately after winning it, before the provider is called.** A caller
        whose own `claim_session` genuinely read revision 0, but whose own guard-acquire attempt is
        merely delayed past the point a winner has already finished *and released* the guard, would
        otherwise walk in on a stale belief and pay for a call it was always going to lose at
        `finalize_discovery`'s own `expected_revision=0`. Re-reading here, before spending anything,
        closes that window the same way `_require_revision_zero` above closes the wide-open one.

        **`finalize` used to reason both calls before writing either (#467).** `analyze()` and the
        brief's own `generate()` both ran, and only then did the one `finalize_discovery` write land
        -- so a refused or failed brief call (a transport error, or #427's spend ceiling reached by
        the first call alone) discarded the already-billed `analyze()` result every time, with the
        session left at revision 0 as though nothing had been paid for. This mirrors #202's own fix
        for the CLI's interactive loop, in this same file: `finalize_discovery` runs immediately after
        `analyze()`, landing revision 1 before the brief is even attempted, and the brief is folded in
        through the ordinary `generate(slug, "brief")` path -- the same one every other caller of a
        brief takes, with its own spend check, its own revision-conflict handling and its own artifact
        save. A stop or a failure there leaves revision 1 standing, discovery applied, brief
        retryable with `generate(slug, "brief")` -- never a total loss of the `analyze()` spend.
        Pinned by `test_a_failed_brief_leaves_the_analyzed_discovery_applied_467`."""
        provider = self._need_provider()
        meta = self.claim_session(request, cards=cards, slug=slug)
        with _discovery_guard(meta.slug, self._store_for_repo()):
            _require_revision_zero(meta.slug, self.sessions.repo.read_meta(meta.slug).current_revision)
            ledger = current_ledger()
            before = len(ledger.calls) if ledger is not None else 0
            self._check_spend()
            with self._provider_call("analyze"):
                out = provider.analyze(request, only=cards)
            slug_out = self.finalize_discovery(request, out, cards=cards, slug=meta.slug,
                                               surface=surface, usage=_usage_since(before))
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
                   answers: str | None = None, cards: list[str] | None = None) -> EngineOutput:
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
                request, current_model=current_model, answers=answers, only=cards, reuse_system=True)

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
                out = self._need_provider().analyze(snap.request, only=snap.context_cards)
            return self.sessions.update_model(
                slug, out.model_dump_json(), expected_revision=snap.revision,
                provenance=self._provenance("analyze", cards=snap.context_cards, surface=surface,
                                            usage=_usage_since(before)))

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
                snap.request, current_model=model, answers=answers, only=snap.context_cards)
        return self.sessions.update_model(
            slug, out.model_dump_json(),
            expected_revision=expected_revision if expected_revision is not None else snap.revision,
            provenance=self._provenance("analyze", cards=snap.context_cards, surface=surface,
                                        usage=_usage_since(before)))

    # ── generation ───────────────────────────────────────────────────────────────
    def reason(self, slug: str, artifact_type: str, **kwargs):
        """Produce an artifact's typed contract without saving anything — for the terminal-only views
        (`stories`, `estimate`) that are analyses rather than deliverables. Still goes through the
        provider seam, so no interface reaches past it to a vendor's functions. Nothing is written, so
        there is no provenance to get wrong — but the model and the cards it is read against still come
        from one snapshot, so the analysis is of a session state that actually existed.

        `**kwargs` is what an analysis needs beyond the model: `estimate` is read against the
        `stories` a previous call produced. Until #77 that one call was made by `cli.py` directly, on
        a second client of its own, which is exactly the "no interface reaches past it" claim above
        being false one line below where it was written."""
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
        model = _require_a_model(snap.slug, snap)
        self._check_spend()
        with self._provider_call(artifact_type):
            return self._need_provider().generate(artifact_type, model, only=snap.context_cards,
                                                  **kwargs)

    # `generate()`'s public signature is these six overloads, not the implementation below. Five are
    # `Literal`-keyed so a call site written with a literal string gets that type's contract back;
    # the sixth takes a plain `str` for a caller holding the name in a variable (a route parameter,
    # e.g. `web/routes/artifacts.py`'s `generate_artifact`). `decision: typed-generation-seam`
    @overload
    def generate(self, slug: str, artifact_type: Literal["brief"], *, surface: str = "generate",
                **kwargs) -> Generated[Brief]: ...
    @overload
    def generate(self, slug: str, artifact_type: Literal["prd"], *, surface: str = "generate",
                **kwargs) -> Generated[PRD]: ...
    @overload
    def generate(self, slug: str, artifact_type: Literal["criteria"], *, surface: str = "generate",
                **kwargs) -> Generated[AcceptanceCriteria]: ...
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

        `brief` (the solution assessment) is the one with an extra step: its reasoning is absorbed back
        into the model as a revision, so downstream artifacts inherit the decisions and challenges, not
        just the facts.

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
        source_revision, cards = snap.revision, snap.context_cards
        out = _require_a_model(slug, snap)
        provider = self._need_provider()

        if artifact_type == "brief":
            ledger = current_ledger()
            before = len(ledger.calls) if ledger is not None else 0
            self._check_spend()
            with self._provider_call("brief"):
                brief = provider.generate("brief", out, only=cards)
            absorb_reasoning(out, brief)
            usage = _usage_since(before)
            # `out` is the revision-N model plus the reasoning just derived from it. Applying it without
            # the precondition would discard any revision that landed while the provider was reasoning.
            try:
                applied = self.sessions.update_model(
                    slug, out.model_dump_json(), expected_revision=source_revision,
                    provenance=self._provenance("brief", cards=cards, surface=surface, usage=usage))
            except RevisionConflictError as e:
                # The paid assessment is not thrown away merely because the apply lost the race:
                # filing it against an older source revision, flagged stale, is legal by invariant 2.
                # What genuinely did not happen is the reasoning's absorption, so both facts and the
                # remedy go in one message — a caller reading only `.message` gets the whole story
                # with no special-casing. Pinned by
                # `test_a_brief_lost_to_a_revision_conflict_is_still_saved_stale_not_discarded`.
                try:
                    status = self._save_generated(slug, "brief", brief_markdown(out, brief), source_revision)
                except ArtifactWriteFailedError as write_err:
                    # Two failures at once (found in review): the apply lost the race AND the
                    # fallback save that was meant to preserve the paid content also failed at the
                    # filesystem. Letting `write_err` propagate bare would silently drop the revision
                    # conflict it happened alongside -- a caller reading only `.message` would see an
                    # ordinary write failure and have no way to tell it apart from one that also lost
                    # a race, which is precisely the "state both facts, not just one" argument this
                    # branch exists for, one exception class over. Chained from the write failure,
                    # not re-raised as the conflict: the write failure is the more urgent, unresolved
                    # one -- this time the content really is lost, not merely unabsorbed.
                    raise ArtifactWriteFailedError(
                        f"{write_err.message} This session also lost a revision race in the same "
                        f"call: {e.message}. The brief's reasoning was NOT absorbed into the model "
                        "either way.",
                        details={**write_err.details, "revision_conflict": True,
                                 "revision_conflict_message": e.message}) from e
                raise RevisionConflictError(
                    f"{e.message}. The decision brief was still generated and saved against revision "
                    f"{source_revision} (now flagged stale); its reasoning was NOT absorbed into the "
                    f"model. `requivo brief {slug}` (or the Web's Regenerate) will refresh both.",
                    details={**e.details, "artifact_saved": True, "artifact_type": "brief",
                             "artifact_stale": status.stale}) from e
            # The assessment renders exactly the model that apply just wrote, so it belongs to that revision.
            status = self._save_generated(slug, "brief", brief_markdown(out, brief), applied.revision)
            return Generated(status=status, artifact=brief, model=out)

        try:
            writer = _WRITERS[artifact_type]
        except KeyError as e:
            raise ValueError(f"{artifact_type!r} has no saveable document — use `reason()`") from e
        self._check_spend()
        with self._provider_call(artifact_type):
            artifact = provider.generate(artifact_type, out, only=cards, **kwargs)
        status = self._save_generated(slug, artifact_type, writer(artifact), source_revision)
        return Generated(status=status, artifact=artifact, model=out)

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
