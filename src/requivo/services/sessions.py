"""SessionService: create sessions and apply model updates through one validated pipeline.

`update_model` is the single write path, whatever produced the proposal: validate → diff → propagate
→ save a revision → flag stale artifacts → readiness, returned as a structured `UpdateResult`.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import cast

from requivo.core import persistence as store
from requivo.core.analysis import model_status, readiness_blockers
from requivo.core.context import resolve_cards
from requivo.core.contracts import EngineOutput
from requivo.core.dependencies import (
    ARTIFACT_FILENAMES,
    REASONING_CONSUMERS,
    EvidenceReport,
    EvidenceUnknown,
    ImpactReport,
    ReasoningDiff,
    diff_models,
    diff_reasoning,
    propagate,
    resolve_slots,
    thinner_evidence,
)
from requivo.core.errors import (
    ModelUnreadableError,
    RevisionConflictError,
    SessionExistsError,
    SessionNotFoundError,
    UnknownSlotError,
)
from requivo.core.perimeters import DEFAULT_PERIMETER, resolve_perimeter
from requivo.core.persistence import SessionMeta, Store
from requivo.core.persistence.identifiers import _stat_exists
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
    """One consistent read of a session, taken under the lock: revision, the model *at* that
    revision, and the inputs a provider call needs (invariant 12). The lock is released before the
    call; `expected_revision` on the write handles the session moving afterwards."""

    slug: str
    revision: int
    model: EngineOutput | None          # None before the first model (revision 0)
    request: str
    context_cards: list[str] | None     # None == every card
    perimeter: str = DEFAULT_PERIMETER  # resolved (#608) -- never None, even for a pre-perimeter session


@dataclass(frozen=True)
class SessionEntry:
    """One slug in the store, with its metadata or the reason it could not be read (#7): the third
    state an aggregate needs. `error` is the exception's text, since the remedy is the useful part.
    `test_one_unreadable_session_no_longer_takes_the_listing_down`."""

    slug: str
    meta: SessionMeta | None = None
    error: str | None = None

    @property
    def readable(self) -> bool:
        """True when the metadata loaded."""
        return self.meta is not None


@dataclass(frozen=True)
class SessionResolution:
    """The default session for a journey verb given no slug (#541). `default` is always a real slug
    once any session exists; `candidates` is empty for the "exactly one" case and otherwise lists
    every session considered, degraded rows included (invariant 15)."""
    default: str
    candidates: list[SessionEntry]


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
    invalidated_exclusions: list[str] = field(default_factory=list)  # excluded options now in question
    invalidated_thresholds: list[str] = field(default_factory=list)  # threshold conditions now in question
    stale_artifacts: list[str] = field(default_factory=list)        # artifact types now out of date
    readiness: Readiness = field(default_factory=lambda: Readiness(False, []))
    # What moved in the reasoning layer, per collection: the judgment over the facts, which can
    # invalidate an artifact on its own.
    changed_decisions: list[str] = field(default_factory=list)
    changed_challenges: list[str] = field(default_factory=list)
    changed_opportunities: list[str] = field(default_factory=list)
    changed_exclusions: list[str] = field(default_factory=list)
    changed_thresholds: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "revision": self.revision,
            "changed_slots": self.changed_slots,
            "changed_decisions": self.changed_decisions,
            "changed_challenges": self.changed_challenges,
            "changed_opportunities": self.changed_opportunities,
            "changed_exclusions": self.changed_exclusions,
            "changed_thresholds": self.changed_thresholds,
            "invalidated_decisions": self.invalidated_decisions,
            "invalidated_challenges": self.invalidated_challenges,
            "invalidated_exclusions": self.invalidated_exclusions,
            "invalidated_thresholds": self.invalidated_thresholds,
            "stale_artifacts": self.stale_artifacts,
            "readiness": self.readiness.to_dict(),
        }


def _readiness(model: EngineOutput) -> Readiness:
    blockers = readiness_blockers(model)
    return Readiness(ready=not blockers, blocking_slots=blockers)


class SessionService:
    """Create, resolve, load and mutate sessions through one validated pipeline. Storage is an
    injected `SessionRepository`; stateless beyond that handle."""

    def __init__(self, repo: SessionRepository | None = None):
        self.repo: SessionRepository = repo or default_repository()

    # ── resolution ────────────────────────────────────────────────────────────
    def resolve_slug(self, reference: str | Path, *, accept_path: bool = True) -> str:
        """Turn a user reference (slug, session directory, or model.json path) into a slug.

        `accept_path=False` refuses anything path-shaped, decided from the string alone (invariant 17):
        `test_resolve_slug_refuses_a_model_json_path_when_the_caller_opted_out` (#402). A reference
        is only mined for a name when a session really is there (#402, #414):
        `test_a_directory_reference_does_not_silently_use_an_unrelated_real_session`."""
        ref = str(reference)
        p = Path(ref)
        if not accept_path:
            looks_like_a_path = (
                p.name in ("model.json", "session.json")
                or os.sep in ref or (os.altsep and os.altsep in ref)
            )
            if looks_like_a_path:
                # Every mention of untrusted `ref` goes through `display_token` (invariant 14):
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
            # A metadata read preserves the third state that `is_file()` hides on Python 3.14.
            # `test_an_unreadable_model_json_path_refuses_cleanly_instead_of_crashing`.
            try:
                is_real_file = S_ISREG(p.stat().st_mode)
            except (FileNotFoundError, NotADirectoryError):
                is_real_file = False
            except OSError as e:
                raise SessionNotFoundError(
                    f"could not tell whether {display_token(ref)} is a saved model.json: {e}",
                    details={"ref": ref},
                ) from e
            return p.parent.name if is_real_file else ref
        # A directory is mined only when it carries a session marker (#414); metadata reads retain
        # the denied-ancestor error on Python 3.14 (#636).
        try:
            is_dir = S_ISDIR(p.stat().st_mode)
        except (FileNotFoundError, NotADirectoryError):
            is_dir = False
        except OSError as e:
            raise SessionNotFoundError(
                f"could not tell whether {display_token(ref)} is a session directory: {e}",
                details={"ref": ref},
            ) from e
        if is_dir:
            try:
                looks_like_a_session = _stat_exists(p / "session.json") or _stat_exists(p / "model.json")
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
        """Text to a slug-shaped name: the surface's seam onto slug derivation (#76), used for the
        request text and for a `discover <file>` filename stem."""
        return store.derive_slug(text)

    def exists(self, slug: str) -> bool:
        """True if a usable session exists."""
        return self.repo.exists(slug)

    def no_session(self, ref: str, *, what: str = "session",
                   details: dict | None = None) -> SessionNotFoundError:
        """The "no such session" refusal (#243), naming *this service's* root, not the ambient one
        (#272: `test_no_session_names_the_root_of_an_explicitly_rooted_repository`). `what` widens
        the noun; `details["ref"]` is a published key (`docs/compatibility.md`)."""
        message = self._store_for_error_text().no_session_message(ref, what=what)
        return SessionNotFoundError(message,
                                    details=details if details is not None else {"slug": ref})

    def _store_for_error_text(self) -> Store:
        """The `Store` this service's repository addresses, for `no_session`'s root; duck-typed on
        `repo.store()`, ambient only when there is none (#272)."""
        get_store = getattr(self.repo, "store", None)
        if callable(get_store):
            return cast(Store, get_store())
        return Store(workspace_root())

    def _ensure_canonical(self, slug: str) -> None:
        """Before any mutation: migrate a legacy `out/<slug>/` session in place on first write."""
        self.repo.ensure_writable(slug)

    # ── creation ──────────────────────────────────────────────────────────────
    def create_session(self, request: str, *, context_cards: list[str] | None = None,
                        slug: str | None = None, provider: str | None = None,
                        model_name: str | None = None,
                        perimeter: str | None = None) -> SessionMeta:
        """Create a fresh session from a request (no model yet); `slug` defaults to a derivation
        from the request, made collision-safe. Idempotent on identity, which is the request, its
        cards and its perimeter (invariant 11); the claim is `repo.create`, atomic. Cards and size
        are checked here, not trusted (invariant 14):
        `test_the_service_refuses_a_context_card_that_does_not_exist`."""
        meta, _created = self.create_session_report(
            request, context_cards=context_cards, slug=slug, provider=provider,
            model_name=model_name, perimeter=perimeter)
        return meta

    def create_session_report(self, request: str, *, context_cards: list[str] | None = None,
                              slug: str | None = None, provider: str | None = None,
                              model_name: str | None = None, strict_slug: bool = False,
                              perimeter: str | None = None,
                              ) -> tuple[SessionMeta, bool]:
        """`create_session`, plus whether *this call* created the session (`POST /sessions` answers
        201/200 off it, #425) and, with `strict_slug`, a 409 `session_exists` when an explicit slug
        is taken by a different identity. The default keeps the hash-suffixed fallback every other
        caller relies on (`test_a_taken_session_name_is_suffixed_rather_than_refused` pins it)."""
        require_input_within_bounds(request, field="request")
        context_cards = resolve_cards(context_cards) if context_cards else None
        # Resolved once, so "no perimeter named" and an explicit `software` compare equal below.
        resolved_perimeter = resolve_perimeter(perimeter)
        explicit = bool(slug)  # matches the `or` below: an empty string is "no slug", same as None
        base = slug or self.slug_hint(request)
        refuse_immediately = explicit and strict_slug
        candidates = (base,) if refuse_immediately else (
            base, f"{base}-{self._identity_hash(request, context_cards, resolved_perimeter)}")
        for candidate in candidates:
            try:
                meta = self.repo.create(candidate, request, provider=provider, model_name=model_name,
                                        context_cards=context_cards, perimeter=perimeter)
                logger.info("session created: slug=%s", meta.slug)
                return meta, True
            except SessionExistsError:
                if self._same_identity(candidate, request, context_cards, resolved_perimeter):
                    return self.repo.read_meta(candidate), False  # idempotent re-init, same discovery
                if refuse_immediately:
                    raise SessionExistsError(
                        f"session '{candidate}' already exists with a different request, context "
                        "selection or perimeter — choose a different slug",
                        details={"slug": candidate}) from None
        raise SessionExistsError(
            f"sessions '{base}' and "
            f"'{base}-{self._identity_hash(request, context_cards, resolved_perimeter)}' both exist "
            "with a different request, context selection or perimeter — pass an explicit slug",
            details={"slug": base})

    def find_existing_session(self, request: str, *, context_cards: list[str] | None = None,
                              perimeter: str | None = None, slug: str | None = None
                              ) -> SessionMeta | None:
        """Whether a session exists under this exact identity, read-only, `None` when none does; the
        router's free lookup before claiming (#601). Mirrors `create_session_report`'s candidates."""
        context_cards = resolve_cards(context_cards) if context_cards else None
        resolved_perimeter = resolve_perimeter(perimeter)
        base = slug or self.slug_hint(request)
        for candidate in (base, f"{base}-{self._identity_hash(request, context_cards, resolved_perimeter)}"):
            if self._same_identity(candidate, request, context_cards, resolved_perimeter):
                return self.repo.read_meta(candidate)
        return None

    def ensure_canonical(self, slug: str) -> None:
        """Public form of the migrate-on-first-mutation guard."""
        self._ensure_canonical(slug)

    # ── deletion ─────────────────────────────────────────────────────────────
    def delete_session(self, slug: str) -> None:
        """Irreversibly remove a session (#238); a missing slug is refused by the repository's own
        locked check (invariant 9): `test_deleting_a_nonexistent_slug_is_refused_with_session_not_found`."""
        self.repo.delete(slug)

    @staticmethod
    def _identity_hash(request: str, context_cards: list[str] | None,
                       perimeter: str = DEFAULT_PERIMETER) -> str:
        """The fallback slug suffix: a short hash over the identity. Cards join only when present, so
        the no-cards slugs stay; `perimeter` always joins, resolved by the caller (#608)."""
        parts = [request.strip(), perimeter]
        if context_cards:
            parts.append(",".join(sorted(context_cards)))
        return hashlib.sha1("␟".join(parts).encode("utf-8")).hexdigest()[:6]

    def _same_identity(self, slug: str, request: str, context_cards: list[str] | None,
                       perimeter: str = DEFAULT_PERIMETER) -> bool:
        """Same discovery: same request, same card selection (`None` and a list differ), same
        perimeter compared *resolved* on both sides, so a pre-#608 session reads as `software`."""
        if not self.repo.has_meta(slug):
            return False  # a legacy-only session has no recorded cards to compare
        existing = self.repo.context_cards(slug)
        existing_perimeter = resolve_perimeter(self.repo.read_meta(slug).perimeter)
        return (self.repo.request_text(slug).strip() == request.strip()
                and (sorted(existing) if existing else existing)
                == (sorted(context_cards) if context_cards else context_cards)
                and existing_perimeter == resolve_perimeter(perimeter))

    # ── reads ─────────────────────────────────────────────────────────────────
    def meta(self, slug: str) -> SessionMeta:
        """The session metadata; a legacy-only session has none (use `load_model` for reads)."""
        return self.repo.read_meta(slug)

    def load_model(self, slug: str) -> EngineOutput:
        """The current model, falling back to a legacy `out/<slug>/` model for read-only operations."""
        return self.repo.load_model(slug)

    def exists_meta(self, slug: str) -> bool:
        """True if the session is in the mutation-backed store, i.e. `meta()` will succeed."""
        return self.repo.has_meta(slug)

    def load_revision(self, slug: str, revision: int) -> EngineOutput:
        """A historical model revision — the basis for "what moved since this artifact was made?"."""
        return self.repo.load_revision(slug, revision)

    def list_sessions(self) -> list[SessionMeta]:
        """Every session's metadata, raising on the first that will not load; an aggregate uses
        `list_entries` instead."""
        return [self.repo.read_meta(s) for s in self.repo.list_slugs()]

    def list_entries(self) -> list[SessionEntry]:
        """Every session, degrading per member instead of raising for the set (#7, invariant 15).
        Bare `Exception`, deliberately: the ways a member can be broken are open. Failing to list the
        slugs at all propagates. Unexaminable entries (#80) are degraded rows too, interleaved by slug.
        `test_one_unreadable_session_no_longer_takes_the_listing_down`,
        `test_one_unexaminable_entry_no_longer_takes_the_whole_listing_down`."""
        entries = []
        for slug in self.repo.list_slugs():
            try:
                entries.append(SessionEntry(slug=slug, meta=self.repo.read_meta(slug)))
            except Exception as e:  # noqa: BLE001 - see the docstring: an open set, by contract
                entries.append(SessionEntry(slug=slug, meta=None, error=str(e)))
        for entry in self.repo.list_unexaminable():
            entries.append(SessionEntry(slug=entry.name, meta=None, error=entry.error))
        return sorted(entries, key=lambda e: e.slug)

    def resolve_default_session(self) -> SessionResolution:
        """The default session when a journey verb names none (#541): one → it; several → the most
        recently written by `updated_at`, with every candidate returned; none → a `RequivoError`
        naming `run`. Reads `list_entries()`, so a degraded row hides nothing (invariant 15)."""
        entries = self.list_entries()
        if not entries:
            raise SessionNotFoundError(
                "no session in this workspace yet -- run `requivo run` to start one.", details={})
        if len(entries) == 1:
            return SessionResolution(default=entries[0].slug, candidates=[])
        readable = [e for e in entries if e.readable]
        default = (max(readable, key=lambda e: cast(SessionMeta, e.meta).updated_at).slug
                   if readable else entries[0].slug)
        return SessionResolution(default=default, candidates=entries)

    def cards(self, slug: str) -> list[str] | None:
        """The context-card selection recorded for a session (None means every card)."""
        return self.repo.context_cards(slug)

    def request_text(self, slug: str) -> str:
        """The originating request text (empty string if none)."""
        return self.repo.request_text(slug)

    def snapshot(self, slug: str) -> SessionSnapshot:
        """One coherent read of everything a provider call needs; the session must be canonical."""
        if not self.repo.has_meta(slug):
            # `self.no_session`, not the ambient message: the refusal names the store it asked (#457).
            # test_snapshot_names_the_root_of_an_explicitly_rooted_repository_not_the_ambient_one.
            raise self.no_session(slug)
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)  # `migrate_session` already refused an unknown perimeter
            return SessionSnapshot(
                slug=slug,
                revision=meta.current_revision,
                model=self.load_model(slug) if meta.current_revision > 0 else None,
                request=self.repo.request_text(slug),
                context_cards=meta.context_cards,
                perimeter=resolve_perimeter(meta.perimeter),
            )

    def impact(self, slug: str, slots: list[str]) -> ImpactReport:
        """What rests on the named slots: a pure query, the API's `/impact` behind the seam (#425).
        A token matching nothing is refused (`test_impact_refuses_an_unknown_slot_naming_it_in_details`);
        an empty list is an empty report (`test_impact_with_no_slots_named_is_an_empty_report_not_a_refusal`)."""
        # One lock around both reads (invariant 12); the lock is re-entrant per thread.
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            perimeter = resolve_perimeter(meta.perimeter)
            model = self.load_model(slug)
            resolved, unmatched = resolve_slots(slots, perimeter)
            if unmatched:
                raise UnknownSlotError(
                    f"Unknown slot(s): {', '.join(unmatched)} -- use a slot id or a label word "
                    "(e.g. 'permissions', 'workflow', 'reporting').",
                    details={"unmatched": unmatched})
            report = propagate(model, resolved, perimeter)
            # Not narrowed to `slots` (#493): the slot that thickened is the one nobody asks about.
            report.evidence = self.thinner_evidence(slug)
        return report

    def thinner_evidence(self, slug: str) -> EvidenceReport:
        """Which decisions were derived while a slot they rest on was thinner than now (#493). The
        derivation revision is the earliest frozen one carrying the decision's id (invariant 5), so
        a reworded decision counts as new (`test_a_reworded_decision_counts_as_newly_derived_at_its_rewording`).
        A revision this version cannot read makes the unlocated decisions `could_not_tell`, never a
        flag (`test_a_revision_from_an_older_requivo_without_confidence_data_is_could_not_tell`)."""
        # The whole walk under the lock: `session import --force` swaps the directory under it.
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            perimeter = resolve_perimeter(meta.perimeter)
            if meta.current_revision == 0:
                return EvidenceReport()
            now = self.load_model(slug)
            pending = {d.id for d in now.decisions}
            derived_at: dict[int, set[str]] = {}
            frozen: dict[int, EngineOutput] = {}
            unreadable: str | None = None
            for rev in range(1, meta.current_revision + 1):
                if not pending:
                    break
                try:
                    then = self.repo.load_revision(slug, rev)
                except (SessionNotFoundError, ModelUnreadableError) as e:
                    unreadable = f"revision {rev} could not be read ({e.code})"
                    break
                found = pending & {d.id for d in then.decisions}
                if found:
                    derived_at[rev] = found
                    frozen[rev] = then
                    pending -= found
        by_id: dict[str, tuple] = {}
        for rev, ids in derived_at.items():
            partial = thinner_evidence(frozen[rev], now, perimeter)
            for f in partial.flagged:
                if f.id in ids:
                    f.derived_at = rev
                    by_id[f.id] = ("flagged", f)
            for u in partial.could_not_tell:
                if u.id in ids:
                    by_id[u.id] = ("unknown", u)
        report = EvidenceReport(reviewed=len(now.decisions))
        for d in now.decisions:
            state, item = by_id.get(d.id, (None, None))
            if state == "flagged":
                report.flagged.append(item)
            elif state == "unknown":
                report.could_not_tell.append(item)
            elif d.id in pending:
                # In no readable revision: the walk stopped, or the model was hand-edited.
                report.could_not_tell.append(EvidenceUnknown(
                    d.decision, d.id, unreadable or "recorded in no frozen revision"))
        return report

    def rescope(self, slug: str, context_cards: list[str] | None) -> RescopeResult:
        """Re-scope a session's context-card selection (`session rescope`, #168): a metadata write
        at revision 0, its own revision once a model exists; no artifact goes stale (invariant 1);
        nothing re-runs; cards are resolved as at creation (invariant 14); a same-set re-scope is a no-op.
        `test_rescope_after_a_model_records_a_new_revision_with_unchanged_content`,
        `test_rescope_does_not_mark_existing_artifacts_stale`, `test_rescope_to_the_current_selection_is_a_no_op`."""
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
                # A re-scope mints a revision, so it is logged like one (#435):
                # `test_a_rescope_that_mints_a_revision_is_logged_too`.
                logger.info("session rescoped: slug=%s revision=%d", slug, revision)
            else:
                # No revision to mint; this branch stamps `updated_at` itself, in the store's one format.
                meta.updated_at = store._now()
            meta.context_cards = resolved
            self.repo.write_meta(slug, meta)
        return RescopeResult(slug=slug, previous_context_cards=previous, context_cards=resolved,
                             revision=meta.current_revision, changed=True)

    # ── the write path ──────────────────────────────────────────────────────────
    def diff(self, slug: str, proposal: dict | str, *, require_complete: bool = True) -> UpdateResult:
        """Dry run of `update_model`: what *would* change, nothing written. `revision` is the one that would be created."""
        current = self.load_model(slug) if self.exists(slug) else None
        perimeter = resolve_perimeter(self.meta(slug).perimeter) if self.exists_meta(slug) else DEFAULT_PERIMETER
        new = validate_proposal(proposal, require_complete=require_complete, current=current,
                                perimeter=perimeter)
        return self._plan(slug, current, new, apply=False, perimeter=perimeter)

    def update_model(self, slug: str, proposal: dict | str, *, require_complete: bool = True,
                     expected_revision: int | None = None, provenance: dict | None = None) -> UpdateResult:
        """Validate a proposal and apply it as a new revision: save the prior model, flag stale
        artifacts, return the outcome. A legacy-only session is named in the error, not migrated.
        `expected_revision` is the optimistic-locking precondition; `provenance` records who produced it."""
        self._ensure_canonical(slug)
        # One lock for the read, the revision save and the flag rewrite; validation is inside it
        # because a proposal is resolved against the model it refines.
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            perimeter = resolve_perimeter(meta.perimeter)
            current = self.load_model(slug) if meta.current_revision > 0 else None
            new = validate_proposal(proposal, require_complete=require_complete, current=current,
                                    perimeter=perimeter)
            return self._plan(slug, current, new, apply=True, perimeter=perimeter,
                              expected_revision=expected_revision, provenance=provenance)

    def _plan(self, slug: str, current: EngineOutput | None, new: EngineOutput, *, apply: bool,
              perimeter: str = DEFAULT_PERIMETER,
              expected_revision: int | None = None, provenance: dict | None = None) -> UpdateResult:
        # A first model counts every present slot as changed.
        changed = diff_models(current, new) if current is not None else list(new.model.keys())
        # The reasoning layer invalidates on its own; on a first apply there is nothing to compare against.
        reasoning = diff_reasoning(current, new) if current is not None else ReasoningDiff()
        # Artifacts rest on slots through the static ARTIFACT_SLOTS map, so the blast radius is basis-neutral.
        report = propagate(new, changed, perimeter)

        # Reasoning invalidation is about the *prior* reasoning a change unseats; on a first apply
        # `new`'s reasoning was proposed for this very state and nothing is invalidated.
        if current is not None and (current.decisions or current.challenges or current.exclusions
                                    or current.thresholds):
            prior = propagate(current, changed, perimeter)
            invalidated_decisions = [d.decision for d in prior.decisions]
            invalidated_challenges = [c.headline for c in prior.challenges]
            invalidated_exclusions = [e.option for e in prior.exclusions]
            invalidated_thresholds = [t.condition for t in prior.thresholds]
        else:
            invalidated_decisions, invalidated_challenges = [], []
            invalidated_exclusions, invalidated_thresholds = [], []

        def _resolve_stale(generated: set[str]) -> list[str]:
            # The blast radius intersected with what exists on disk; REASONING_CONSUMERS is every
            # generator when the reasoning moved (invariant 1).
            hit = set(report.artifacts) | (REASONING_CONSUMERS if reasoning.changed else set())
            return [t for t in ARTIFACT_FILENAMES if t in hit and t in generated]

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
            invalidated_exclusions=invalidated_exclusions,
            invalidated_thresholds=invalidated_thresholds,
            stale_artifacts=stale,
            readiness=_readiness(new),
            changed_decisions=reasoning.decisions,
            changed_challenges=reasoning.challenges,
            changed_opportunities=reasoning.opportunities,
            changed_exclusions=reasoning.exclusions,
            changed_thresholds=reasoning.thresholds,
        )

    # ── status ──────────────────────────────────────────────────────────────────
    def status(self, slug: str) -> dict:
        """A machine-readable status for `status --json`: a pure projection of the model plus metadata."""
        meta = self.repo.read_meta(slug) if self.repo.has_meta(slug) else None
        perimeter = resolve_perimeter(meta.perimeter) if meta else DEFAULT_PERIMETER
        model = self.load_model(slug)
        artifacts = {}
        if meta:
            for t, st in meta.artifact_status.items():
                # The explicit stale flag only; revision is provenance (invariant 1).
                artifacts[t] = {"revision": st.revision, "filename": st.filename, "stale": st.stale}
        return {
            "slug": slug,
            "revision": meta.current_revision if meta else None,
            "perimeter": perimeter,
            **model_status(model, perimeter),
            "context_cards": meta.context_cards if meta else None,
            "artifacts": artifacts,
        }
