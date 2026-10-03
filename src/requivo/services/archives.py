"""The portable session archive (#101, #219, #702): its format, its integrity checks and its closed
refusal vocabulary, implemented once. `SessionService.export_archive`/`import_archive` reach it against
their own repository; `requivo session export/import` are thin wrappers over those. File-backed only:
the archive is a session directory's zip.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Optional

from requivo.core.errors import (
    ImportDestinationOccupiedError,
    ImportMoveFailedError,
    InconsistentArchiveError,
    InvalidArchiveError,
    SessionExistsError,
    SessionNotFoundError,
    SessionUnreadableError,
    UnreadableArchiveError,
    UnsupportedRepositoryError,
)
from requivo.core.integrity import check_session_dir
from requivo.core.persistence import SessionMeta, Store, validate_slug
from requivo.core.selectors import display_token
from requivo.services.repository import SessionRepository

# Ceilings for an imported archive, so a hostile or corrupt one fails on a bound, not on the filesystem.
MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
# Both caps above count files alone; this bounds every entry, directories included, before either runs.
# `test_import_refuses_an_archive_bounded_by_files_and_bytes_but_not_by_directory_entries`.
MAX_ARCHIVE_ENTRIES = MAX_ARCHIVE_FILES * 4


def file_store(repo: SessionRepository, operation: str) -> Store:
    """The `Store` under `repo`, or `unsupported_repository`; never the ambient workspace (#272).
    `test_a_repository_with_no_session_directory_refuses_the_archive_by_name`."""
    get_store = getattr(repo, "store", None)
    found = get_store() if callable(get_store) else None
    if not isinstance(found, Store):
        name = type(repo).__name__
        raise UnsupportedRepositoryError(
            f"{operation} needs a file-backed repository; {name} has no session directory to archive",
            details={"repository": name, "operation": operation})
    return found


def export_archive(repo: SessionRepository, store: Store, slug: str) -> bytes:
    """Zip a session directory under its lock, complete or not at all; `.lock` and scratch files are
    excluded. `test_export_excludes_the_lock_file_and_waits_for_the_writer`."""
    d = store.canonical_dir(slug)
    buf = io.BytesIO()
    # The zip closes before the lock is released: the archive is one instant of the session.
    with repo.lock(slug), zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(d.rglob("*")):
            if f.is_file() and not any(part.startswith(".") for part in f.relative_to(d).parts):
                z.write(f, f.relative_to(d.parent))
    return buf.getvalue()


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
    return validate_slug(slug), all_infos


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


def import_archive(repo: SessionRepository, store: Store, data: bytes | BinaryIO | Path, *,
                   force: bool, name: str) -> tuple[SessionMeta, bool]:
    """Inspect → extract to scratch → validate → move into place, under `store`; nothing lands until
    the whole archive has been checked. Returns the landed session and whether it replaced one."""
    if isinstance(data, Path) and not data.is_file():
        raise SessionNotFoundError(f"archive not found: {display_token(name)}", details={"archive": name})
    if not isinstance(data, (bytes, bytearray, Path)) and not data.seekable():
        # zipfile reads the central directory from the end; refused by name, not as "not a zip file".
        raise UnreadableArchiveError(
            f"{display_token(name)} is a stream that cannot seek, and a .zip is read from its end -- pass "
            "bytes, a path, or a seekable stream", details={"archive": name})
    root = store.session_root()
    # `ensure_store_dir`, not `mkdir`: import can create `.requivo/` and must write the privacy
    # `.gitignore` (#211). `test_import_into_a_fresh_workspace_writes_the_privacy_gitignore`.
    store.ensure_store_dir(root)
    try:
        z = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
    except (zipfile.BadZipFile, OSError) as e:
        raise UnreadableArchiveError(f"{display_token(name)} is not a readable .zip archive: {e}",
                                     details={"archive": name}) from e
    with z:
        slug, entries = _inspect_archive(z)
        # Decided once, before the unzip, and never re-asked (invariant 9): a session created in the
        # window is refused, not destroyed (#101, #111).
        # `test_a_session_created_during_the_extraction_window_is_refused_not_destroyed`.
        occupied = repo.exists(slug)
        if occupied and not force:
            raise SessionExistsError(
                f"session '{slug}' already exists in this workspace — pass --force to replace it",
                details={"slug": slug})
        # Scratch beside the store, not inside it: same filesystem, never visible to `session list`.
        scratch = Path(tempfile.mkdtemp(prefix=".import-", dir=root.parent))
        try:
            # Exactly the entry list `_inspect_archive` validated, never a fresh `infolist()` (#219).
            try:
                for info in entries:
                    z.extract(info, scratch)
            except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as e:
                # Intact directory, member data it cannot read (corrupt, encrypted, unsupported method):
                # nothing has landed yet (#647, `test_import_refuses_an_archive_whose_member_data_is_corrupt`).
                raise UnreadableArchiveError(f"{display_token(name)} has a member it cannot extract: {e}",
                                             details={"archive": name}) from e
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
    return repo.read_meta(slug), occupied
