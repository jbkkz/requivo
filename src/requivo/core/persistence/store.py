"""The canonical session store: `Store`, session and revision CRUD, and the ambient-default
module-level wrappers over it.

Split out of `core/persistence.py` by #550 (the lean pass, #548) into a `core/persistence/`
package: this module is the composition root -- `Store` composes `_ScanMixin` (`scan.py`) and
`_LockMixin` (`lock.py`), and every module-level function below that used to read
`_default_store()` still does, from here, since that is where `_default_store` and `Store` live.
The session metadata schema and model I/O live in `models.py`, moved out to keep this module under
the 900-line ceiling. Public names are re-exported from `core/persistence/__init__.py`, so
`requivo.core.persistence.X` is unchanged for every caller.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Optional

from requivo.core.contracts import EngineOutput
from requivo.core.errors import (
    ArtifactRevisionOutOfRangeError,
    InvalidFilenameError,
    InvalidSlugError,
    RevisionConflictError,
    SessionExistsError,
    SessionNotFoundError,
    SessionUnreadableError,
)
from requivo.core.persistence.atomic import _atomic_write
from requivo.core.persistence.identifiers import _probe, _refuse_new_reserved_slug, _slug_shape, validate_filename
from requivo.core.persistence.lock import _LockHandle, _LockMixin, _resolve, is_contained
from requivo.core.persistence.models import (
    ArtifactStatus,
    RevisionRecord,
    SessionMeta,
    _now,
    _read_model,
    content_hash,
    migrate_session,
)
from requivo.core.persistence.scan import NonSessionEntry, UnexaminableEntry, _ScanMixin
from requivo.core.selectors import display_token
from requivo.paths import output_root as _ambient_output_root
from requivo.paths import workspace_root


class Store(_ScanMixin, _LockMixin):
    """One workspace's `.requivo/` layout, addressed by an explicit `root` rather than by reading
    `paths.workspace_root()` (`REQUIVO_WORKSPACE`/cwd) fresh on every call (#272).

    Every method below used to be a free function resolving its root from `requivo.paths` ambiently,
    which is what made two `FileSessionRepository` instances in one process indistinguishable. This
    class is the "one construction site" `docs/cloud-boundary.md` (§3.1) argues for: an object holds
    the root, and everything that needs to know *which* workspace it addresses reads it off `self`.
    `test_two_repositories_against_two_roots_are_independent_in_one_process`.

    The module-level functions of the same names, below, are thin wrappers over a **freshly-resolved
    default instance**, `Store(workspace_root())`, rebuilt on every call — that is what preserves the
    CLI's `--workspace`/`REQUIVO_WORKSPACE` behaviour byte-for-byte, while an explicit `Store(root)`
    is immune to a mid-process env mutation by design
    (`test_an_explicit_repository_root_is_immune_to_a_later_workspace_env_mutation` and
    `test_the_ambient_default_repository_still_tracks_a_mid_process_workspace_mutation`).

    **Root identity, not object identity, decides lock re-entrancy** — two roots sharing a slug name
    are two sessions, and keying by `id(self)` would instead break re-entrancy for the ambient
    wrapper, which builds a fresh instance per call. `test_two_roots_sharing_a_slug_do_not_share_a_lock`
    and `test_reentrant_acquisition_across_fresh_ambient_stores_is_still_recognised`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        # Resolved once here, not by `_lock_key` per acquisition (#272): were the answer ever to
        # change between an outer and an inner acquisition -- an ancestor symlink repointed mid-hold
        # -- `session_lock`'s re-entrancy check would miss and a second `flock` on a distinct open
        # file description could block the process against its own held lock. Fixing the value at the
        # same moment as `self.root` removes the question rather than arguing it cannot occur.
        # `test_lock_key_resolves_the_root_once_at_construction_not_per_acquisition`.
        self._root_key = str(_resolve(root))

    # ── roots -- mirroring paths.py's ambient functions, bound to self.root instead of the process ──


    def store_root(self) -> Path:
        return self.root / ".requivo"

    def session_root(self) -> Path:
        return self.store_root() / "sessions"

    def lock_root(self) -> Path:
        return self.store_root() / "locks"

    def debug_root(self) -> Path:
        return self.store_root() / "debug"

    def output_root(self) -> Path:
        """The retired `out/` layout root -- deliberately NOT derived from `self.root`, unlike every
        other root on this class. `REQUIVO_OUTPUT_DIR`/cwd was always a knob independent of
        `REQUIVO_WORKSPACE`, so deriving it here silently substituted the workspace root for cwd and
        broke `session migrate` for anyone passing `--workspace` without also setting
        `REQUIVO_OUTPUT_DIR` (#272). Pinned by
        `test_an_explicit_stores_legacy_root_still_honours_the_ambient_output_dir_override` and
        `test_an_explicit_stores_legacy_root_is_cwd_relative_with_no_override`."""
        return _ambient_output_root()


    # ── everything below was a free function; each docstring is the original, unchanged, and each
    # body is unchanged except that it now reads its root off `self` -----------------------------

    def ensure_store_dir(self, path: Path) -> Path:
        """`mkdir(parents=True, exist_ok=True)` for anything under `.requivo/`, writing the privacy
        `.gitignore` on the call that brings the store root into existence.

        **Every directory creation under the store goes through here, and that is the point** (#211).
        `.requivo/` lands in the caller's workspace, which defaults to cwd, and holds the client's
        request verbatim — a routine `git add .` publishes confidential requirements against the
        local-first confidentiality this product states as its wedge. There is no single "first
        creation" site to guard: seven call sites create the root as a `parents=True` ancestor, so
        `test_no_store_directory_is_created_outside_ensure_store_dir` guards the seam instead.

        **Written once, on creation, and never recreated**, so a deliberate deletion or a hand edit
        stands: `test_the_privacy_gitignore_is_written_once_and_never_restored`.

        **The trigger is `mkdir` winning, not `exists()` answering** (#320). *Does the root exist* is
        not *did I create the root*, so a marker write that failed once left `.requivo/` present and
        unignored and every later call read `fresh = False` — one transient error switching the
        confidentiality guarantee off for the life of that workspace, indistinguishable from a user
        who deleted the file on purpose. `mkdir(parents=True)` with no `exist_ok` answers the real
        question atomically and probes nothing (`Path.exists()` also re-raises `EACCES` — invariant
        15's #80, one function along). All-or-nothing so a failure is retryable: the root is removed
        again if the marker cannot be written, keeping the store's states to "root and marker" or
        "neither". Pinned by
        `test_a_failed_marker_write_leaves_no_root_behind_to_suppress_the_next_attempt`.
        """
        root = self.store_root()
        fresh = True
        try:
            root.mkdir(parents=True)
        except FileExistsError:
            # Somebody else owns the root — this process, an earlier run, or a concurrent creator whose
            # marker decision already stands. Losing this race is success.
            fresh = False
        except OSError as e:
            raise SessionUnreadableError(
                f"could not create the session store at {root}: {e}", details={"path": str(root)}) from e
        if fresh:
            marker = root / ".gitignore"
            try:
                # `x` rather than a plain write: the loser of a race must not truncate the winner's file.
                with open(marker, "x", encoding="utf-8") as fh:
                    fh.write(_STORE_GITIGNORE)
            except FileExistsError:
                pass
            except OSError as e:
                with suppress(OSError):
                    root.rmdir()
                raise SessionUnreadableError(
                    f"could not write the privacy marker at {marker}: {e}",
                    details={"path": str(marker)}) from e
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SessionUnreadableError(
                f"could not create {path}: {e}", details={"path": str(path)}) from e
        return path


    def no_session_message(self, ref: str, *, what: str = "session") -> str:
        """The one sentence for "there is no such session" — every CLI-facing site builds it here (#243).

        Three facts, and the two that were missing are the ones that end the trap. **Where Requivo
        looked**, because the way a session actually goes missing is a user running from a different
        directory — the plugin README calls it a failure with no visible symptom — and a root printed at
        the moment of the refusal is what makes that visible. **How to see what is really there**, so a
        typo'd slug is a step rather than a dead end. The third is the reference itself, which was the
        only one all five previous wordings carried.

        A method rather than a constant because the session root is workspace-dependent and must be
        read when the refusal happens, not at import — and, since #272, dependent on *which* store is
        asking, not only on the ambient process root.

        `what` exists for the one caller whose absence is genuinely wider: `_resolve_ref` accepts a
        *path* to a `model.json` as well as a slug, so "no session" would name half of what it looked
        for. Everything else takes the default.

        The word `canonical` is deliberately gone. It distinguished this layout from the retired `out/`
        one — a fact about the store's history that a user cannot act on, and it appeared only in the
        three sites reachable from none of the main verbs, so the jargon and the missing help arrived
        together.

        `display_token` for the same reason every other render of an untrusted string calls it (#40):
        the reference is raw argv, a newline in it ends the line, and everything after that point reads
        as a sentence Requivo is saying. **On every current CLI route it cannot fire**, because
        `validate_slug` refuses a control character first — it is here as the second line of defence
        invariant 14 asks for, since this function is public and an external consumer calls this layer
        rather than a careful surface. Pinned as such, against the builder, by
        `test_the_shared_builder_escapes_a_reference_it_could_be_handed_directly`; a test routed through
        a verb would have been green whether or not this call escaped anything.

        The whole set is pinned by `tests/test_session_not_found.py`, which sweeps the *verbs* rather
        than the sites, because the builder this replaced was itself correct and reached by nothing a
        user runs.
        """
        return (f"no {what} named {display_token(ref)} under {self.session_root()}. "
                "`requivo session list` shows the sessions in this workspace; a different --workspace "
                "(or REQUIVO_WORKSPACE) changes where Requivo looks.")


    def _no_session(self, slug: str) -> SessionNotFoundError:
        """The one refusal for "there is no such session", so the lock and the metadata read cannot drift
        into telling a caller two different stories about the same absence."""
        return SessionNotFoundError(self.no_session_message(slug), details={"slug": slug})


    def canonical_dir(self, slug: str) -> Path:
        """The canonical session directory `<workspace>/.requivo/sessions/<slug>/`."""
        return _child_of(self.session_root(), slug)


    def legacy_dir(self, slug: str) -> Path:
        """The legacy `out/<slug>/` directory — read-only, and migrated only by an explicit
        `requivo session migrate`, never on a read or a first write (see `migrate_legacy`)."""
        return _child_of(self.output_root(), slug)


    def artifact_path(self, slug: str, filename: str) -> Path:
        """`<session>/artifacts/<filename>`, with **both** halves validated — the single chokepoint every
        artifact read and write goes through.

        One function rather than a check at each call site, for the reason `_child_of` gives for the
        slug: a rule applied per-caller is a rule the next caller forgets. Belt-and-suspenders in the
        same shape — the pattern makes a separator or a dot segment unrepresentable, and the result is
        confirmed a genuine child of `artifacts/` through the same `is_contained` the slug half uses.

        **Display-only callers come through here too, and that is not ceremony.** Two sites printed
        the path inline without opening it and survived both the sweep that closed the writes (#5) and
        the one that closed the read (#23), because "it only prints it" reads as harmless — a read
        traversal answers what this code may *disclose*, and a printed path is the plainest disclosure
        there is. Coming through here also means the name cannot forge a line in the terminal it is
        printed to: `_FILENAME_RE` is anchored at end-of-string and admits no line break (#40).

        The name arrives on an `ArtifactStatus`, whose `filename` is a plain `str` nothing re-validates
        when `read_meta` loads it back — invariant 14's threat model exactly. **`session import` is not
        that door, and saying so is the point**: `check_session_dir` puts every recorded filename
        through `validate_filename` and `is_contained`, and import refuses the whole archive when
        either fails (`test_a_crafted_artifact_filename_cannot_be_used_to_probe_for_files_outside_the_session`,
        `test_an_artifact_that_is_a_symlink_out_of_the_session_is_still_refused`). Since #260 that is
        the *whole* of what pins a filename whose artifact type this build does not know, since there
        is then no `ARTIFACT_FILENAMES` value to pin it against; the claim that matters is unchanged —
        a bare file inside `artifacts/`, or the archive is refused.

        A target that is not there is not an error here: `is_contained` answers True for what it
        cannot find rather than raising, so routing a display site through this does not turn a
        session with nothing generated into a refusal."""
        d = self.canonical_dir(slug) / "artifacts"
        p = d / validate_filename(filename)
        if not is_contained(p, d):
            raise InvalidFilenameError(
                f"artifact filename {filename!r} does not resolve to a path inside {d}",
                details={"slug": slug, "filename": filename})
        return p


    def session_exists(self, slug: str) -> bool:
        return _probe(self.canonical_dir(slug) / "session.json", slug)


    def legacy_exists(self, slug: str) -> bool:
        return _probe(self.legacy_dir(slug) / "model.json", slug)


    def write_meta(self, slug: str, meta: SessionMeta) -> Path:
        d = self.canonical_dir(slug)
        self.ensure_store_dir(d)
        return _atomic_write(d / "session.json", meta.model_dump_json(indent=2))


    def read_meta(self, slug: str) -> SessionMeta:
        p = self.canonical_dir(slug) / "session.json"
        # Through `_probe`, not a bare `p.exists()` (#264): `Path.exists()` re-raises `EACCES`, and this
        # check used to sit outside the `try` below that wraps `OSError`, so a session.json the process
        # cannot stat escaped as a raw `PermissionError` instead of `SessionUnreadableError` -- the
        # identical unguarded probe #80 removed from `_scan_session_root` and #97 removed from
        # `session_exists`, a third time here.
        if not _probe(p, slug):
            raise self._no_session(slug)
        try:
            return migrate_session(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            raise SessionUnreadableError(f"session '{slug}' has an unreadable session.json: {e}",
                                         details={"slug": slug}) from e


    def create_session(self, slug: str, request: str, *, provider: str | None = None,
                       model_name: str | None = None, context_cards: list[str] | None = None) -> SessionMeta:
        """Create a fresh session directory from a request — no model yet (current_revision 0). The
        model is applied later via `save_revision` (deterministic `model apply`, or a provider turn).

        The session is assembled beside its destination and moved in with a single rename, which is
        the *claim* on the slug (invariant 11). Two bugs follow from doing it any other way: a
        preceding `has_meta` check is not atomic, so two concurrent creations both passed it and the
        second rewrote the first's identity, provider and cards; and a directory created before its
        metadata is a session a concurrent reader can find with no `session.json` in it."""
        now = _now()
        meta = SessionMeta(
            session_id=uuid.uuid4().hex, slug=slug, created_at=now, updated_at=now,
            provider=provider, model_name=model_name, context_cards=context_cards,
            request_hash=content_hash(request),
        )
        d = self.canonical_dir(slug)
        self.ensure_store_dir(d.parent)
        # Dot-prefixed, so a staging directory can never be mistaken for a session: slugs are validated and
        # cannot start with a dot, and `list_session_slugs` skips them.
        staging = d.with_name(f".{d.name}.new-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            (staging / "revisions").mkdir(parents=True)
            (staging / "artifacts").mkdir()
            _atomic_write(staging / "request.md", request)
            _atomic_write(staging / "session.json", meta.model_dump_json(indent=2))
            try:
                staging.rename(d)
            except OSError as e:
                if not d.exists():  # the rename failed for some other reason — don't mislabel it
                    raise
                raise SessionExistsError(f"session '{slug}' already exists", details={"slug": slug}) from e
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return meta


    def delete_session(self, slug: str) -> None:
        """Irreversibly remove a session: its directory and its lock file (#238).

        **The directory removal runs entirely inside `session_lock`'s critical section** (invariant 9),
        so a racing writer either finishes first and has its result removed, or blocks and then meets
        `session_lock`'s authoritative existence check rather than writing into a half-removed
        directory.
        `test_a_writer_racing_an_in_flight_delete_is_refused_rather_than_writing_into_a_half_removed_directory`
        and `test_delete_waits_for_a_concurrent_writer_then_removes_what_it_wrote`.

        **The lock file is unlinked *while the lock is still held*, not after releasing it.** The
        slug can be legally re-created the instant `rmtree` returns (invariant 11: `create_session`'s
        rename is lock-free), so any gap between release and unlink lets a third actor mint a second
        inode under the same name and two holders each believe they hold the only lock — the failure
        #22 named when it rejected unlinking a lock file a concurrent process may hold.
        `test_the_lock_file_is_gone_before_the_lock_is_released_not_after` pins the ordering.

        **On Windows that unlink is refused every time, not as an edge (#469)**, and swallowing the
        refusal left `doctor` reporting residue after an ordinary delete — the diagnostic accusing the
        user of a state Requivo had created two lines earlier. Only that platform defers to
        `_LockHandle.unlink_on_release`; POSIX keeps the zero-window ordering byte for byte.
        `test_delete_session_removes_the_directory_and_the_lock_file` and
        `test_a_delete_whose_in_lock_unlink_is_refused_still_removes_the_lock_file`, the second of
        which stages the Windows refusal on every platform.
        """
        with self.session_lock(slug) as lock:
            shutil.rmtree(self.canonical_dir(slug))
            try:
                self.lock_path(slug).unlink(missing_ok=True)
            except OSError:
                # The unlink under the lock is the correct ordering and the only one with no window
                # at all; a platform that refuses it gets the next-smallest, not a leftover file.
                lock.unlink_on_release()


    def save_revision(self, slug: str, model: EngineOutput, *, expected_revision: int | None = None,
                      provenance: dict | None = None) -> tuple[int, SessionMeta]:
        """Persist a new model revision: freeze revisions/NNNN-model.json, replace model.json with the
        same payload (the prior model is already frozen in an earlier revision file), record the
        revision's provenance, then bump current_revision + updated_at. Returns (new_revision,
        updated_meta). The order of those writes is a guarantee rather than a detail; see the comment on
        the two `_atomic_write` calls below.

        `expected_revision` is an optimistic-locking precondition: when given, the write fails with
        `RevisionConflictError` unless the session is still at that revision — so two updates racing from
        the same base can't both land silently. The single-user CLI omits it (last-writer-wins is fine
        locally); a concurrent Web service passes the revision the client read. `provenance` carries the
        surface-supplied fields (provider / model_name / surface / prompt_version) for the revision log.

        The precondition and every write it guards run under `session_lock`, because a check that is not
        held across the writes it authorises is not a precondition — two writers could both read revision
        N, both pass the check, and both write revision N+1."""
        with self.session_lock(slug):
            meta = self.read_meta(slug)  # raises SessionNotFoundError if the session isn't there
            if expected_revision is not None and meta.current_revision != expected_revision:
                raise RevisionConflictError(
                    f"session '{slug}' is at revision {meta.current_revision}, not the expected "
                    f"{expected_revision} — reload the current model and re-apply",
                    details={"slug": slug, "expected": expected_revision,
                             "actual": meta.current_revision})
            d = self.canonical_dir(slug)
            self.ensure_store_dir(d / "revisions")
            rev = meta.current_revision + 1
            payload = model.model_dump_json(indent=2)
            # Frozen revision file first, then model.json. Three writes and no transaction, so the order
            # decides what a crash between two of them leaves: reversed, a death here served every reader
            # content no revision records while session.json still named the previous one. Pinned by
            # `test_a_crash_after_the_first_payload_write_still_reads_as_the_recorded_revision` and, on
            # the revision 0 -> 1 arm that reports different codes,
            # `test_a_crash_in_the_very_first_apply_leaves_a_session_still_at_revision_zero`. The window
            # this does *not* close is pinned beside them by
            # `test_a_crash_after_both_payload_writes_is_still_reported_as_inconsistent`.
            _atomic_write(d / "revisions" / f"{rev:04d}-model.json", payload)
            _atomic_write(d / "model.json", payload)
            prov = dict(provenance or {})
            meta.revisions.append(RevisionRecord(
                revision=rev,
                created_at=_now(),
                previous_revision=meta.current_revision or None,
                model_hash=content_hash(payload),
                provider=prov.get("provider"),
                model_name=prov.get("model_name"),
                surface=prov.get("surface"),
                prompt_version=prov.get("prompt_version"),
                usage_input_tokens=prov.get("usage_input_tokens"),
                usage_output_tokens=prov.get("usage_output_tokens"),
                usage_cache_read_tokens=prov.get("usage_cache_read_tokens"),
                usage_cache_write_tokens=prov.get("usage_cache_write_tokens"),
                usage_rate_per_mtok=prov.get("usage_rate_per_mtok"),
                usage_priced_as_of=prov.get("usage_priced_as_of"),
            ))
            meta.current_revision = rev
            meta.updated_at = _now()
            self.write_meta(slug, meta)
            return rev, meta


    def load_session_model(self, slug: str) -> EngineOutput:
        """The current model of a canonical session."""
        p = self.canonical_dir(slug) / "model.json"
        if not p.exists():
            raise SessionNotFoundError(
                f"session '{slug}' has no model yet (apply a proposal first)", details={"slug": slug})
        return _read_model(p, slug=slug)


    def load_revision_model(self, slug: str, revision: int) -> EngineOutput:
        """A historical model revision — the basis for `impact` since a given point."""
        p = self.canonical_dir(slug) / "revisions" / f"{revision:04d}-model.json"
        if not p.exists():
            raise SessionNotFoundError(
                f"session '{slug}' has no revision {revision}", details={"slug": slug, "revision": revision})
        return _read_model(p, slug=slug, revision=revision)


    def session_request(self, slug: str) -> str:
        p = self.canonical_dir(slug) / "request.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""


    def save_session_artifact(self, slug: str, artifact_type: str, filename: str, content: str,
                              source_revision: int, *, stale: bool = False) -> ArtifactStatus:
        """Write an artifact under artifacts/ and record its provenance (source revision) in session.json.

        The revision is validated against the session's history first: provenance that cannot be true is
        worse than none, because every freshness question downstream is answered from it. A revision in
        the future (or before the first model) is refused rather than recorded.

        `stale` is supplied by the caller, which is the layer that knows the dependency graph — see
        `ArtifactService.save`. Core records freshness; it does not decide it.

        `filename` is validated exactly as `slug` is, and before the lock is taken: it is the *other* half
        of the write target, and it is also recorded into session.json, where `integrity.py` and the
        artifact-show paths read it back — so an unvalidated one both escapes the directory and persists.
        """
        path = self.artifact_path(slug, filename)   # refuse a bad target before taking the lock
        with self.session_lock(slug):
            meta = self.read_meta(slug)
            if not 1 <= source_revision <= meta.current_revision:
                raise ArtifactRevisionOutOfRangeError(
                    f"cannot record {artifact_type!r} against revision {source_revision}: session '{slug}' "
                    f"has revisions 1..{meta.current_revision or 0}",
                    details={"slug": slug, "source_revision": source_revision,
                             "current_revision": meta.current_revision})
            self.ensure_store_dir(path.parent)
            _atomic_write(path, content)
            st = ArtifactStatus(revision=source_revision, filename=filename, updated_at=_now(), stale=stale)
            meta.artifact_status[artifact_type] = st
            meta.updated_at = _now()
            self.write_meta(slug, meta)
            return st


    def write_artifact_file(self, slug: str, filename: str, content: str) -> Path:
        """Write a raw file into a session's artifacts/ directory (no status tracking) — for the neutral
        epic exports (epic.json / epic.github.json / …) that are extra views of one generated artifact.

        Both halves of the target go through `artifact_path`: the mutating route validated its slug and
        not the filename beside it, so `write_artifact_file(slug, '../../../x.md', …)` wrote outside the
        session entirely."""
        path = self.artifact_path(slug, filename)
        self.ensure_store_dir(path.parent)
        return _atomic_write(path, content)


    def read_artifact_file(self, slug: str, filename: str) -> Optional[str]:
        """The saved content of a file under a session's artifacts/, or None if there is no such file.

        The read sibling of `write_artifact_file`, so the read goes through `artifact_path` instead of
        re-joining the path. `FileSessionRepository` built it inline one layer above the chokepoint,
        which is how it escaped the sweep that routed the two *mutating* paths through it — `_child_of`
        on a rule the next caller forgets. A read traversal is a different exposure from a write one:
        not what this code may create, but what it may disclose.

        **Absence and refusal are deliberately different answers**: an unsafe `filename` raises,
        only a genuinely missing file returns None. Collapsing them makes a rejected traversal
        indistinguishable from an artifact nobody has generated yet.

        Decoded as UTF-8 explicitly, matching `_atomic_write` on the way in (invariant 16)."""
        p = self.artifact_path(slug, filename)
        return p.read_text(encoding="utf-8") if p.exists() else None


    def migrate_legacy(self, slug: str) -> SessionMeta:
        """Copy a legacy `out/<slug>/` session into the canonical store, **preserving the originals**.

        Called explicitly (`requivo session migrate`), never on a read. The existing model becomes
        revision 1; provenance is recovered from the old session.json where present; known artifact files
        are copied into artifacts/ and recorded at revision 1. The legacy directory is left untouched.

        **The claim on the slug is `create_session`'s rename**, not an existence check (invariant 11).
        Checking only that the legacy *model* existed meant that, pointed at a slug a live session
        occupied, this rewrote session.json at revision 0 and then wrote the legacy model over
        revisions/0001-model.json — the only durable copy, so revision 1 was destroyed with no copy
        anywhere. The rename now loses and `SessionExistsError` is raised before anything is written.

        Everything after the claim runs under one `session_lock` (invariant 9), so the metadata patch,
        the revision and the artifact writes are a single unit, and `expected_revision=0` holds the
        session to the state the claim left it in."""
        from requivo.core.dependencies import ARTIFACT_FILES  # local import avoids a load-time cycle

        src = self.legacy_dir(slug)
        if not (src / "model.json").exists():
            raise SessionNotFoundError(f"no legacy session '{slug}' under {self.output_root()}",
                                       details={"slug": slug})
        request = ""
        for name in ("request.md", "request.txt"):
            if (src / name).exists():
                request = (src / name).read_text(encoding="utf-8")
                break
        old: dict = {}
        if (src / "session.json").exists():
            try:
                old = json.loads((src / "session.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old = {}
        # Parse the legacy model *before* claiming the slug: a malformed out/ model should fail without
        # leaving an empty session behind holding a name nothing can now use.
        #
        # Through `_read_model` like the other three: wrapping three of four doors would be the defect
        # the helper exists to prevent (#204). `slug=` is deliberately not passed -- the legacy layout
        # has no `revisions/`, so the recovery remedy would name a directory that is not there.
        model = _read_model(src / "model.json")

        if request:
            req_hash = content_hash(request)
        else:
            # Fall back to the legacy session.json's hash, normalising a bare hex digest to "sha256:…".
            legacy_hash = str(old.get("request_sha256", ""))
            req_hash = legacy_hash if legacy_hash.startswith("sha256:") or not legacy_hash else "sha256:" + legacy_hash

        # The claim. Raises SessionExistsError if a canonical session already occupies the slug.
        self.create_session(slug, request, provider=old.get("provider"), model_name=old.get("model_name"),
                            context_cards=old.get("context_cards"))

        with self.session_lock(slug):
            # The three fields `create_session` cannot know, because they belong to the *legacy* session:
            # its original creation date, the request hash a migration may have to recover from the old
            # metadata when no request file survived, and an id derived from the slug so re-reading a
            # migrated session finds the identity a previous migration of it would have given.
            meta = self.read_meta(slug)
            meta.session_id = uuid.uuid5(uuid.NAMESPACE_URL, f"requivo:legacy:{slug}").hex
            meta.created_at = old.get("created_at", meta.created_at)
            meta.request_hash = req_hash
            self.write_meta(slug, meta)

            rev, _ = self.save_revision(slug, model, expected_revision=0)  # existing model → revision 1

            filename_to_type = {fn: t for t, fn in ARTIFACT_FILES.items() if fn}
            for fn, atype in filename_to_type.items():
                legacy_file = src / fn
                if legacy_file.exists():
                    content = legacy_file.read_text(encoding="utf-8")
                    self.save_session_artifact(slug, atype, fn, content, source_revision=rev)
            return self.read_meta(slug)




def _default_store() -> Store:
    """A fresh `Store` resolved from the ambient workspace root, built again on every call. This is
    what keeps every module-level function below behaving byte-identically to before this class
    existed: `workspace_root()` reads `REQUIVO_WORKSPACE`/cwd afresh each time, so a CLI `--workspace`
    env mutation mid-process (`cli.py`) is picked up by the very next call, exactly as it was when
    these functions read the root directly. See `Store`'s own docstring and
    `docs/cloud-boundary.md` (§3.1)."""
    return Store(workspace_root())


# Ambient-default wrappers over `Store`'s own root methods (#272), rather than a bare re-import from
# `paths.py`: three of these four are `Store` computing the identical path from an explicit root, so
# re-importing would silently diverge from `Store`'s own math the moment one was edited without the
# other. Wrapping via `_default_store()` keeps exactly one definition of what these paths are.
# `output_root` is the one exception -- see `Store.output_root` for why.
def store_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.store_root`."""
    return _default_store().store_root()


def session_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.session_root`."""
    return _default_store().session_root()


def lock_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.lock_root`."""
    return _default_store().lock_root()


def debug_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.debug_root`."""
    return _default_store().debug_root()


def output_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.output_root`."""
    return _default_store().output_root()


# What `.requivo/.gitignore` is written with. `*` ignores the directory's whole contents including
# the ignore file itself -- the self-ignoring pattern `uv` writes into `.venv/` and terraform into
# `.terraform/`, chosen so nothing has to be added to the *user's* `.gitignore`, which is a file
# Requivo has no business editing.
_STORE_GITIGNORE = """\
# Written by Requivo the first time this directory was created, and never rewritten.
# Sessions hold your request text verbatim -- for most users that is client-confidential
# material sitting inside a git repository. Delete this file to commit sessions deliberately;
# it will not come back. To share one session instead, use `requivo session export`.
*
"""


def ensure_store_dir(path: Path) -> Path:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh, from
    `paths.workspace_root()`, on every call. Full contract on `Store.ensure_store_dir`, which this
    delegates to; see `Store`'s own docstring and `docs/cloud-boundary.md` (§3.1) for why the root
    is resolved this way rather than read off `self`."""
    return _default_store().ensure_store_dir(path)


def no_session_message(ref: str, *, what: str = "session") -> str:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.no_session_message`, which this delegates to."""
    return _default_store().no_session_message(ref, what=what)


def lock_path(slug: str) -> Path:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.lock_path`, which this delegates to."""
    return _default_store().lock_path(slug)


@contextmanager
def session_lock(slug: str) -> Iterator[_LockHandle]:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh, from
    `paths.workspace_root()`, on every call. Full contract, including the re-entrancy keying, on
    `Store.session_lock`, which this delegates to."""
    with _default_store().session_lock(slug) as handle:
        yield handle   # forwarded, not swallowed: the annotation above is the contract, not decoration


def _child_of(root: Path, slug: str) -> Path:
    """`root / slug`, having validated the slug and confirmed the result is genuinely a child of
    `root` — the defence-in-depth check the traversal guard is built around. `is_contained` carries
    the reasoning for both halves of that confirmation, and for why it is one function.

    **The reserved-device-name refusal is conditional here, and `validate_slug` itself stays
    unconditional** (#372) -- `_refuse_new_reserved_slug` carries the split. A genuinely new reserved
    slug is still refused, since `canonical_dir` is what `create_session` calls
    (`test_reserved_windows_device_names_are_refused_as_slugs`); what changes is a name a session
    already occupies, which reads through."""
    slug = _slug_shape(slug)
    d = root / slug
    _refuse_new_reserved_slug(slug, d)
    if not is_contained(d, root):
        raise InvalidSlugError(f"slug {slug!r} does not resolve to a path inside the session root",
                               details={"slug": slug})
    return d


def canonical_dir(slug: str) -> Path:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.canonical_dir`, which this delegates to."""
    return _default_store().canonical_dir(slug)


def legacy_dir(slug: str) -> Path:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.legacy_dir`, which this delegates to."""
    return _default_store().legacy_dir(slug)


def artifact_path(slug: str, filename: str) -> Path:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract, including why display-only callers come through here, on `Store.artifact_path`."""
    d = canonical_dir(slug) / "artifacts"
    p = d / validate_filename(filename)
    if not is_contained(p, d):
        raise InvalidFilenameError(
            f"artifact filename {filename!r} does not resolve to a path inside {d}",
            details={"slug": slug, "filename": filename})
    return p




def session_exists(slug: str) -> bool:
    """Ambient-default wrapper (#272) -- see `Store.session_exists`."""
    return _default_store().session_exists(slug)


def legacy_exists(slug: str) -> bool:
    """Ambient-default wrapper (#272) -- see `Store.legacy_exists`."""
    return _default_store().legacy_exists(slug)


def write_meta(slug: str, meta: SessionMeta) -> Path:
    """Ambient-default wrapper (#272) -- see `Store.write_meta`."""
    return _default_store().write_meta(slug, meta)


# Keys a past Requivo wrote (or declared) and no longer means anything. `extra="allow"` preserves
# every unknown key, which is right for a key from the *future* and wrong for one from the past — so
# retirement is explicit here, in the migration, rather than implicit in the model config.


def read_meta(slug: str) -> SessionMeta:
    """Ambient-default wrapper (#272) -- see `Store.read_meta`."""
    return _default_store().read_meta(slug)


def create_session(slug: str, request: str, *, provider: str | None = None,
                   model_name: str | None = None, context_cards: list[str] | None = None) -> SessionMeta:
    """Ambient-default wrapper (#272) -- see `Store.create_session`."""
    return _default_store().create_session(
        slug, request, provider=provider, model_name=model_name, context_cards=context_cards)


def delete_session(slug: str) -> None:
    """Ambient-default wrapper (#272) -- see `Store.delete_session`."""
    return _default_store().delete_session(slug)


def save_revision(slug: str, model: EngineOutput, *, expected_revision: int | None = None,
                  provenance: dict | None = None) -> tuple[int, SessionMeta]:
    """Ambient-default wrapper (#272) -- see `Store.save_revision`."""
    return _default_store().save_revision(
        slug, model, expected_revision=expected_revision, provenance=provenance)


def load_session_model(slug: str) -> EngineOutput:
    """Ambient-default wrapper (#272) -- see `Store.load_session_model`."""
    return _default_store().load_session_model(slug)


def load_revision_model(slug: str, revision: int) -> EngineOutput:
    """Ambient-default wrapper (#272) -- see `Store.load_revision_model`."""
    return _default_store().load_revision_model(slug, revision)


def session_request(slug: str) -> str:
    """Ambient-default wrapper (#272) -- see `Store.session_request`."""
    return _default_store().session_request(slug)


def save_session_artifact(slug: str, artifact_type: str, filename: str, content: str,
                          source_revision: int, *, stale: bool = False) -> ArtifactStatus:
    """Ambient-default wrapper (#272) -- see `Store.save_session_artifact`."""
    return _default_store().save_session_artifact(
        slug, artifact_type, filename, content, source_revision, stale=stale)


def write_artifact_file(slug: str, filename: str, content: str) -> Path:
    """Ambient-default wrapper (#272) -- see `Store.write_artifact_file`."""
    return _default_store().write_artifact_file(slug, filename, content)


def read_artifact_file(slug: str, filename: str) -> Optional[str]:
    """Ambient-default wrapper (#272) -- see `Store.read_artifact_file`."""
    return _default_store().read_artifact_file(slug, filename)


# How much of a non-session directory's contents is worth carrying into a report. A lock ghost holds
# one entry; a half-extracted archive can hold thousands, and a diagnostic that prints all of them
# stops being read at all. Five is enough to tell those two apart on sight, which is the whole job.
# The true total travels beside the sample, so a truncated list can never be mistaken for the whole
# of what is there.


def _scan_session_root() -> tuple[list[str], list[Path], list[UnexaminableEntry]]:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store._scan_session_root`, which this delegates to."""
    return _default_store()._scan_session_root()


def list_session_slugs() -> list[str]:
    """Ambient-default wrapper (#272) -- see `Store.list_session_slugs`."""
    return _default_store().list_session_slugs()




def scan_session_root() -> tuple[list[str], list[NonSessionEntry], list[UnexaminableEntry]]:
    """Ambient-default wrapper (#272) -- see `Store.scan_session_root`."""
    return _default_store().scan_session_root()


def list_unexaminable_entries() -> list[UnexaminableEntry]:
    """Ambient-default wrapper (#272) -- see `Store.list_unexaminable_entries`."""
    return _default_store().list_unexaminable_entries()


def scan_lock_root() -> tuple[list[str], list[str], list[UnexaminableEntry]]:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.scan_lock_root`, which this delegates to."""
    return _default_store().scan_lock_root()


def migrate_legacy(slug: str) -> SessionMeta:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.migrate_legacy`, which this delegates to."""
    return _default_store().migrate_legacy(slug)
