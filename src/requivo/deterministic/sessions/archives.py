"""`requivo session export/import/restore` -- the archive verbs.

Split out of `deterministic/sessions.py` by #550 (the lean pass, #548): the three verbs that move a
session's bytes across a boundary -- into a zip, out of one, or a revision back over `model.json`.
`_replace_with_retry` moved out of here entirely, to `core/persistence/atomic.py`, so this module
and the store share one transient-rename retry instead of two copies of it (#550's own direction).
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
from requivo.core.persistence import _replace_with_retry, ensure_store_dir
from requivo.core.selectors import display_token
from requivo.deterministic._shared import print_json
from requivo.paths import session_root
from requivo.services.repository import SessionRepository
from requivo.services.sessions import SessionService


def _cmd_session_export(a, client) -> None:
    """Archive a session as a .zip — under its lock, and complete or not at all.

    A session is a handful of files that must agree with each other, and reading them one by one
    while another surface applies a revision produces an archive that combines an old metadata with a
    new model -- internally inconsistent, and only discovered on import. So the read happens under the
    session lock, the same one every writer takes.

    `.lock` and the scratch files of an interrupted write are excluded: they are local artefacts of
    *this* machine's coordination, meaningless in an archive, and the lock file in particular would
    import as a session component. The archive itself is written beside its destination and renamed
    into place, so an interrupted export leaves no half-written .zip looking like a real one.
    `test_export_excludes_the_lock_file_and_waits_for_the_writer`.

    The default `<slug>.requivo.zip` destination's reserved-stem overlap with a refused-for-creation
    slug is not a live gap -- `decision: reserved-stem-export-filenames-are-not-a-live-gap`."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    # Direct, and legitimately so: this verb archives the session's *directory*. A path is the
    # subject of the command, not an implementation detail leaking through it.
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
    """Copy a readable `revisions/NNNN-model.json` over `model.json` -- the explicit, user-invoked
    repair for a torn or inconsistent session (#210).

    This is **consent, not automation**. `core/integrity.py`'s own module docstring states why
    `doctor` and `session verify` diagnose and never write ("it reports rather than raises... a
    caller decides what a problem means") -- that design does not bind this verb, whose name on the
    command line is the consent. It changes exactly one file.

    **The revision history is untouched.** This is not `model apply`/`save_revision` under a new
    name: no entry is appended to `session.json`'s revision log, `current_revision` does not move,
    and no new `revisions/NNNN-model.json` is written. Restoring is model.json catching up with a
    history that was already the truth, not a new fact about the session.

    **"Clean afterwards" is a claim about the ordinary case, not a guarantee** (found in review).
    When the restored revision *is* the last one, model.json's bytes now equal it byte for byte and
    `session verify`'s `model_is_not_the_last_revision` check passes. When it is not -- because the
    last revision's own file could not be read or trusted, and the search fell back to an older one
    -- restoring is still the right thing to do (a real, historical state beats a torn file), but
    `session verify` will keep reporting the session as inconsistent afterwards, correctly: the last
    revision's content is genuinely gone, and no copy of an older one changes that. The printed
    receipt below says which case this run was.

    **Trusted, not merely parseable** (found in review, closing the same gap `revision_hash_mismatch`
    exists to catch on the read side). A revision file edited by hand after being frozen still parses
    as a valid model; restoring from one without checking its hash would make this the one place in
    the store that trusts what `session verify` itself would refuse. Both paths below compare the
    candidate's `content_hash` against the hash `session.json`'s own revision log recorded for it
    (`SessionMeta.revisions[i].model_hash`) and refuse a mismatch exactly as they refuse a parse
    failure -- an empty recorded hash is unconfirmed rather than refused, the same tolerance
    `inspect_session_dir` gives a legacy record with none.

    **A named target is refused, never silently substituted.** `--revision N` that does not exist,
    whose file cannot be read, does not parse, or does not match its recorded hash, raises rather
    than quietly falling back to another revision -- silently repairing from something the caller did
    not ask for is the wrong kind of helpful in a tool whose whole job is a deliberate, auditable
    repair. Only the *default* (no `--revision`) searches for the newest revision this build can read
    and trust (`newest_readable_revision`, the same search `session verify`'s remedy line names), and
    says which one it picked.

    **No `--json`, and that is a scoping decision, not an oversight.** Every sibling verb in this
    module has one; this one does not, because `docs/compatibility.md`'s `--json` promise and its
    two guards (`test_every_json_verb_is_inside_the_promise`,
    `test_every_public_json_payload_keeps_its_recorded_top_level_shape`) are a heavier commitment
    than #210's own stated scope (`deterministic/sessions.py`, `docs/cli.md`, tests) asked for. The
    human output already states everything this verb does.

    **No repository seam for this, and that is scoped the same way.** `SessionRepository.save_revision`
    always advances the revision log; there is no protocol method for "replace model.json and touch
    nothing else", and `core/persistence.py`/`services/` are outside #210's own stated scope, so
    inventing one was not this change to make. This reaches `store.canonical_dir` for the directory
    -- already a justified direct call in this file -- and reads/writes the two files with plain
    `Path` operations, the same shape `_cmd_session_export` already uses for directory-level work the
    repository has no method for either. The lock is still taken through the repository
    (`svc.repo.lock`), never `store.session_lock` directly, so the one direct reach past the seam
    here is the file operations themselves, not the serialisation around them.
    """
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
            found = readable_revision(d, target_rev, expected_hashes=hashes)
            if found is None:
                raise ModelUnreadableError(
                    f"revisions/{target_rev:04d}-model.json is missing, does not parse, or does not "
                    "match the hash session.json recorded for it -- refusing to restore from it",
                    details={"slug": slug, "revision": target_rev})
            payload = found.payload
        else:
            found = newest_readable_revision(d, n, expected_hashes=hashes)
            if found is None:
                raise ModelUnreadableError(
                    f"session '{slug}' has no readable, trusted revision file (1..{n}) to restore "
                    "from",
                    details={"slug": slug, "current_revision": n})
            target_rev, payload = found.revision, found.payload
        model_path = d / "model.json"
        # Temp file + rename, the same shape `_cmd_session_export` already uses for a write this
        # module's own repository seam has no method for: a crash mid-write can never leave
        # model.json half-written, because the rename is atomic on the same filesystem and nothing
        # else on disk ever points at the temp file's name. `_replace_with_retry` above retries a
        # transient Windows lock instead of raising raw -- `_cmd_session_export`'s own rename goes
        # through the identical helper (#524).
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




