"""The canonical session store: `Store` (composing `_ScanMixin` and `_LockMixin`), session and
revision CRUD, and the ambient-default module-level wrappers over it (#550). Public names are
re-exported from `core/persistence/__init__.py`.
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
from requivo.core.perimeters import resolve_perimeter
from requivo.core.persistence.atomic import _atomic_write
from requivo.core.persistence.identifiers import (
    _probe,
    _refuse_new_reserved_slug,
    _slug_shape,
    _stat_exists,
    validate_filename,
)
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
    """One workspace's `.requivo/` layout, addressed by an explicit `root` (#272). The module-level
    functions below wrap a freshly-resolved default instance per call, which keeps `--workspace`/
    `REQUIVO_WORKSPACE` behaviour intact. Root identity, not object identity, keys lock re-entrancy:
    `test_two_roots_sharing_a_slug_do_not_share_a_lock`,
    `test_reentrant_acquisition_across_fresh_ambient_stores_is_still_recognised`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # Resolved once, not per acquisition, or a repointed ancestor could block the process against its
        # own lock (#272). `test_lock_key_resolves_the_root_once_at_construction_not_per_acquisition`.
        self._root_key = str(_resolve(root))

    # ── roots, bound to self.root instead of the process ──


    def store_root(self) -> Path:
        return self.root / ".requivo"

    def session_root(self) -> Path:
        return self.store_root() / "sessions"

    def lock_root(self) -> Path:
        return self.store_root() / "locks"

    def debug_root(self) -> Path:
        return self.store_root() / "debug"

    def output_root(self) -> Path:
        """The retired `out/` root, deliberately NOT derived from `self.root`: `REQUIVO_OUTPUT_DIR`/cwd
        is a knob independent of the workspace (#272).
        `test_an_explicit_stores_legacy_root_still_honours_the_ambient_output_dir_override`."""
        return _ambient_output_root()


    # ── session CRUD ──

    def ensure_store_dir(self, path: Path) -> Path:
        """`mkdir(parents=True, exist_ok=True)` under `.requivo/`, writing the privacy `.gitignore` on
        the call that creates the root (#211). Every directory creation under the store goes through
        here (`test_no_store_directory_is_created_outside_ensure_store_dir`); the marker is written once
        and never restored (`test_the_privacy_gitignore_is_written_once_and_never_restored`); the trigger
        is `mkdir` winning, not `exists()`, and a failed marker write removes the root again (#320):
        `test_a_failed_marker_write_leaves_no_root_behind_to_suppress_the_next_attempt`."""
        root = self.store_root()
        fresh = True
        try:
            root.mkdir(parents=True)
        except FileExistsError:
            # Somebody else owns the root; losing this race is success.
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
        """The one sentence for "there is no such session" (#243): the reference, where Requivo looked,
        and how to list what is there. A method because the root is this store's, read at refusal time.
        `what` widens the noun for `_resolve_ref`. `display_token` because `ref` is raw argv (#40,
        invariant 14): `test_the_shared_builder_escapes_a_reference_it_could_be_handed_directly`."""
        return (f"no {what} named {display_token(ref)} under {self.session_root()}. "
                "`requivo session list` shows the sessions in this workspace; a different --workspace "
                "(or REQUIVO_WORKSPACE) changes where Requivo looks.")


    def _no_session(self, slug: str) -> SessionNotFoundError:
        """The one refusal for a missing session, shared by the lock and the metadata read."""
        return SessionNotFoundError(self.no_session_message(slug), details={"slug": slug})


    def canonical_dir(self, slug: str) -> Path:
        """The canonical session directory `<workspace>/.requivo/sessions/<slug>/`."""
        return _child_of(self.session_root(), slug)


    def legacy_dir(self, slug: str) -> Path:
        """The legacy `out/<slug>/` directory: read-only, migrated only by `session migrate`."""
        return _child_of(self.output_root(), slug)


    def artifact_path(self, slug: str, filename: str) -> Path:
        """`<session>/artifacts/<filename>` with both halves validated: the single chokepoint every
        artifact read, write and *display* goes through (#5, #23, #40). A recorded filename is untrusted
        input (invariant 14); `session import` re-validates each one (`test_a_crafted_artifact_filename_cannot_be_used_to_probe_for_files_outside_the_session`).
        A target that is not there is not an error here."""
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
        # Through `_probe`, not `p.exists()` (#264): `EACCES` must become `SessionUnreadableError`, not escape.
        if not _probe(p, slug):
            raise self._no_session(slug)
        try:
            return migrate_session(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            raise SessionUnreadableError(f"session '{slug}' has an unreadable session.json: {e}",
                                         details={"slug": slug}) from e


    def create_session(self, slug: str, request: str, *, provider: str | None = None,
                       model_name: str | None = None, context_cards: list[str] | None = None,
                       perimeter: str | None = None) -> SessionMeta:
        """Create a fresh session directory from a request, no model yet (revision 0). `perimeter`
        (#608) is frozen here as half of the identity; `None` writes none and reads as software.
        Assembled in a staging directory and moved in with one rename, which is the claim on the
        slug (invariant 11): an existence check is not atomic, and a directory created before its
        metadata is a session a reader can find with no `session.json`."""
        if perimeter is not None:
            resolve_perimeter(perimeter)  # refused by name here, not left for the next reader
        now = _now()
        meta = SessionMeta(
            session_id=uuid.uuid4().hex, slug=slug, created_at=now, updated_at=now,
            provider=provider, model_name=model_name, context_cards=context_cards,
            request_hash=content_hash(request), perimeter=perimeter,
        )
        d = self.canonical_dir(slug)
        self.ensure_store_dir(d.parent)
        # Dot-prefixed, so a staging directory can never be mistaken for a session.
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
        """Irreversibly remove a session: its directory and its lock file (#238). The removal runs
        inside the critical section (invariant 9), and the lock file is unlinked while the lock is
        still held, or a re-creation could mint a second inode under the name (#22):
        `test_the_lock_file_is_gone_before_the_lock_is_released_not_after`. Windows refuses that
        unlink, so only it defers to `unlink_on_release` (#469):
        `test_a_delete_whose_in_lock_unlink_is_refused_still_removes_the_lock_file`."""
        with self.session_lock(slug) as lock:
            shutil.rmtree(self.canonical_dir(slug))
            try:
                self.lock_path(slug).unlink(missing_ok=True)
            except OSError:
                # The in-lock unlink has no window; a platform that refuses it gets the next-smallest.
                lock.unlink_on_release()


    def save_revision(self, slug: str, model: EngineOutput, *, expected_revision: int | None = None,
                      provenance: dict | None = None) -> tuple[int, SessionMeta]:
        """Persist a new model revision: freeze `revisions/NNNN-model.json`, replace `model.json`,
        record provenance, bump `current_revision` and `updated_at`. Returns `(new_revision, meta)`.
        `expected_revision` is the optimistic-locking precondition, raising `RevisionConflictError`,
        and it is held across the writes it authorises under `session_lock` (invariant 9)."""
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
            # Frozen revision first, then model.json: the order decides what a crash between them leaves.
            # `test_a_crash_after_the_first_payload_write_still_reads_as_the_recorded_revision`.
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
        if not _stat_exists(p):
            raise SessionNotFoundError(
                f"session '{slug}' has no model yet (apply a proposal first)", details={"slug": slug})
        # The session's own perimeter; `read_meta` already refused an unknown one by name (#608).
        perimeter = resolve_perimeter(self.read_meta(slug).perimeter)
        return _read_model(p, slug=slug, perimeter=perimeter)


    def load_revision_model(self, slug: str, revision: int) -> EngineOutput:
        """A historical model revision — the basis for `impact` since a given point."""
        p = self.canonical_dir(slug) / "revisions" / f"{revision:04d}-model.json"
        if not _stat_exists(p):
            raise SessionNotFoundError(
                f"session '{slug}' has no revision {revision}", details={"slug": slug, "revision": revision})
        perimeter = resolve_perimeter(self.read_meta(slug).perimeter)
        return _read_model(p, slug=slug, revision=revision, perimeter=perimeter)


    def session_request(self, slug: str) -> str:
        p = self.canonical_dir(slug) / "request.md"
        return p.read_text(encoding="utf-8") if _stat_exists(p) else ""


    def save_session_artifact(self, slug: str, artifact_type: str, filename: str, content: str,
                              source_revision: int, *, stale: bool = False) -> ArtifactStatus:
        """Write an artifact under `artifacts/` and record its source revision in `session.json`. A
        revision outside the session's history is refused rather than recorded; `stale` is decided by
        the caller (`ArtifactService.save`); `filename` is validated before the lock is taken."""
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
        """Write a raw file into `artifacts/` with no status tracking (the epic exports). Both halves
        of the target go through `artifact_path`."""
        path = self.artifact_path(slug, filename)
        self.ensure_store_dir(path.parent)
        return _atomic_write(path, content)


    def read_artifact_file(self, slug: str, filename: str) -> Optional[str]:
        """The content of a file under `artifacts/`, through `artifact_path`. An unsafe `filename`
        raises; only a missing file returns None. Decoded as UTF-8 (invariant 16)."""
        p = self.artifact_path(slug, filename)
        return p.read_text(encoding="utf-8") if p.exists() else None


    def migrate_legacy(self, slug: str) -> SessionMeta:
        """Copy a legacy `out/<slug>/` session into the canonical store, preserving the original:
        the model becomes revision 1 and known artifacts are recorded against it. The claim on the
        slug is `create_session`'s rename (invariant 11), and everything after it runs under one
        `session_lock` with `expected_revision=0` (invariant 9)."""
        from requivo.core.dependencies import ARTIFACT_FILENAMES  # local import avoids a load-time cycle

        src = self.legacy_dir(slug)
        if not _stat_exists(src / "model.json"):
            raise SessionNotFoundError(f"no legacy session '{slug}' under {self.output_root()}",
                                       details={"slug": slug})
        request = ""
        for name in ("request.md", "request.txt"):
            if _stat_exists(src / name):
                request = (src / name).read_text(encoding="utf-8")
                break
        old: dict = {}
        if (src / "session.json").exists():
            try:
                old = json.loads((src / "session.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old = {}
        # Parsed before claiming the slug, through `_read_model` like the other doors (#204); no
        # `slug=`, since the legacy layout has no `revisions/` to name in a remedy.
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
            # The three fields that belong to the *legacy* session: its creation date, its request hash
            # and an id derived from the slug.
            meta = self.read_meta(slug)
            meta.session_id = uuid.uuid5(uuid.NAMESPACE_URL, f"requivo:legacy:{slug}").hex
            meta.created_at = old.get("created_at", meta.created_at)
            meta.request_hash = req_hash
            self.write_meta(slug, meta)

            rev, _ = self.save_revision(slug, model, expected_revision=0)  # existing model → revision 1

            filename_to_type = {fn: t for t, fn in ARTIFACT_FILENAMES.items()}
            for fn, atype in filename_to_type.items():
                legacy_file = src / fn
                if _stat_exists(legacy_file):
                    content = legacy_file.read_text(encoding="utf-8")
                    self.save_session_artifact(slug, atype, fn, content, source_revision=rev)
            return self.read_meta(slug)




def _default_store() -> Store:
    """A fresh `Store` over the ambient workspace root, rebuilt on every call so a mid-process
    `--workspace` mutation is picked up by the next call (#272)."""
    return Store(workspace_root())


# Ambient-default wrappers over `Store`'s root methods (#272), so there is one definition of each path.
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


# `*` ignores the directory's whole contents, the ignore file included, so the user's `.gitignore` is never edited.
_STORE_GITIGNORE = """\
# Written by Requivo the first time this directory was created, and never rewritten.
# Sessions hold your request text verbatim -- for most users that is client-confidential
# material sitting inside a git repository. Delete this file to commit sessions deliberately;
# it will not come back. To share one session instead, use `requivo session export`.
*
"""


def ensure_store_dir(path: Path) -> Path:
    """Ambient-default wrapper (#272) -- see `Store.ensure_store_dir`."""
    return _default_store().ensure_store_dir(path)


def no_session_message(ref: str, *, what: str = "session") -> str:
    """Ambient-default wrapper (#272) -- see `Store.no_session_message`."""
    return _default_store().no_session_message(ref, what=what)


def lock_path(slug: str) -> Path:
    """Ambient-default wrapper (#272) -- see `Store.lock_path`."""
    return _default_store().lock_path(slug)


@contextmanager
def session_lock(slug: str) -> Iterator[_LockHandle]:
    """Ambient-default wrapper (#272) -- see `Store.session_lock`."""
    with _default_store().session_lock(slug) as handle:
        yield handle   # forwarded, not swallowed: the annotation above is the contract, not decoration


def _child_of(root: Path, slug: str) -> Path:
    """`root / slug`, validated and confirmed a genuine child of `root` (`is_contained`). The
    reserved-device-name refusal is conditional here and unconditional in `validate_slug` (#372):
    `test_reserved_windows_device_names_are_refused_as_slugs`."""
    slug = _slug_shape(slug)
    d = root / slug
    _refuse_new_reserved_slug(slug, d)
    if not is_contained(d, root):
        raise InvalidSlugError(f"slug {slug!r} does not resolve to a path inside the session root",
                               details={"slug": slug})
    return d


def canonical_dir(slug: str) -> Path:
    """Ambient-default wrapper (#272) -- see `Store.canonical_dir`."""
    return _default_store().canonical_dir(slug)


def legacy_dir(slug: str) -> Path:
    """Ambient-default wrapper (#272) -- see `Store.legacy_dir`."""
    return _default_store().legacy_dir(slug)


def artifact_path(slug: str, filename: str) -> Path:
    """Ambient-default wrapper (#272) -- see `Store.artifact_path`."""
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


# Keys a past Requivo wrote and no longer means anything; `extra="allow"` keeps the future's keys,
# so the past's are retired explicitly here.


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


# How many of a non-session directory's entries a report carries; the true total travels beside the sample.


def _scan_session_root() -> tuple[list[str], list[Path], list[UnexaminableEntry]]:
    """Ambient-default wrapper (#272) -- see `Store._scan_session_root`."""
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
    """Ambient-default wrapper (#272) -- see `Store.scan_lock_root`."""
    return _default_store().scan_lock_root()


def migrate_legacy(slug: str) -> SessionMeta:
    """Ambient-default wrapper (#272) -- see `Store.migrate_legacy`."""
    return _default_store().migrate_legacy(slug)
