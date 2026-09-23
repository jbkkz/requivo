"""DiscoveryService: the provider-backed orchestration every interface shares.

It holds a `ReasoningProvider` plus the session/artifact services, talks to the provider through the
protocol only, and never touches the filesystem directly: every write goes through `SessionService`
and `ArtifactService`. A second provider is a constructor argument; extracting the vendor-neutral
assembly out of `providers/anthropic` is deferred (`decision: deferring-the-neutral-provider-layer`).
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
    """The estimate's writer over the `(draft, soft, confidence)` triple, so the registry stays one-argument."""
    draft, soft, confidence = estimate
    return estimate_markdown(draft, soft, confidence)


# artifact type → the writer that turns its contract into the saved Markdown, in the order a user meets
# them (`stories`/`estimate` since #519, `decision: the-estimate-graduates`).
# The annotation is load-bearing: without it pyright infers a union no argument satisfies. `decision: typed-generation-seam`
_WRITERS: dict[str, Callable[[Any], str]] = {
    "prd": prd_markdown,
    "stories": stories_markdown,
    "criteria": criteria_markdown,
    "estimate": _estimate_document,
    "epic": epic_markdown,
    "release": release_markdown,
}

# Everything `generate()` can produce, the one source every interface asks. `gtm_plan` (#609) is not
# in `_WRITERS`: it takes the reasoning-absorbing path `brief` does (`_ASSESSMENT_ARTIFACTS`).
GENERATABLE: tuple[str, ...] = ("brief", "gtm_plan", *_WRITERS)

_A = TypeVar("_A")


@dataclass
class SavedEstimate:
    """`generate(slug, "estimate")`'s artifact: the estimate as `reason_from` returns it, plus the
    stories it was reasoned against, saved from the same snapshot against the same revision (#519)."""

    draft: EstimateDraft
    soft: list[str]
    confidence: str
    stories: Stories
    stories_status: ArtifactStatus


@dataclass
class Generated(Generic[_A]):
    """What one generation produced: the saved `status`, the typed `artifact` (so a caller renders
    its own view without a second call) and the `model` it was rendered from (post-absorption for
    the assessment). The type parameter is resolved by `generate()`'s overloads. `decision: typed-generation-seam`"""

    status: ArtifactStatus
    artifact: _A
    model: EngineOutput


def _require_revision_zero(slug: str, revision: int) -> None:
    """A first discovery may only land on a session with no model yet: discovery *replaces* the
    model, and the optimistic lock cannot catch a naive first turn written over a refined one."""
    if revision > 0:
        raise RevisionConflictError(
            f"session '{slug}' already carries a model (revision {revision}) — a fresh discovery "
            f'would replace it. Refine it instead (`requivo answer {slug} "…"`), or run this '
            "discovery under another slug.",
            details={"slug": slug, "expected": 0, "actual": revision})



def _require_no_conflict_yet(slug: str, expected_revision: int | None, snap: SessionSnapshot) -> None:
    """Refuse a precondition that is already stale against the snapshot before the paid call, not
    after it (#205); `None` states no expectation. Does not replace `expected_revision` on the apply.
    `test_a_stale_answers_form_is_refused_before_the_provider_is_paid` (the assertion is the call count)."""
    if expected_revision is not None and expected_revision != snap.revision:
        raise RevisionConflictError(
            f"session '{slug}' is at revision {snap.revision}, not the expected "
            f"{expected_revision} — reload the page and re-submit your answers",
            details={"slug": slug, "expected": expected_revision, "actual": snap.revision})


def _require_owned_artifact_type(perimeter: str, artifact_type: str) -> None:
    """Refuse an artifact type the session's perimeter does not produce (#608), as a structured
    `ArtifactTypeNotOwnedError` (409) rather than a bare `ValueError` no handler catches."""
    owned = get_perimeter(perimeter).artifact_types
    if artifact_type not in owned:
        raise ArtifactTypeNotOwnedError(
            f"{artifact_type!r} is not produced by the {perimeter!r} perimeter this session runs "
            f"under -- it can produce: {sorted(owned) or '(none yet)'}",
            details={"artifact_type": artifact_type, "perimeter": perimeter, "owned": sorted(owned)})


def _require_a_model(slug: str, snap: SessionSnapshot) -> EngineOutput:
    """Generation needs a model; returns it so the narrowing is in the type too.
    `test_generating_from_a_session_with_no_model_is_refused_before_the_provider`."""
    if snap.model is None:
        raise RevisionConflictError(
            f"session '{slug}' has no model yet (revision 0) — there is nothing to generate from. "
            "Run `requivo discover` on it.",
            details={"slug": slug, "expected": 1, "actual": snap.revision})
    return snap.model