# Ceilings for an imported archive. A session is a handful of small JSON and Markdown files; anything
# near these is not one. They exist so a hostile or corrupt archive fails on a bound rather than on the
# filesystem filling up, and so decompression cannot be used as an amplifier.
MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
# Both caps above are computed over files alone, so an archive built entirely of *directory* entries
# declared zero files and ~zero bytes and passed both while the extraction loop still created every
# one of them -- inode exhaustion through a door neither file-only cap covers. This bounds the raw
# entry count, files and directories together, before either runs. A real `session export` writes no
# directory entry at all, so a small multiple is loose for the legitimate case and tight for the
# hostile one. Pinned by
# `test_import_refuses_an_archive_bounded_by_files_and_bytes_but_not_by_directory_entries`, with
# `test_an_archive_with_directory_entries_just_under_the_cap_still_imports` as the control.
MAX_ARCHIVE_ENTRIES = MAX_ARCHIVE_FILES * 4




def _inspect_archive(z: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
    """Validate an export archive *before* anything is written, and return the single session slug it
    contains together with the exact entry list that was validated. Raises `InvalidArchiveError` on
    anything unexpected, with `details["problem"]` naming which shape check refused it — see that
    class for the vocabulary and for why the eight conditions are one code (#101, #219). It answered
    `invalid_model` until then, on a path where nobody had proposed a model and where the arm on
    either side of it already named the archive.

    **The returned entry list is what the caller must extract, and only that.** The caller used to
    re-read `z.infolist()` for the extraction loop — a second call that happened to agree every time,
    which is not what "the extracted set is exactly the validated set" means. Handing back the one
    list this function counted and bounded makes it structural rather than a coincidence.

    Names are decomposed into path components, never prefix-matched: `str(target).startswith(root)`
    is not a containment test, since `/…/sessions-evil` starts with `/…/sessions`. A separator, a
    drive letter, a root or a `..` segment is unrepresentable rather than merely unlikely. Pinned by
    `test_import_refuses_unsafe_entries` and `test_every_refusal_on_the_import_path_names_what_it_is_about`."""
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
        # `display_token` per name, and this is the one arm on this path that needs it: the two
        # entry-name refusals above render with `!r`, and every message *after* this point names a
        # slug that `validate_slug` has already made kebab-safe. Here the names are raw archive text
        # — a directory called "ok\nAll clear." ends the line and writes the next at column 0, in the
        # refusal that exists to report it. Same class as #40 and #98, one function along. A name
        # with nothing to escape comes back byte-for-byte, so an ordinary archive reads unchanged,
        # and `details["slugs"]` stays raw because `json.dumps` escapes it on the way out.
        shown = ", ".join(display_token(s) for s in sorted(slugs))
        raise InvalidArchiveError(
            f"the archive holds {len(slugs)} session directories ({shown}); "
            "import takes exactly one",
            details={"problem": "multiple_sessions", "slugs": sorted(slugs)})
    slug = slugs.pop()
    # The directory name becomes a session slug, so it faces the same validation as any other — this is
    # what stopped an archive whose folder was called `bad slug` from being unpacked into the store and
    # breaking every later `session list`. Direct on purpose: this is the *name* rule the file
    # backing enforces, asked before any session exists to ask a repository about.
    return store.validate_slug(slug), all_infos




def _swap_in(extracted: Path, target: Path, slug: str, repo: SessionRepository) -> None:
    """Replace an existing session directory with a freshly extracted one, reversibly.

    A swap, not a delete-then-move. `rmtree` followed by a rename leaves nothing at all if the
    rename fails — the archive is refused *and* the session the user already had is gone. The old
    one steps aside first and only dies once the new one is in place; anything going wrong in
    between puts it back.

    **Only ever called for a session the caller passed `--force` for**, and never for a slug that was
    free at the guard — that arm claims by rename instead, because a session that appeared during the
    extraction window has an owner who never asked for it to be replaced (#111). That is why taking
    `session_lock` here needs no relaxation of the rule that a session must exist to be locked: this
    function is unreachable unless one does.

    **It runs under `session_lock`, which it could not do for a release** (#113). The lock used to be
    an open handle on `.lock` *inside* the directory being renamed, which Windows refuses — #112's
    four Windows legs died on `WinError 5`. It now lives in `lock_root()`, outside every session, so
    the swap is serialised against the writers of the session it replaces like any other compound
    write (invariant 9), and `os.replace` sees no open handle.

    Three things the lock closes, and the third is the one the issue did not name:

    1. a writer inside `save_revision` no longer keeps writing by pathname into the *imported*
       directory, stamping the replaced session's identity and revision log onto it;
    2. a third process no longer opens a fresh lock file in the imported directory and acquires a
       lock the first writer still holds on the old, since-unlinked inode;
    3. between the two renames below `<root>/<slug>` does not exist, and a concurrent
       `save_revision` recreated it with `(d / "revisions").mkdir(parents=True, exist_ok=True)`.
       Both the move and the rollback then failed on a non-empty destination — the rollback raising a
       bare `OSError` rather than `ImportMoveFailedError` — leaving the user's session stranded at a
       dot-prefixed name `_scan_session_root` skips and the slug held by a stub containing only
       `revisions/`. Reproduced before the fix: `session list` reported no sessions at all. So the
       step-aside, whose whole justification is that it is reversible, was defeated by the same race.

    A window remains between the caller's `repo.exists(slug)` and this lock, and it now ends in a
    structured `SessionNotFoundError` rather than the bare `FileNotFoundError` `target.replace` used
    to raise there.

    The lock is taken through `repo.lock`, not `store.session_lock`: *hold this session exclusively*
    has a backing-neutral form, so the direct call is the one
    `test_the_surfaces_reach_the_store_only_through_the_named_filesystem_concerns` is right to
    refuse. The two `Path.replace` calls below stay direct, because moving a directory onto another
    is genuinely about paths — the justification the sibling `canonical_dir` call already carries.

    Pinned by `test_a_forced_import_serialises_against_a_concurrent_writer`."""
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
    """Refuse when something that is **not a session** already occupies the slug's directory.

    `os.replace` answers this differently per platform, so without this guard the same stray `mkdir`
    imported on POSIX and failed on Windows, and the Windows refusal read `import_move_failed` — a
    sentence about a move, naming a cause that is not the cause. Enforced by
    `test_a_stray_directory_at_the_slug_is_refused_by_name_on_every_platform`.

    **It only ever refuses**, so invariant 11 is intact: the rename is still the claim, and nothing
    here authorises an import the rename would have lost. It is called on *both* sides of that rename
    because the two sides catch different windows — before it for a stray already on disk, and from
    the `except OSError` arm for one that landed while the archive was being read
    (`test_a_stray_appearing_in_the_rename_window_is_named_rather_than_called_a_move_failure`).

    The session half of the question goes through `repo.exists` rather than `store.session_exists`:
    *is this slug a session* is not a question about a path, so it has a backing-neutral form and
    `test_the_surfaces_reach_the_store_only_through_the_named_filesystem_concerns` is right to refuse
    the direct call. `target` is a path because the import moves a directory onto it, which is the
    justification the sibling `canonical_dir` call above already carries.

    **Three answers, not two.** Both probes re-raise rather than answering `False` — `Path.exists` on
    EACCES, `repo.exists` as `SessionUnreadableError` — and a probe that could not look has not
    established anything about the destination, so it says nothing and lets the rename decide, which
    is the only decision that was ever authoritative. `is_symlink` rides with `exists` for the reason
    invariant 17 gives: `exists()` follows the link, so a dangling symlink at the slug is a stray this
    would otherwise call an empty space.

    **A destination that really is a session is not this function's answer**, and reading the name
    without the body is how that gets lost. It belongs to `SessionExistsError` — *created while this
    archive was being read; pass `--force`* — which is a different remedy from this one and is raised
    by the caller. Answering here instead would replace a code a consumer already branches on with a
    new one, in the window `test_that_window_refusal_names_the_conflict_rather_than_a_move_failure`
    exists to pin.
    """
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
    """Confirm an extracted directory really is a *coherent* session before it is allowed in.

    This used to check that session.json parsed, that its slug agreed, and that a claimed revision had
    a model.json — which is shape, not truth. An archive announcing revision 2 with no `revisions/` at
    all passed, and so did one whose model.json had been swapped for a different model: nothing is
    malformed in either, only the relationships are broken. `check_session_dir` is the same check
    `requivo session verify` runs, so an archive is held to exactly the standard a live session is.

    **Exactly the same standard, and deliberately not the same output.** What `session verify` has
    and this does not is the `notes` half — an artifact type this build has no generator for is a
    fact about the session, not a reason to refuse it, so it has nothing to say on a path whose only
    question is accept or reject. Pinned by
    `test_a_future_artifact_type_survives_an_export_import_round_trip`."""
    problems = check_session_dir(d, expected_slug=slug)
    if problems:
        raise InconsistentArchiveError(
            f"the archive's session '{slug}' is not internally consistent: "
            + "; ".join(p.message for p in problems),
            details={"slug": slug, "problems": [p.to_dict() for p in problems]})




def _cmd_session_import(a, client) -> None:
    """Import a session archive: inspect → extract to a scratch directory → validate → move into place.

    Nothing lands in the session store until the whole archive has been checked and what came out of it
    has been confirmed to be a session. The old flow did the reverse — `extractall` straight into the
    store, then report success — so a bad archive was already unpacked by the time anyone could object.
    (If a second surface ever needs this, it moves to core; today the CLI is the only importer.)"""
    archive = Path(a.archive)
    if not archive.is_file():
        raise SessionNotFoundError(f"archive not found: {display_token(str(archive))}",
                                   details={"archive": str(archive)})
    root = session_root()
    # Not a bare `mkdir`: on a fresh workspace `session import` is one of the calls that can bring
    # `.requivo/` into existence -- the second door of invariant 14, receiving a colleague's session
    # before this user has run one of their own -- and the privacy `.gitignore` is written by
    # whichever call creates the root (#211). Pinned by
    # `test_import_into_a_fresh_workspace_writes_the_privacy_gitignore`.
    ensure_store_dir(root)
    repo = SessionService().repo

    try:
        z = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as e:
        raise UnreadableArchiveError(f"{display_token(str(archive))} is not a readable .zip archive: {e}",
                                     details={"archive": str(archive)}) from e
    with z:
        slug, entries = _inspect_archive(z)
        # A conflict with the store's current state, not a malformed proposal: `session_exists`
        # exists for exactly this fact and answers 409 where `invalid_model` answered 400 (#101).
        #
        # **This answer is remembered, never asked twice.** Re-deciding it as `target.exists()`
        # *after* the unzip let a session created in that window be moved aside and `rmtree`d —
        # destroyed without `--force`, because when the user would have been asked to force there was
        # nothing to force past. Pinned by
        # `test_a_session_created_during_the_extraction_window_is_refused_not_destroyed`.
        # That is invariant 9 ("a precondition is
        # held across the writes it authorises") in the one verb that writes a whole session, and it
        # is why the two arms below are two arms rather than one flag.
        occupied = repo.exists(slug)
        if occupied and not a.force:
            raise SessionExistsError(
                f"session '{slug}' already exists in this workspace — pass --force to replace it",
                details={"slug": slug})
        # Scratch space beside the store, not inside it: same filesystem, so the final move is a
        # rename, but never visible to `session list` while it is still half-written.
        scratch = Path(tempfile.mkdtemp(prefix=".import-", dir=root.parent))
        try:
            # Exactly the entry list `_inspect_archive` validated and bounded — never a fresh
            # `z.infolist()` call — so the extracted set is structurally the validated set (#219).
            for info in entries:
                z.extract(info, scratch)
            extracted = scratch / slug
            _validate_extracted(extracted, slug)
            # Direct: the import moves a directory into place, so the destination *is* a path.
            target = store.canonical_dir(slug)
            if not occupied:
                # The slug was free when it was checked, so **the rename is the claim** and nothing
                # steps aside — invariant 11's rule, and what makes the window above safe rather than
                # merely narrow. `os.replace` refuses a non-empty destination (POSIX `ENOTEMPTY`;
                # Windows any existing directory, stricter still), so a session that appeared during
                # the unzip stops this import rather than being destroyed by it. Read without that
                # platform qualifier the sentence is how #114 happened: the safety claim holds on
                # both platforms and the *answer* did not. Pinned by
                # `test_that_window_refusal_names_the_conflict_rather_than_a_move_failure`.
                #
                # **What `os.replace` does *not* answer the same way on every platform is a
                # destination that holds no session at all** (#114), which is what
                # `_refuse_a_non_session_destination` is for. It is called on both sides of the
                # rename: neither side alone converges the platforms, because a stray already on
                # disk never reaches the `except` on POSIX and one that lands mid-window never
                # reaches the pre-check.
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
                # `--force` was given against a session that is really there.
                #
                # **The swap holds `session_lock`**, which for one release it could not: the lock
                # was an open handle inside the very directory being renamed, and Windows refuses
                # that on all four legs. Moving the lock out of the session is what made this
                # available; locking anywhere else *in addition* would serialise nothing, since this
                # is the one lock every writer already takes. Pinned by
                # `test_a_forced_import_serialises_against_a_concurrent_writer`.
                #
                # What closes #111 is still the arm above and the single decision it rests on, not
                # this lock: losing a session the caller was never asked about is a question about
                # which arm runs, and no amount of mutual exclusion answers it.
                #
                # What `--force` still means, deliberately: a concurrent writer's in-flight work is
                # lost. It is now lost cleanly — the writer completes, and then the whole session is
                # replaced — rather than half-landing in the imported directory.
                _swap_in(extracted, target, slug, repo)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    if a.json:
        # `slug`/`path`, the spelling every sibling session verb uses; it was `imported`/`into`, so
        # a consumer looping over the verbs and reading `row["slug"]` got a `KeyError` from the one
        # verb that had just put the session there. Pinned by
        # `test_import_json_names_the_session_and_its_directory_the_way_its_siblings_do`.
        #
        # `path` is the session's own directory, which is what `session init --json` means by the
        # word and what the line below already prints. `into` carried the session *root*; renaming
        # the key over that value would give `path` two meanings across two verbs of one noun, which
        # is this defect back under the harmonised name and harder to see for it.
        # `replaced` keeps the meaning it always had — *did this import replace an existing session*
        # — and is now the guard's own answer rather than a second observation taken after the
        # extraction. Those two used to be able to disagree, and the disagreement was #111.
        print_json({"slug": slug, "path": str(target), "replaced": occupied})
        return
    # Same as `session init`: the line's subject is where the session landed on this machine.
    print(f"Imported session '{slug}' → {store.canonical_dir(slug)}"
          + (" (replaced an existing session)" if occupied else ""))




