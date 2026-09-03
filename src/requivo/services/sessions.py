"""SessionService — create sessions and apply model updates through one validated pipeline.

`update_model` is the single write path for the model, whatever produced the proposal (the Anthropic
provider, a Claude Code proposal file, Requivo Web): validate → diff against the current
model → propagate the blast radius → save a new revision → flag the artifacts that went stale →
compute readiness. It returns a structured `UpdateResult` so any caller can render it or emit `--json`.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from requivo.core import persistence as store
from requivo.core.analysis import model_status, readiness_blockers
from requivo.core.context import resolve_cards
from requivo.core.contracts import EngineOutput
from requivo.core.dependencies import (
    ARTIFACT_FILES,
    REASONING_CONSUMERS,
    ReasoningDiff,
    diff_models,
    diff_reasoning,
    propagate,
)
from requivo.core.errors import RevisionConflictError, SessionExistsError, SessionNotFoundError
from requivo.core.persistence import SessionMeta, Store
from requivo.core.selectors import display_token
from requivo.core.validation import require_input_within_bounds, validate_proposal
from requivo.paths import workspace_root
from requivo.services.repository import SessionRepository, default_repository

logger = logging.getLogger(__name__)


@dataclass
class Readiness:
    ready: bool
    blocking_slots: list[str]  # slot ids, schema order

    def to_dict(self) -> dict:
        return {"ready": self.ready, "blocking_slots": self.blocking_slots}


@dataclass(frozen=True)
class SessionSnapshot:
    """One consistent read of a session: its revision, the model *at* that revision, and the inputs a
    provider call needs. Taken under the session lock, so the parts cannot disagree.

    Reading the revision and the model as two separate calls looks harmless and is not: a write
    landing between them yields revision N with the model of N+1. The generation then reasons from the
    newer model and files the artifact as coming from the older revision — content and provenance
    describing different sources, which is precisely the claim the product cannot afford to get wrong.
    Worse, it is undetectable afterwards: the number is plausible.

    The lock is released before the provider call. It is not there to make the whole operation atomic —
    it cannot be, the call takes minutes — but to make the *basis* coherent. `expected_revision` on the
    write is what handles the session moving afterwards."""

    slug: str
    revision: int
    model: EngineOutput | None          # None before the first model (revision 0)
    request: str
    context_cards: list[str] | None     # None == every card


@dataclass(frozen=True)
class SessionEntry:
    """One slug in the store, with either its metadata or the reason it could not be read (#7).

    The third state, made representable: without it an aggregate can only raise for the set (one bad
    session hides every good one) or drop the member (the reader is told nothing is wrong), and both
    were live. `error` is the exception's own text rather than a code, because the remedy is the part
    worth keeping. `test_one_unreadable_session_no_longer_takes_the_listing_down` and
    `test_the_degraded_row_carries_the_reason_because_the_reason_is_the_remedy`."""

    slug: str
    meta: SessionMeta | None = None
    error: str | None = None

    @property
    def readable(self) -> bool:
        """True when the metadata loaded. Named rather than left as `meta is not None` so a caller
        reads the question it is asking instead of the representation of the answer."""
        return self.meta is not None


@dataclass(frozen=True)
class RescopeResult:
    """The structured outcome of `session rescope` — the payload of `session rescope [--json]`."""
    slug: str
    previous_context_cards: list[str] | None
    context_cards: list[str] | None
    revision: int
    changed: bool                                  # False when the selection did not move

    def to_dict(self) -> dict:
        return {"slug": self.slug, "previous_context_cards": self.previous_context_cards,
                "context_cards": self.context_cards, "revision": self.revision, "changed": self.changed}


@dataclass
class UpdateResult:
    """The structured outcome of applying a proposal — the payload of `model apply [--json]`."""
    status: str                                   # "applied"
    revision: int
    changed_slots: list[str]                      # slot ids that materially moved
    invalidated_decisions: list[str] = field(default_factory=list)  # decision text needing re-validation
    invalidated_challenges: list[str] = field(default_factory=list)  # challenge headlines now in question
    stale_artifacts: list[str] = field(default_factory=list)        # artifact types now out of date
    readiness: Readiness = field(default_factory=lambda: Readiness(False, []))
    # What moved in the reasoning layer — ids, per collection. Reported separately from
    # `changed_slots` because they answer different questions: the slots say the *facts* moved, these
    # say the *judgment over them* moved. Either can invalidate an artifact on its own.
    changed_decisions: list[str] = field(default_factory=list)
    changed_challenges: list[str] = field(default_factory=list)
    changed_opportunities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "revision": self.revision,
            "changed_slots": self.changed_slots,
            "changed_decisions": self.changed_decisions,
            "changed_challenges": self.changed_challenges,
            "changed_opportunities": self.changed_opportunities,
            "invalidated_decisions": self.invalidated_decisions,
            "invalidated_challenges": self.invalidated_challenges,
            "stale_artifacts": self.stale_artifacts,
            "readiness": self.readiness.to_dict(),
        }


def _readiness(model: EngineOutput) -> Readiness:
    blockers = readiness_blockers(model)
    return Readiness(ready=not blockers, blocking_slots=blockers)


class SessionService:
    """Create, resolve, load, and mutate sessions through one validated pipeline. Storage is injected
    as a `SessionRepository` (files by default, Postgres elsewhere), so this orchestration is reused
    verbatim across backings. Stateless beyond that handle — safe to construct per call (the CLI does)
    or hold as a singleton (Requivo Web does)."""

    def __init__(self, repo: SessionRepository | None = None):
        self.repo: SessionRepository = repo or default_repository()

    # ── resolution ────────────────────────────────────────────────────────────
    def resolve_slug(self, reference: str | Path, *, accept_path: bool = True) -> str:
        """Turn a user reference into a slug. Accepts a bare slug, a path to a session directory, or a
        path to a model.json — under either the canonical `.requivo/sessions/` or legacy `out/` root.

        `accept_path=False` refuses anything path-shaped outright, naming the reference exactly as
        given (#402): the eight generator verbs pass it, because they resolve a *slug* and then read
        and write the store's own copy, so a path was never a meaningful input for them --
        `test_resolve_slug_refuses_a_model_json_path_when_the_caller_opted_out`, with
        `test_resolve_slug_still_accepts_a_bare_slug_when_paths_are_refused` for the other half.
        "Path-shaped" is decided from the string alone, never from what happens to exist on disk at
        that name (invariant 17), so filesystem noise cannot refuse a bare slug as a path.

        Where paths are still accepted, **a reference is only mined for a name when a session really
        is there**: a `model.json`/`session.json` when the file exists (#402), a directory when it
        carries its own session marker (#414). Mining unconditionally resolved a reference that
        merely shared its final segment with an unrelated real session *to that session*, which is
        the wrong-cause class this refuses instead of producing.
        `test_resolve_slug_no_longer_mines_a_nonexistent_model_json_path`,
        `test_a_directory_reference_does_not_silently_use_an_unrelated_real_session` and
        `test_resolve_slug_refuses_a_directory_that_is_not_a_session`, with
        `test_resolve_slug_still_mines_a_real_saved_model_json` and
        `test_resolve_slug_still_mines_a_real_session_directory` for the must-fire half."""
        ref = str(reference)
        p = Path(ref)
        if not accept_path:
            looks_like_a_path = (
                p.name in ("model.json", "session.json")
                or os.sep in ref or (os.altsep and os.altsep in ref)
            )
            if looks_like_a_path:
                # `ref` is untrusted input reaching a message printed verbatim, so *every* mention
                # goes through `display_token` (invariant 14, #40), not only the first: a raw second
                # occurrence leaves a control character free to forge a line the refusal never wrote.
                # `test_the_path_refusal_cannot_forge_a_second_line_of_its_own_message`.
                safe_ref = display_token(ref)
                raise SessionNotFoundError(
                    f"{safe_ref} looks like a path, but this command takes a session slug -- it "
                    "resolves and writes back into a session, not the file itself, so a path is "
                    "not enough to tell it which one. Pass the session's slug (see `requivo "
                    f"session list`), or inspect the file directly with `requivo status {safe_ref}`.",
                    details={"ref": ref},
                )
            return ref
        if p.name in ("model.json", "session.json"):
            # `Path.is_file()` swallows ENOENT/ENOTDIR into `False` -- what "mine only a real file"
            # needs -- but re-raises `PermissionError`, so this gets the same third state `_probe`
            # exists for rather than a traceback escaping a verb that promises every clean failure
            # surfaces without one (#402) --
            # `test_an_unreadable_model_json_path_refuses_cleanly_instead_of_crashing`.
            try:
                is_real_file = p.is_file()
            except OSError as e:
                raise SessionNotFoundError(
                    f"could not tell whether {display_token(ref)} is a saved model.json: {e}",
                    details={"ref": ref},
                ) from e
            return p.parent.name if is_real_file else ref
        # A directory is mined for its own name only when it carries a session's own marker (#414) --
        # see the docstring for the wrong-cause class unconditional mining produced. Two probes, and
        # both re-raise: `p.exists()`/`p.is_dir()` stat `p` itself and fail when an ANCESTOR denies
        # traversal, a distinct case from the marker probe failing on the directory's own contents,
        # so guarding only the second left the entry gate able to raise a bare traceback for an
        # otherwise healthy directory (`test_a_directory_reference_under_a_blocked_ancestor_refuses_cleanly_too`,
        # `test_an_unreadable_session_directory_refuses_cleanly_instead_of_crashing`).
        try:
            is_dir = p.exists() and p.is_dir()
        except OSError as e:
            raise SessionNotFoundError(
                f"could not tell whether {display_token(ref)} is a session directory: {e}",
                details={"ref": ref},
            ) from e
        if is_dir:
            try:
                looks_like_a_session = (p / "session.json").exists() or (p / "model.json").exists()
            except OSError as e:
                raise SessionNotFoundError(
                    f"could not tell whether {display_token(ref)} is a session directory: {e}",
                    details={"ref": ref},
                ) from e
            if looks_like_a_session:
                return p.name
            raise SessionNotFoundError(
                f"{display_token(ref)} does not look like a session directory -- it has no "
                "session.json or model.json of its own, so it is not something this command "
                "can resolve a slug from. Pass the session's slug instead (see `requivo "
                "session list`).",
                details={"ref": ref},
            )
        return ref  # a bare slug

    @staticmethod
    def slug_hint(text: str) -> str:
        """Turn arbitrary text into a slug-shaped name — the surface's route to slug derivation.

        Not a repository method: deriving a name from a request is a naming policy, the same whatever
        backs the store. It is a seam because `cli.py` was reaching into `core.persistence` for the
        derivation itself (#76) — a surface holding a core implementation detail, and what keeps a
        surface out of the store is this seam rather than the underscore `derive_slug` used to carry
        (`test_the_surfaces_reach_the_store_only_through_the_named_filesystem_concerns`).

        Two callers, two inputs: `create_session` derives a slug from the request text, and
        `requivo discover <file>` derives a *hint* from a filename stem — passing a raw
        "Leave Approval v2.md" stem through turned an ordinary input file into an `invalid_slug`.
        """
        return store.derive_slug(text)

    def exists(self, slug: str) -> bool:
        """True if a usable session exists (the repository decides what backs it)."""
        return self.repo.exists(slug)

    def no_session(self, ref: str, *, what: str = "session",
                   details: dict | None = None) -> SessionNotFoundError:
        """The refusal for "there is no such session" — the surface's route to it (#243).

        The sentence names the sessions root, so it is read off *this service's own repository*, not
        the process ambient default: an explicitly-rooted `SessionService` naming the ambient
        workspace here is the silent disagreement #272 exists to close, with every other read on the
        service going to the right store (`test_no_session_names_the_root_of_an_explicitly_rooted_repository`
        and `test_no_session_still_names_the_ambient_root_for_the_default_repository`). It is an
        instance method for that reason alone.

        It is a seam because six sites in `cli.py` and `deterministic/sessions.py` raise this, and
        reaching into `core.persistence` for it would put a *copy* concern in an allowlist of
        justified **filesystem** concerns (#76) — a message is not a path, even when it contains one.

        `what` widens the noun for `_resolve_ref`, which accepts a path as well as a slug. `details`
        is explicit for the same caller: its published key is `ref` rather than `slug`, and `details`
        is a contract (`docs/compatibility.md`), so a rewording of the message must not move it.
        """
        message = self._store_for_error_text().no_session_message(ref, what=what)
        return SessionNotFoundError(message,
                                    details=details if details is not None else {"slug": ref})

    def _store_for_error_text(self) -> Store:
        """The `core.persistence.Store` this service's own repository addresses, for the one place
        outside any repository method that reads the workspace root: `no_session`'s error text (#272).
        Duck-typed against `self.repo.store()` rather than added to the `SessionRepository` protocol,
        for the reason `DiscoveryService._store_for_repo` gives — a Postgres backing has no
        filesystem root to hand back, and the fallback below is what this call had unconditionally
        before #272."""
        get_store = getattr(self.repo, "store", None)
        if callable(get_store):
            return cast(Store, get_store())
        return Store(workspace_root())

    def _ensure_canonical(self, slug: str) -> None:
        """Before any mutation, make sure the session is in the mutation-backed store — for a file
        backing this migrates a legacy `out/<slug>/` session in place on first write."""
        self.repo.ensure_writable(slug)

    # ── creation ──────────────────────────────────────────────────────────────
    def create_session(self, request: str, *, context_cards: list[str] | None = None,
                        slug: str | None = None, provider: str | None = None,
                        model_name: str | None = None) -> SessionMeta:
        """Create a fresh session from a request (no model yet). If `slug` is omitted it is derived
        from the request and made collision-safe against existing sessions.

        Creation is idempotent on *identity*, and identity is the request **and its context cards**
        (invariant 11): the same request read against different cards gets different impact
        estimates, so keying on the request alone silently returned the first session with cards the
        caller never asked for — `test_the_same_request_under_different_cards_is_a_different_session`.
        The claim on a slug is `repo.create` itself, which is atomic; a check-then-create here would
        let two concurrent callers both decide the session was theirs to make.

        The card selection and the request's size (#255) are both checked here rather than trusted,
        because the interfaces being careful is not an integrity boundary and an external consumer
        calls exactly this layer (invariant 14). An unknown card is not inert — an empty resolved
        selection means *every* card, so a bad name widens the context instead of narrowing it.
        `test_the_service_refuses_a_context_card_that_does_not_exist` and
        `test_create_only_refuses_an_oversized_request_too`."""
        require_input_within_bounds(request, field="request")
        context_cards = resolve_cards(context_cards) if context_cards else None
        base = slug or self.slug_hint(request)
        for candidate in (base, f"{base}-{self._identity_hash(request, context_cards)}"):
            try:
                meta = self.repo.create(candidate, request, provider=provider, model_name=model_name,
                                        context_cards=context_cards)
                logger.info("session created: slug=%s", meta.slug)
                return meta
            except SessionExistsError:
                if self._same_identity(candidate, request, context_cards):
                    return self.repo.read_meta(candidate)  # idempotent re-init of the same discovery
        raise SessionExistsError(
            f"sessions '{base}' and '{base}-{self._identity_hash(request, context_cards)}' both exist "
            "with a different request or context selection — pass an explicit slug",
            details={"slug": base})

    def ensure_canonical(self, slug: str) -> None:
        """Public form of the migrate-on-first-mutation guard — call before writing an artifact to a
        session that may still live only in the legacy `out/` store."""
        self._ensure_canonical(slug)

    # ── deletion ─────────────────────────────────────────────────────────────
    def delete_session(self, slug: str) -> None:
        """Irreversibly remove a session (#238). Refuses a missing slug with the structured
        `session_not_found` error, raised by the repository's own *locked* existence check rather
        than a separate check here — a check here-and-there is the precondition-not-held-across-the-
        write shape invariant 9 is written against
        (`test_deleting_a_nonexistent_slug_is_refused_with_session_not_found`).

        A thin delegation on purpose: the ordering that matters (lock, remove the directory, release,
        unlink the lock file last) is the repository/store's own concern, so a Postgres backing can
        implement the identical guarantee its own way underneath this call."""
        self.repo.delete(slug)

    @staticmethod
    def _identity_hash(request: str, context_cards: list[str] | None) -> str:
        """The fallback slug suffix: a short hash over what makes a discovery distinct. The cards join
        the hash only when there are some, so the ordinary no-cards case keeps the slugs it had."""
        parts = [request.strip()]
        if context_cards:
            parts.append(",".join(sorted(context_cards)))
        return hashlib.sha1("␟".join(parts).encode("utf-8")).hexdigest()[:6]

    def _same_identity(self, slug: str, request: str, context_cards: list[str] | None) -> bool:
        """Whether an existing session is the same discovery: same request, same context selection.
        `None` (every card) and an explicit list are different selections, not the same one."""
        if not self.repo.has_meta(slug):
            return False  # a legacy-only session has no recorded cards to compare
        existing = self.repo.context_cards(slug)
        return (self.repo.request_text(slug).strip() == request.strip()
                and (sorted(existing) if existing else existing)
                == (sorted(context_cards) if context_cards else context_cards))

    # ── reads ─────────────────────────────────────────────────────────────────
    def meta(self, slug: str) -> SessionMeta:
        """The session metadata. A legacy-only session has no metadata, so callers that need it for a
        read-only op should use `load_model`, which tolerates the legacy layout."""
        return self.repo.read_meta(slug)

    def load_model(self, slug: str) -> EngineOutput:
        """The current model. Reads the mutation-backed store, falling back to a legacy `out/<slug>/`
        model for read-only operations (status/impact) so they work without forcing a migration."""
        return self.repo.load_model(slug)

    def exists_meta(self, slug: str) -> bool:
        """True if the session is in the mutation-backed store — i.e. `meta()` will succeed. A legacy
        `out/` session is readable but has no metadata until its first write migrates it."""
        return self.repo.has_meta(slug)

    def load_revision(self, slug: str, revision: int) -> EngineOutput:
        """A historical model revision — the basis for "what moved since this artifact was made?"."""
        return self.repo.load_revision(slug, revision)

    def list_sessions(self) -> list[SessionMeta]:
        """Every session's metadata, raising on the first one that will not load.

        The strict read, kept as such. A caller acting on one session it named is right to want the
        failure; what must not use this is an **aggregate**, because one unreadable member then
        raises before any row exists to degrade. Those call `list_entries` instead.
        """
        return [self.repo.read_meta(s) for s in self.repo.list_slugs()]

    def list_entries(self) -> list[SessionEntry]:
        """Every session, degrading per member instead of raising for the whole set (#7).

        This is the *source* of the rows, and where invariant 15 has to be enforced: guarding the
        calls made on each row leaves the comprehension that produced them unguarded, which is the
        line that breaks first. A member that cannot be read is reported, never dropped — a listing
        that silently omits it tells the reader nothing is wrong and loses the session.
        `test_one_unreadable_session_no_longer_takes_the_listing_down` and
        `test_the_degraded_row_states_no_fact_it_could_not_read`.

        The catch is bare `Exception`, deliberately: an aggregate's contract is that one member
        cannot take the view down, and the set of ways a member can be broken is open, so naming a
        family here is how a guard ends up nominally on and effectively off for the next failure
        mode — the shape of #7 itself. `doctor`'s `_session_health` already made this call for the
        same question. `BaseException` is *not* caught: a `KeyboardInterrupt` is not a broken
        session.

        Failing to list the slugs at all is **not** caught here and propagates: that is not one
        member failing but the aggregate having no members to speak for, and answering `[]` would
        tell a reader their sessions were deleted.

        **Between those two sits a third source of rows** (#80) — `list_unexaminable`, the names the
        store found and could not decide about. Dropping one loses it silently and calling it a
        session claims what nobody established, so it is a degraded row like any other:
        `test_one_unexaminable_entry_no_longer_takes_the_whole_listing_down` and
        `test_the_row_states_no_fact_it_could_not_read`. Sorted by slug at the end so the two
        sources interleave into one listing; `list_slugs` is already sorted, so a workspace with
        nothing unexaminable comes back in exactly the order it always did.
        """
        entries = []
        for slug in self.repo.list_slugs():
            try:
                entries.append(SessionEntry(slug=slug, meta=self.repo.read_meta(slug)))
            except Exception as e:  # noqa: BLE001 - see the docstring: an open set, by contract
                entries.append(SessionEntry(slug=slug, meta=None, error=str(e)))
        for entry in self.repo.list_unexaminable():
            entries.append(SessionEntry(slug=entry.name, meta=None, error=entry.error))
        return sorted(entries, key=lambda e: e.slug)

    def cards(self, slug: str) -> list[str] | None:
        """The context-card selection recorded for a session (None == all cards)."""
        return self.repo.context_cards(slug)

    def request_text(self, slug: str) -> str:
        """The originating request text (empty string if none)."""
        return self.repo.request_text(slug)

    def snapshot(self, slug: str) -> SessionSnapshot:
        """One coherent read of everything a provider call needs — see `SessionSnapshot`. The session
        must be in the mutation-backed store; call `ensure_canonical` first for one that may still be
        legacy, which is what every provider-backed operation does anyway before it writes."""
        if not self.repo.has_meta(slug):
            # `self.no_session(slug)`, not the module-level ambient `store.no_session_message` --
            # #457, one call site over from what #272 already fixed for `no_session` itself. The
            # ambient wrapper always names the *process* workspace; this service may be addressing an
            # explicitly-rooted repository instead, and the refusal has to name the store it actually
            # asked. See test_snapshot_names_the_root_of_an_explicitly_rooted_repository_not_the_ambient_one.
            raise self.no_session(slug)
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            return SessionSnapshot(
                slug=slug,
                revision=meta.current_revision,
                model=self.load_model(slug) if meta.current_revision > 0 else None,
                request=self.repo.request_text(slug),
                context_cards=meta.context_cards,
            )

    def rescope(self, slug: str, context_cards: list[str] | None) -> RescopeResult:
        """Re-scope an existing session's context-card selection (`session rescope`).

        Four questions, argued out in #168 and each decided in a test rather than restated here:

        1. **New revision, or mutate in place?** Both, depending on what is on disk — a plain
           metadata write at revision 0, its own revision (unchanged model, unchanged hash,
           `surface="session-rescope"`) once one exists, so the history cannot claim a switch never
           happened. `test_rescope_before_any_model_only_mutates_metadata` and
           `test_rescope_after_a_model_records_a_new_revision_with_unchanged_content`.
        2. **Does it mark existing artifacts stale?** No — context is not a fifth kind of dependency
           edge, and the model has not moved (invariant 1).
           `test_rescope_does_not_mark_existing_artifacts_stale`, with
           `test_a_model_change_still_marks_the_same_artifact_stale` as its positive control.
        3. **Does it re-run anything?** No; writing the selection *is* the whole effect, and the
           next turn reasons against it.
           `test_rescope_does_not_re_run_anything_the_next_snapshot_reads_the_new_cards`.
        4. **Untrusted input, same as creation.** `resolve_cards` runs here too — a re-scope is
           invariant 14's second entrance onto a persisted `context_cards`.
           `test_rescope_resolves_and_normalizes_cards_like_creation`.

        Re-scoping to the selection a session already has (order aside — this is a set) is a no-op:
        `test_rescope_to_the_current_selection_is_a_no_op`.
        """
        self._ensure_canonical(slug)
        resolved = resolve_cards(context_cards) if context_cards else None
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            previous = meta.context_cards
            same = ((sorted(previous) if previous else previous)
                    == (sorted(resolved) if resolved else resolved))
            if same:
                return RescopeResult(slug=slug, previous_context_cards=previous,
                                     context_cards=previous, revision=meta.current_revision,
                                     changed=False)
            if meta.current_revision > 0:
                model = self.load_model(slug)
                revision, meta = self.repo.save_revision(slug, model,
                                                         provenance={"surface": "session-rescope"})
                # The second `save_revision` call site here (`_plan`'s is the first): a re-scope
                # mints a real revision even with unchanged content, so an operator watching this
                # logger for "a revision landed" must see it too (#435) --
                # `test_a_rescope_that_mints_a_revision_is_logged_too`.
                logger.info("session rescoped: slug=%s revision=%d", slug, revision)
            else:
                # No model yet — nothing was reasoned under `previous`, so there is no revision to
                # mint. `save_revision` bumps `updated_at` for the branch above; this branch is the
                # only writer here, so it has to stamp it itself. `store._now()` rather than a second
                # implementation of "UTC, second precision, Z-suffixed" — one format, one place.
                meta.updated_at = store._now()
            meta.context_cards = resolved
            self.repo.write_meta(slug, meta)
        return RescopeResult(slug=slug, previous_context_cards=previous, context_cards=resolved,
                             revision=meta.current_revision, changed=True)

    # ── the write path ──────────────────────────────────────────────────────────
    def diff(self, slug: str, proposal: dict | str, *, require_complete: bool = True) -> UpdateResult:
        """Dry run of `update_model`: validate the proposal and report what *would* change, without
        writing anything (`model diff`). `revision` is the revision that would be created."""
        current = self.load_model(slug) if self.exists(slug) else None
        new = validate_proposal(proposal, require_complete=require_complete, current=current)
        return self._plan(slug, current, new, apply=False)

    def update_model(self, slug: str, proposal: dict | str, *, require_complete: bool = True,
                     expected_revision: int | None = None, provenance: dict | None = None) -> UpdateResult:
        """Validate a proposal and apply it as a new revision (`model apply`): saves the prior model
        as a revision, flags stale artifacts, and returns the structured outcome. A session that lives
        only in the retired `out/` layout is *named in the error*, not migrated behind your back —
        `ensure_writable` raises pointing at `requivo session migrate`.

        `expected_revision` is the optimistic-locking precondition (see `persistence.save_revision`):
        omit it for the single-user CLI, pass the client's last-known revision from a concurrent
        service. `provenance` records who produced the revision (provider / surface / model)."""
        self._ensure_canonical(slug)
        # One lock for the whole update. Reading the current model, saving the revision and rewriting
        # the artifact flags are three storage calls that must see one consistent session: without
        # this, a writer that lands between the read and the flag rewrite has its staleness silently
        # reverted by ours.
        #
        # Validation is *inside* the lock rather than before it, because a proposal is resolved against
        # the model it refines (`ModelProposal.resolve`): the reasoning it carries forward has to come
        # from the same model the diff is computed against, or a concurrent write could slip between
        # the two and the carried reasoning would describe a model that is no longer there.
        with self.repo.lock(slug):
            current = self.load_model(slug) if self.repo.read_meta(slug).current_revision > 0 else None
            new = validate_proposal(proposal, require_complete=require_complete, current=current)
            return self._plan(slug, current, new, apply=True,
                              expected_revision=expected_revision, provenance=provenance)

    def _plan(self, slug: str, current: EngineOutput | None, new: EngineOutput, *, apply: bool,
              expected_revision: int | None = None, provenance: dict | None = None) -> UpdateResult:
        # A first model (no prior) counts every present slot as changed, so the whole blast radius is
        # reported; otherwise only the slots that materially moved.
        changed = diff_models(current, new) if current is not None else list(new.model.keys())
        # The reasoning layer moves independently of the slots, and every generator is prompted with
        # it, so it invalidates artifacts on its own. On a first apply there is nothing to compare
        # against — the reasoning arrived with the model it describes.
        reasoning = diff_reasoning(current, new) if current is not None else ReasoningDiff()
        # Artifacts rest on slots via the static ARTIFACT_SLOTS map, so the blast radius is basis-neutral
        # — any model with these `changed` slots yields the same artifact set.
        report = propagate(new, changed)

        # Reasoning invalidation is about the *prior established* reasoning a change unseats — it exists
        # only when the current model on disk carries decisions/challenges (a refinement turn often drops
        # them from its reply, so `new` may have none). On a first apply — `current is None` — the
        # reasoning in `new` was proposed *for* this very state; it is not stale, so nothing is
        # invalidated. Computing this against `new` (the old `basis` fallback) was the bug: it reported a
        # model's own freshly-proposed decisions and challenges as invalidated on their first apply.
        if current is not None and (current.decisions or current.challenges):
            prior = propagate(current, changed)
            invalidated_decisions = [d.decision for d in prior.decisions]
            invalidated_challenges = [c.headline for c in prior.challenges]
        else:
            invalidated_decisions, invalidated_challenges = [], []

        def _resolve_stale(generated: set[str]) -> list[str]:
            # The blast radius, intersected with what actually exists on disk. Two edge sets feed it:
            # the slots an artifact consumes (ARTIFACT_SLOTS), and — when the reasoning layer moved —
            # REASONING_CONSUMERS, which is every generator, since each is prompted with the full
            # model. The saved assessment needs no special case in either: it rests on every slot.
            hit = set(report.artifacts) | (REASONING_CONSUMERS if reasoning.changed else set())
            return [t for t in ARTIFACT_FILES if t in hit and t in generated]

        if apply:
            try:
                revision, meta = self.repo.save_revision(
                    slug, new, expected_revision=expected_revision, provenance=provenance)
            except RevisionConflictError:
                logger.warning("model apply refused: slug=%s expected_revision=%s (conflict)",
                              slug, expected_revision)
                raise
            stale = _resolve_stale(set(meta.artifact_status))
            if stale:
                for t in stale:
                    meta.artifact_status[t].stale = True
                self.repo.write_meta(slug, meta)
            logger.info("model applied: slug=%s revision=%d changed_slots=%d stale_artifacts=%d",
                       slug, revision, len(changed), len(stale))
        else:
            meta = self.repo.read_meta(slug) if self.repo.has_meta(slug) else None
            revision = (meta.current_revision + 1) if meta else 1
            stale = _resolve_stale(set(meta.artifact_status) if meta else set())

        return UpdateResult(
            status="applied" if apply else "planned",
            revision=revision,
            changed_slots=changed,
            invalidated_decisions=invalidated_decisions,
            invalidated_challenges=invalidated_challenges,
            stale_artifacts=stale,
            readiness=_readiness(new),
            changed_decisions=reasoning.decisions,
            changed_challenges=reasoning.challenges,
            changed_opportunities=reasoning.opportunities,
        )

    # ── status ──────────────────────────────────────────────────────────────────
    def status(self, slug: str) -> dict:
        """A machine-readable status snapshot for `status --json` — rich enough that Claude Code and a
        the Web render the full picture (understanding checklist, priority questions, gaps,
        context) without rebuilding the presentation logic in another language. Everything here is a
        pure projection of the model plus the session metadata."""
        model = self.load_model(slug)
        meta = self.repo.read_meta(slug) if self.repo.has_meta(slug) else None
        artifacts = {}
        if meta:
            for t, st in meta.artifact_status.items():
                # Explicit stale flag only — revision is provenance, not an invalidation rule. See
                # ArtifactService.list for the rationale (dependency-graph freshness, not revision drift).
                artifacts[t] = {"revision": st.revision, "filename": st.filename, "stale": st.stale}
        return {
            "slug": slug,
            "revision": meta.current_revision if meta else None,
            **model_status(model),
            "context_cards": meta.context_cards if meta else None,
            "artifacts": artifacts,
        }