def absorb_reasoning(out: EngineOutput, brief) -> None:
    """Persist the assessment's reasoning into the model so every generator inherits it; the single
    definition, shared by the CLI and the Web."""
    out.decisions = brief.decisions
    out.challenges = brief.challenges
    out.opportunities = brief.opportunities
    # Exclusions (#600) and thresholds (#604) land in `model.json` like their siblings:
    # test_a_generated_briefs_reasoning_items_are_absorbed_into_the_persisted_model.
    out.exclusions = brief.exclusions
    out.thresholds = brief.thresholds


def absorb_gtm_reasoning(out: EngineOutput, brief: GoToMarketPlan) -> None:
    """`absorb_reasoning` for the go-to-market plan (#609): only `exclusions` and `thresholds`."""
    out.exclusions = brief.exclusions
    out.thresholds = brief.thresholds


@dataclass(frozen=True)
class _AssessmentArtifact:
    """An artifact type whose generation absorbs reasoning into the model before saving (`brief`,
    `gtm_plan`); `cli_verb`/`label` feed the revision-conflict messages in `generate()`."""
    writer: Callable[[EngineOutput, Any], str]
    absorb: Callable[[EngineOutput, Any], None]
    cli_verb: str
    label: str


# The artifact types that take the reasoning-absorbing path, keyed like every registry here (#609).
_ASSESSMENT_ARTIFACTS: dict[str, _AssessmentArtifact] = {
    "brief": _AssessmentArtifact(brief_markdown, absorb_reasoning, "brief", "decision brief"),
    "gtm_plan": _AssessmentArtifact(gtm_plan_markdown, absorb_gtm_reasoning, "gtm_plan",
                                     "go-to-market plan"),
}


def _discovery_guard_path(slug: str, store: Store) -> Path:
    """The in-flight first-discovery guard file, `<workspace>/.requivo/locks/<slug>.discovering`.

    Distinct from `Store.lock_path`, which is released before a provider call starts (#209):
    `test_a_concurrent_first_discovery_is_refused_before_any_provider_call`. `store` is the caller's own
    repository, never the ambient default (#272). Validated as `lock_path` is (#390):
    `test_a_reserved_slug_the_sweep_one_commit_later_missed_reaches_the_discovery_guard`."""
    root = store.lock_root()
    slug = _slug_shape(slug)
    # Checked against the *session* root: what decides the reserved-name refusal is whether a session
    # already claims this name (#221).
    _refuse_new_reserved_slug(slug, store.session_root() / slug)
    p = root / (slug + ".discovering")
    if not is_contained(p, root):
        raise InvalidSlugError(f"slug {slug!r} does not resolve to a lock file inside {root}",
                               details={"slug": slug})
    return p


@contextmanager
def _discovery_guard(slug: str, store: Store) -> Iterator[None]:
    """Refuse a second, concurrent first-discovery on `slug` before it can pay. Non-blocking, not
    re-entrant, held for one paid call plus its write; addresses the repository's own root (#272).
    `test_a_concurrent_first_discovery_is_refused_before_any_provider_call`,
    `test_the_discovery_guard_addresses_an_explicitly_rooted_repositorys_own_workspace`."""
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
    """The token/rate provenance for the provider calls since `before`, as the extra `RevisionRecord`
    fields. `{}` (never zero-filled) when there is no ledger or no call in the span (invariant 6).
    `test_a_provider_backed_apply_stamps_token_and_rate_provenance_onto_its_revision`."""
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
    """Is this session grounded in a card that knows its domain? `judgment is None` means nobody
    looked (`why_not` says why), which must never render as `ContextDecision.none` (#492)."""

    judgment: ContextJudgment | None
    why_not: str


class Routing(NamedTuple):
    """Which installed perimeter does this request belong to (#601)? `Grounding`'s shape: `judgment
    is None` means nobody looked, never a `none` verdict."""

    judgment: PerimeterJudgment | None
    why_not: str


class Reclaim(NamedTuple):
    """What `_reclaim_under` did, as three independent facts (#601): `meta` is where the claim is now;
    `landed` is whether the identity moved to what was asked (False only when the delete was refused);
    `created` is whether *this call* created `meta`'s session, the one fact a later delete may read.
    Never infer one from another."""

    meta: Any
    landed: bool
    created: bool


class ClaimAndGround(NamedTuple):
    """What `claim_and_ground` produced (#601). Read it by attribute, not by position: the shape
    already grew once and `docs/compatibility.md` declares it."""

    meta: Any
    grounding: Grounding
    cards: list[str] | None
    routing: Routing


