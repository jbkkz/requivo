"""`requivo session export/import/restore`: the archive verbs (#550). The transient-rename retry
they share with the store is `core/persistence/atomic.py`'s.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from requivo.core import persistence as store
from requivo.core.errors import (
    ImportDestinationOccupiedError,
    ImportMoveFailedError,
    InconsistentArchiveError,
    InvalidArchiveError,
    InvalidModelError,
    ModelUnreadableError,
    SessionExistsError,
    SessionNotFoundError,
    SessionUnreadableError,
    UnreadableArchiveError,
)
from requivo.core.integrity import check_session_dir, newest_readable_revision, readable_revision
from requivo.core.perimeters import resolve_perimeter
from requivo.core.persistence import _replace_with_retry, ensure_store_dir
from requivo.core.selectors import display_token
from requivo.deterministic._shared import print_json
from requivo.paths import session_root
from requivo.services.repository import SessionRepository
from requivo.services.sessions import SessionService


def _cmd_session_export(a, client) -> None:
    """Archive a session as a .zip, under its lock, complete or not at all; `.lock` and scratch files
    are excluded, and the zip is renamed into place. `test_export_excludes_the_lock_file_and_waits_for_the_writer`.
    The default filename's reserved-stem overlap: `decision: reserved-stem-export-filenames-are-not-a-live-gap`."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    # Direct, legitimately: this verb archives the session's *directory*.
    d = store.canonical_dir(slug)
    dest = Path(a.output) if a.output else Path.cwd() / f"{slug}.requivo.zip"
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    try:
        with svc.repo.lock(slug):
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(d.rglob("*")):
                    if f.is_file() and not any(part.startswith(".") for part in f.relative_to(d).parts):
                        z.write(f, f.relative_to(d.parent))
        _replace_with_retry(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    if a.json:
        print_json({"slug": slug, "archive": str(dest)})
        return
    print(f"Exported session '{slug}' → {dest}")


def _cmd_session_restore(a, client) -> None:
    """Copy a readable `revisions/NNNN-model.json` over `model.json`: the explicit repair for a torn
    session (#210). Consent, not automation; the revision history is untouched. A candidate is trusted
    only when its `content_hash` matches the recorded `model_hash` (an empty record is unconfirmed);
    a named `--revision` is refused, never substituted, and only the default searches for the newest
    trusted one. No `--json` and no repository seam, both scoped out deliberately."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    with svc.repo.lock(slug):
        meta = svc.meta(slug)
        n = meta.current_revision
        if n <= 0:
            raise InvalidModelError(
                f"session '{slug}' has no applied revision to restore from (current_revision is {n})",
                details={"slug": slug, "current_revision": n})
        hashes = {r.revision: r.model_hash for r in meta.revisions}
        d = store.canonical_dir(slug)
        if a.revision is not None:
            target_rev = a.revision
            if not 1 <= target_rev <= n:
                raise ModelUnreadableError(
                    f"session '{slug}' has revisions 1..{n}; {target_rev} is out of range",
                    details={"slug": slug, "revision": target_rev, "current_revision": n})
            found = readable_revision(d, target_rev, expected_hashes=hashes,
                                       perimeter=resolve_perimeter(meta.perimeter))
            if found is None:
                raise ModelUnreadableError(
                    f"revisions/{target_rev:04d}-model.json is missing, does not parse, or does not "
                    "match the hash session.json recorded for it -- refusing to restore from it",
                    details={"slug": slug, "revision": target_rev})
            payload = found.payload
        else:
            found = newest_readable_revision(d, n, expected_hashes=hashes,
                                              perimeter=resolve_perimeter(meta.perimeter))
            if found is None:
                raise ModelUnreadableError(
                    f"session '{slug}' has no readable, trusted revision file (1..{n}) to restore "
                    "from",
                    details={"slug": slug, "current_revision": n})
            target_rev, payload = found.revision, found.payload
        model_path = d / "model.json"
        # Temp file + rename, through `_replace_with_retry` for a transient Windows lock (#524).
        tmp = model_path.with_name(f".{model_path.name}.{os.getpid()}.restore.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            _replace_with_retry(tmp, model_path)
        finally:
            tmp.unlink(missing_ok=True)
    print(f"✅ Restored model.json for '{display_token(slug)}' from revision {target_rev}.")
    print("  The revision history is untouched — this is not a new revision.")
    if target_rev != n:
        print(f"  This is a partial repair: revision {n}'s own content could not be read or "
              f"trusted and cannot be recovered. `session verify` will keep reporting this session "
              f"as inconsistent, correctly.")
    print(f"  requivo session verify {display_token(slug)}")




# Ceilings for an imported archive, so a hostile or corrupt one fails on a bound, not on the filesystem.
MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
# Both caps above count files alone; this bounds every entry, directories included, before either runs.
# `test_import_refuses_an_archive_bounded_by_files_and_bytes_but_not_by_directory_entries`.
MAX_ARCHIVE_ENTRIES = MAX_ARCHIVE_FILES * 4




def _inspect_archive(z: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
    """Validate an export archive before anything is written; returns the single slug it contains and
    the exact entry list validated, which the caller extracts and only that (#219). Raises
    `InvalidArchiveError` with `details["problem"]` (#101). Names are decomposed into components,
    never prefix-matched: `test_import_refuses_unsafe_entries`."""
    all_infos = z.infolist()
    if len(all_infos) > MAX_ARCHIVE_ENTRIES:
        raise InvalidArchiveError(
            f"the archive holds {len(all_infos)} entries (files and directories); the maximum is "
            f"{MAX_ARCHIVE_ENTRIES}",
            details={"problem": "too_many_entries",
                     "entries": len(all_infos), "max_entries": MAX_ARCHIVE_ENTRIES})
    infos = [i for i in all_infos if not i.is_dir()]
    if not infos:
        raise InvalidArchiveError("the archive contains no files", details={"problem": "empty"})
    if len(infos) > MAX_ARCHIVE_FILES:
        raise InvalidArchiveError(
            f"the archive holds {len(infos)} files; the maximum is {MAX_ARCHIVE_FILES}",
            details={"problem": "too_many_files",
                     "files": len(infos), "max_files": MAX_ARCHIVE_FILES})
    total = sum(i.file_size for i in infos)
    if total > MAX_ARCHIVE_BYTES:
        raise InvalidArchiveError(
            f"the archive expands to {total} bytes; the maximum is {MAX_ARCHIVE_BYTES}",
            details={"problem": "too_large",
                     "bytes": total, "max_bytes": MAX_ARCHIVE_BYTES})

    slugs = set()
    for i in infos:
        name = i.filename
        if "\\" in name:  # a Windows-style separator is not a component boundary to zipfile
            raise InvalidArchiveError(f"unsafe path in archive: {name!r}",
                                      details={"problem": "unsafe_entry", "entry": name})
        parts = PurePosixPath(name).parts
        if len(parts) < 2:
            raise InvalidArchiveError(
                f"archive entry {name!r} is not inside a session directory; an export contains "
                "<slug>/session.json and friends",
                details={"problem": "entry_outside_session_directory", "entry": name})
        if any(p in ("", ".", "..") for p in parts) or PurePosixPath(name).is_absolute():
            raise InvalidArchiveError(f"unsafe path in archive: {name!r}",
                                      details={"problem": "unsafe_entry", "entry": name})
        slugs.add(parts[0])

    if len(slugs) != 1:
        # `display_token` per name: raw archive text, the one arm on this path that needs it (#40, #98).
        shown = ", ".join(display_token(s) for s in sorted(slugs))
        raise InvalidArchiveError(
            f"the archive holds {len(slugs)} session directories ({shown}); "
            "import takes exactly one",
            details={"problem": "multiple_sessions", "slugs": sorted(slugs)})
    slug = slugs.pop()
    # The directory name becomes a slug, so it faces the same validation; direct, since no session exists yet.
    return store.validate_slug(slug), all_infos




def _swap_in(extracted: Path, target: Path, slug: str, repo: SessionRepository) -> None:
    """Replace an existing session directory with a freshly extracted one, reversibly: the old one
    steps aside first and dies only once the new one is in place. Only called under `--force` for a
    session that exists (#111), and under `repo.lock`, which lives outside the session (#113,
    invariant 9), so a concurrent `save_revision` cannot recreate the slug between the two renames.
    `test_a_forced_import_serialises_against_a_concurrent_writer`."""
    with repo.lock(slug):
        backup = target.with_name(f".{target.name}.replaced-{os.getpid()}")
        target.replace(backup)
        try:
            extracted.replace(target)
        except OSError as e:
            backup.replace(target)
            raise ImportMoveFailedError(
                f"could not move the imported session into place: {e}"
                " — the session that was already here has been restored",
                details={"slug": slug}) from e
        shutil.rmtree(backup, ignore_errors=True)




def _refuse_a_non_session_destination(target: Path, slug: str, repo: SessionRepository,
                                      cause: Optional[BaseException] = None) -> None:
    """Refuse when something that is not a session occupies the slug's directory, by name on every
    platform (`os.replace` answers differently per platform, #114). It only ever refuses (the rename
    stays the claim, invariant 11) and is called on both sides of it. A probe that could not look says
    nothing and lets the rename decide; a real session is `SessionExistsError`, the caller's answer.
    `test_a_stray_directory_at_the_slug_is_refused_by_name_on_every_platform`,
    `test_a_stray_appearing_in_the_rename_window_is_named_rather_than_called_a_move_failure`."""
    try:
        if not (target.exists() or target.is_symlink()):
            return
        if repo.exists(slug):
            return
    except (OSError, SessionUnreadableError):
        return
    raise ImportDestinationOccupiedError(
        f"cannot import session '{slug}': {display_token(str(target))} already exists and is not a "
        "session — nothing was imported and nothing was removed. Move or delete it and import again; "
        "--force replaces a session and does not apply here.",
        details={"slug": slug, "path": str(target)}) from cause




def _validate_extracted(d: Path, slug: str) -> None:
    """Confirm an extracted directory is a *coherent* session, through `check_session_dir`, the
    standard a live session is held to; `notes` are not a reason to refuse.
    `test_a_future_artifact_type_survives_an_export_import_round_trip`."""
    problems = check_session_dir(d, expected_slug=slug)
    if problems:
        raise InconsistentArchiveError(
            f"the archive's session '{slug}' is not internally consistent: "
            + "; ".join(p.message for p in problems),
            details={"slug": slug, "problems": [p.to_dict() for p in problems]})




def _cmd_session_import(a, client) -> None:
    """Import a session archive: inspect → extract to scratch → validate → move into place. Nothing
    lands in the store until the whole archive has been checked."""
    archive = Path(a.archive)
    if not archive.is_file():
        raise SessionNotFoundError(f"archive not found: {display_token(str(archive))}",
                                   details={"archive": str(archive)})
    root = session_root()
    # `ensure_store_dir`, not `mkdir`: import can create `.requivo/` and must write the privacy
    # `.gitignore` (#211). `test_import_into_a_fresh_workspace_writes_the_privacy_gitignore`.
    ensure_store_dir(root)
    repo = SessionService().repo

    try:
        z = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as e:
        raise UnreadableArchiveError(f"{display_token(str(archive))} is not a readable .zip archive: {e}",
                                     details={"archive": str(archive)}) from e
    with z:
        slug, entries = _inspect_archive(z)
        # Decided once, before the unzip, and never re-asked (invariant 9): a session created in the
        # window is refused, not destroyed (#101, #111).
        # `test_a_session_created_during_the_extraction_window_is_refused_not_destroyed`.
        occupied = repo.exists(slug)
        if occupied and not a.force:
            raise SessionExistsError(
                f"session '{slug}' already exists in this workspace — pass --force to replace it",
                details={"slug": slug})
        # Scratch beside the store, not inside it: same filesystem, never visible to `session list`.
        scratch = Path(tempfile.mkdtemp(prefix=".import-", dir=root.parent))
        try:
            # Exactly the entry list `_inspect_archive` validated, never a fresh `infolist()` (#219).
            for info in entries:
                z.extract(info, scratch)
            extracted = scratch / slug
            _validate_extracted(extracted, slug)
            # Direct: the import moves a directory into place, so the destination *is* a path.
            target = store.canonical_dir(slug)
            if not occupied:
                # The slug was free at the check, so the rename is the claim (invariant 11): `os.replace`
                # refuses a non-empty destination on both platforms. A destination holding no session is
                # answered differently per platform (#114), hence the guard on both sides.
                # `test_that_window_refusal_names_the_conflict_rather_than_a_move_failure`.
                _refuse_a_non_session_destination(target, slug, repo)
                try:
                    extracted.replace(target)
                except OSError as e:
                    if repo.exists(slug):
                        raise SessionExistsError(
                            f"session '{slug}' was created while this archive was being read — "
                            "nothing was imported and nothing was replaced; pass --force to replace it",
                            details={"slug": slug}) from e
                    _refuse_a_non_session_destination(target, slug, repo, e)
                    raise ImportMoveFailedError(
                        f"could not move the imported session into place: {e}",
                        details={"slug": slug}) from e
            else:
                # `--force` against a session that is there: the swap holds the lock (#113). A concurrent
                # writer's in-flight work is lost cleanly, after it completes.
                _swap_in(extracted, target, slug, repo)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    if a.json:
        # `slug`/`path`, the spelling every sibling verb uses; `replaced` is the guard's own answer (#111).
        # `test_import_json_names_the_session_and_its_directory_the_way_its_siblings_do`.
        print_json({"slug": slug, "path": str(target), "replaced": occupied})
        return
    # Same as `session init`: the line's subject is where the session landed on this machine.
    print(f"Imported session '{slug}' → {store.canonical_dir(slug)}"
          + (" (replaced an existing session)" if occupied else ""))