class DiscoveryService:
    """Provider-backed orchestration over the session/artifact services. The provider is built
    lazily, so only the operations that reason need a key; inject a `ReasoningProvider` to swap it."""

    def __init__(self, provider=None, *, client=None, sessions: SessionService | None = None,
                 artifacts: ArtifactService | None = None, repo=None,
                 spend_policy: SpendPolicy | None = None):
        self._provider = provider
        self._client = client
        self._spend_policy = spend_policy
        self.sessions = sessions or SessionService(repo)
        # Defaults to *this service's* storage, never the process default: one repository per service
        # is the only shape that cannot split sessions and artifacts across backings.
        self.artifacts = artifacts or ArtifactService(self.sessions.repo)

    def _store_for_repo(self) -> Store:
        """The `Store` behind `self.sessions.repo` for the two ambient reads outside any repository
        method; duck-typed on `repo.store()`, ambient only when there is none (#272)."""
        get_store = getattr(self.sessions.repo, "store", None)
        return cast(Store, get_store()) if callable(get_store) else Store(workspace_root())

    def _need_provider(self):
        """The provider, built on first use; the default implementation is imported here, not at module scope."""
        if self._provider is None:
            from requivo.providers.anthropic import AnthropicProvider
            self._provider = AnthropicProvider(self._client)
        return self._provider

    def _check_spend(self) -> None:
        """Consult the injected `SpendPolicy` immediately before each provider call (#427), at every
        call site so an operation's second call is refused too. No policy is a no-op."""
        if self._spend_policy is not None:
            self._spend_policy.check(current_ledger())

    @contextmanager
    def _provider_call(self, operation: str) -> Iterator[None]:
        """Wrap a provider call with DEBUG/INFO/WARNING logging and its wall-clock duration; the
        exception is re-raised unchanged, and nothing prints unless a handler is attached (invariant 7).
        `test_a_successful_provider_call_logs_started_and_finished`."""
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
        """A revision's provenance: the provider's own, the surface that asked, and the spend (`_usage_since`, #292)."""
        prov = {**self._need_provider().provenance(op, only=cards, perimeter=perimeter),
               "surface": surface}
        if usage:
            prov.update(usage)
        return prov

    # ── discovery ────────────────────────────────────────────────────────────────
    def create_only(self, request: str, *, cards: list[str] | None = None,
                    slug: str | None = None, perimeter: str = DEFAULT_PERIMETER) -> str:
        """Persist a request as a session with no model yet, no LLM call. `perimeter` (#608) is frozen here."""
        return self.sessions.create_session(request, context_cards=cards, slug=slug,
                                            perimeter=perimeter).slug

    def judge_grounding(self, request: str, *, cards: list[str] | None) -> Grounding:
        """Ask whether any installed context card describes this request's domain. Report-only
        (`decision: the-engine-writes-the-missing-card`). Returns rather than raises, in three states:
        not asked because the user chose, not asked because the provider cannot, or the verdict.
        `test_a_provider_that_cannot_judge_reports_not_asked_rather_than_no_card_needed`."""
        if cards:
            return Grounding(None, "the cards for this session were chosen with --context")
        provider = self._need_provider()
        if not isinstance(provider, ContextJudge):
            return Grounding(None, f"the {getattr(provider, 'name', 'current')} provider does not "
                                   f"answer grounding questions")
        summaries = card_summaries()
        if not summaries:
            # `load_context` refuses this install a moment later; "no card is needed" would be wrong.
            # `test_an_install_with_no_cards_is_not_judged_as_needing_none`.
            return Grounding(None, "this install has no context cards to judge against")
        return Grounding(provider.judge_context(request, cards=summaries), "")

    def route_perimeter(self, request: str, *, perimeter: str | None) -> Routing:
        """Ask which installed perimeter this request's shape belongs to (#601): `judge_grounding`'s
        three-state honesty, plus "not asked, one perimeter installed". A second standalone call,
        deliberately (`decision: two-judgment-calls-not-one`).
        `test_a_provider_that_cannot_route_reports_not_asked`, `test_a_single_installed_perimeter_is_not_judged`."""
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
        """The fourth of #593's preconditions for deleting an empty first-discovery claim, re-read
        fresh under the lock (invariant 9); shared with #601's router. Returns whether it deleted."""
        if not created:
            return False
        with self.sessions.repo.lock(meta.slug):
            if self.sessions.repo.read_meta(meta.slug).current_revision != 0:
                return False
            self.sessions.delete_session(meta.slug)
        return True

    def _reclaim_under(self, meta, *, request: str, slug: str | None, cards: list[str] | None,
                       perimeter: str, created: bool, provider) -> Reclaim:
        """Delete the empty session `meta` names and recreate it under a narrower identity, the one
        implementation of the destructive step (invariant 14). `landed` is False only when the delete
        was refused; `created` is `create_session_report`'s own boolean, never asserted.
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
        """Claim the session, route it to its perimeter (#601), judge its grounding (#593), and act
        on each verdict when acting is safe: one implementation of the destructive step (invariant 14).

        Order: resolve an existing session under every installed perimeter first, free; claim under
        the resolved perimeter (invariant 13's gate ahead of every paid call); route, re-claiming only
        on a `fits` verdict, refusing an `ambiguous` one before any model is reasoned; then judge
        grounding and re-claim under narrowed cards. Returns a `ClaimAndGround`. Pinned by
        `test_a_fitting_perimeter_verdict_reroutes_and_reclaims`,
        `test_an_ambiguous_verdict_refuses_before_any_model_is_reasoned`,
        `test_a_session_this_call_did_not_create_is_never_deleted_by_a_verdict` and
        `test_claim_and_ground_resolves_an_existing_non_default_perimeter_session_before_routing`."""
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
            # The routing call did not complete: the guessed claim is deleted, or a repeat would read
            # the guess as a decision and never route again (#601).
            # `test_a_failed_routing_call_does_not_lock_the_retry_into_the_default_perimeter`.
            self._delete_if_safe(meta, created=created)
            raise
        route_judgment = routing.judgment
        if route_judgment is not None:
            if route_judgment.decision is PerimeterDecision.ambiguous:
                self._delete_if_safe(meta, created=created)
                # `reason` is LLM-authored prose: escaped for the message, raw in `details` (#601).
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
                # Read off `reclaim.meta` in both arms: it is where the claim is now, landed or not;
                # `created`, never `landed`, is the ownership fact the next reclaim reads (#601).
                meta, created = reclaim.meta, reclaim.created
                claim_perimeter = resolve_perimeter(meta.perimeter)
                if not reclaim.landed:
                    # The route could not land; the verdict must say so rather than announce it.
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
        # `reclaim.meta.context_cards` is what the session records, landed or not; inferring the cards
        # from `created` mis-reported an idempotent re-entry (#601).
        return ClaimAndGround(reclaim.meta, grounding, reclaim.meta.context_cards, routing)

    def claim_session(self, request: str, *, cards: list[str] | None, slug: str | None,
                      perimeter: str = DEFAULT_PERIMETER):
        """Create (or reuse) the session a first discovery will land on, and hold it to revision 0:
        the single gate every entry point takes before paying (#133). Public so a surface that owns
        its own loop can take it. `test_both_discover_entry_points_refuse_a_refined_session_before_paying`."""
        provider = self._need_provider()
        meta = self.sessions.create_session(
            request, context_cards=cards, slug=slug,
            provider=provider.name, model_name=provider.model_name(), perimeter=perimeter)
        _require_revision_zero(meta.slug, meta.current_revision)
        return meta

    def finalize_discovery(self, request: str, out: EngineOutput, *, cards: list[str] | None = None,
                           slug: str | None = None, brief=None, surface: str = "discover",
                           usage: dict | None = None, perimeter: str = DEFAULT_PERIMETER) -> str:
        """Create the session and apply a discovered model through the validated path, absorbing a
        `brief`'s reasoning first when given. Held to revision 0 (a re-run must not replace a refined
        model). `perimeter` must be the one `out` was reasoned against; `usage` is threaded through,
        absent rather than wrong (invariant 6)."""
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
        """Run one discovery turn on a fresh request and apply it, returning the slug; with
        `finalize`, also generate the brief. Claimed before the call, and `_discovery_guard` decides
        between two concurrent callers with the revision re-checked inside it (#209). The discovery
        lands as revision 1 before the brief is attempted (#467).
        `test_a_late_caller_of_start_with_a_stale_outer_check_still_pays_nothing`,
        `test_a_failed_brief_leaves_the_analyzed_discovery_applied`."""
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
    # A surface owns the *loop* and never a client; the service is handed state and returns a result,
    # and nothing here writes. `test_the_loop_reasons_through_the_service_and_carries_the_model_not_a_transcript`.

    def draft_turn(self, request: str, *, current_model: EngineOutput | None = None,
                   answers: str | None = None, cards: list[str] | None = None,
                   perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
        """One un-persisted discovery turn: the request alone first, then the model so far plus the
        answers. `reuse_system=True` because this is the one repeated operation
        (`test_the_loop_declares_its_repeated_prompt_at_the_seam`); the size cap runs here too, since
        the turn resends the request every call (`test_an_oversized_request_is_refused_before_any_provider_call`)."""
        require_input_within_bounds(request, field="request")
        if answers is not None:
            require_input_within_bounds(answers, field="answers")
        self._check_spend()
        with self._provider_call("analyze"):
            return self._need_provider().analyze(
                request, current_model=current_model, answers=answers, only=cards, reuse_system=True,
                perimeter=perimeter)

    def run_discovery(self, slug: str, *, surface: str = "discover") -> UpdateResult:
        """Run the first discovery turn on an already-created session and apply it as revision 1.
        Held to revision 0 before the call; `_discovery_guard` serialises concurrent callers, and the
        snapshot is re-taken inside it. `test_a_late_caller_with_a_stale_outer_check_still_pays_nothing`."""
        self.sessions.ensure_canonical(slug)
        snap = self.sessions.snapshot(slug)
        _require_revision_zero(slug, snap.revision)
        with _discovery_guard(slug, self._store_for_repo()):
            # Fresh, not the snapshot above: the guard is what serialises, the outer check only fast-fails.
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
        """Fold the user's answers into the model as a new revision, from one snapshot; the
        precondition defaults to the revision this turn read. Order of the gates: the size cap
        (#255), then `_require_a_model` (#421, `test_answer_refuses_a_session_that_has_no_model_yet`),
        then a stale caller precondition (#205), all before the provider is built."""
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
        """An artifact's typed contract with no write, through the provider seam and from one
        snapshot. `**kwargs` is what an analysis needs beyond the model (`estimate` reads `stories`)."""
        return self.reason_from(self.sessions.snapshot(slug), artifact_type, **kwargs)

    def reason_from(self, snap: SessionSnapshot, artifact_type: str, **kwargs):
        """The same analysis from a snapshot the caller holds, so a two-call analysis reads one
        revision (#135). `test_the_estimate_verb_reads_stories_and_estimate_from_one_snapshot`."""
        _require_owned_artifact_type(snap.perimeter, artifact_type)
        model = _require_a_model(snap.slug, snap)
        self._check_spend()
        with self._provider_call(artifact_type):
            return self._need_provider().generate(artifact_type, model, only=snap.context_cards,
                                                  **kwargs)

    # `generate()`'s public signature is these overloads: `Literal`-keyed per type, plus a plain `str`
    # for a name held in a variable. `decision: typed-generation-seam`. `estimate`'s `on_stories` is
    # the caller's hook, not forwarded to the provider (`_generate_estimate`).
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
        """Generate an artifact through the provider and save it with its source revision; every
        interface goes through here. `brief`/`gtm_plan` absorb their reasoning into the model first
        (`_ASSESSMENT_ARTIFACTS`); `estimate` is two calls (`_generate_estimate`).

        Generation is not atomic: the revision is read from one `SessionSnapshot` before the call and
        carried as the apply's precondition and the artifact's `source_revision` (invariants 2, 12)."""
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
            # Without the precondition, a revision that landed during the call would be discarded.
            try:
                applied = self.sessions.update_model(
                    slug, out.model_dump_json(), expected_revision=source_revision,
                    provenance=self._provenance(artifact_type, cards=cards, surface=surface,
                                                usage=usage, perimeter=snap.perimeter))
            except RevisionConflictError as e:
                # The paid assessment is filed stale against its source revision rather than thrown away
                # (invariant 2). `test_a_brief_lost_to_a_revision_conflict_is_still_saved_stale_not_discarded`.
                try:
                    status = self._save_generated(
                        slug, artifact_type, spec.writer(out, brief), source_revision)
                except ArtifactWriteFailedError as write_err:
                    # Both failures stated, chained from the write failure, the unresolved one.
                    # `test_a_conflict_plus_a_secondary_write_failure_states_both_not_just_one`.
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
        """The one two-call generation (#519): the stories are reasoned from the same snapshot, saved
        against the same revision before the estimate is paid for and before `on_stories` renders
        them, so the estimate's basis is fully recorded (invariant 6). No other keyword is accepted,
        `stories` in particular. `test_generating_the_estimate_saves_the_stories_it_was_reasoned_against_from_one_snapshot`,
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
        """Save a generated artifact against the revision it was produced from; the staleness check
        is `ArtifactService.save`'s. A filesystem failure is an `ArtifactWriteFailedError` naming what
        was lost. `test_an_oserror_writing_a_generated_artifact_is_a_structured_refusal_not_a_traceback`."""
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
