from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from requivo import __version__
from requivo.core.contracts import EngineOutput, PersistedEngineOutput
from requivo.core.errors import (
    ArtifactRevisionOutOfRangeError,
    InvalidFilenameError,
    InvalidSlugError,
    ModelUnreadableError,
    RevisionConflictError,
    SessionExistsError,
    SessionLockedError,
    SessionNotFoundError,
    SessionUnreadableError,
    UnsupportedFormatVersionError,
    UnsupportedSchemaVersionError,
)
from requivo.core.selectors import display_token
from requivo.paths import output_root as _ambient_output_root
from requivo.paths import workspace_root

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

SESSION_FORMAT_VERSION = 1
# The framework's slot schema version. Bumped when the slot vocabulary changes shape; recorded on
# every session so a future reader knows which schema a model was authored against.
SCHEMA_VERSION = 1


def _atomic_write(path: Path, content: str) -> Path:
    """Write via a temp file + atomic rename, so an interruption can never leave a half-written file
    where a good one was. model.json is the durable product — a truncated JSON would be unrecoverable,
    and `os.replace` (via Path.replace) is atomic on the same filesystem.

    The temp name is unique per writer: a fixed one made concurrent writers share a scratch file, and
    the second `replace()` then raised `FileNotFoundError` where the caller should have seen a clean
    write or a `RevisionConflictError` — `test_concurrent_atomic_writes_do_not_collide_on_a_temp_file`.
    Scratch is never left behind on a failure: `test_a_failed_atomic_write_leaves_no_scratch_file`."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # newline="" disables universal-newline translation on write -- the direct analogue of the
        # explicit encoding= beside it (invariant 16). Without it a lone CR already in `content`
        # reaches Windows disk as '\r\r\n', a line the document never had (#464).
        # `test_atomic_write_passes_newline_empty_to_disable_translation`.
        #
        # Through `.open()` and not `write_text(..., newline="")`: that keyword is 3.10+, and on this
        # project's 3.9 floor it is a TypeError on every single write -- which it was, on three CI
        # legs. Guarded as a class rather than at this one site, by
        # `test_no_text_call_passes_a_keyword_the_declared_floor_rejects`.
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        _replace_with_retry(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave scratch behind on a failed write
        raise
    return path


# On Windows `rename` is `MoveFileEx`, which fails with `PermissionError(13)` whenever anything holds
# a handle to the destination — an antivirus scanner or the Search Indexer, neither of which this
# process can serialise against — and losing a completed write to the durable product is not an
# acceptable outcome (invariant 18). Deliberately bounded and narrow: `PermissionError` only, then
# the original is re-raised, because a slow permanent error helps nobody. This is the one place in
# the store where retrying is right rather than a way of hiding something — the operation is
# idempotent and the cause is external. `test_atomic_write_survives_a_transient_permission_error`,
# `test_atomic_write_still_gives_up_on_a_permanent_permission_error`.
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF_S = 0.01


def _replace_with_retry(tmp: Path, path: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))


# ── Session locking ────────────────────────────────────────────────────────────
# A session mutation is a *compound* write: read the metadata, check the revision precondition, write
# revisions/NNNN-model.json, write model.json, then rewrite session.json. Between the check and the
# last write, another writer reading the same revision would produce a second revision from the same
# base and silently overwrite the first. `expected_revision` alone cannot prevent that — it is checked
# and then acted on, and the gap between the two is the race. These helpers close it.
#
# `flock` (and its Windows equivalent) is held by the *open file description*, so the kernel releases
# it when the process dies — a crash cannot leave a session permanently locked, which is the failure
# mode a lockfile-by-existence scheme has. Held locks are tracked per thread so the service layer can
# nest `lock()` around several core calls that each take it.

_LOCK_TIMEOUT_SECONDS = 30.0
_held_locks = threading.local()


class _LockHandle:
    """What `session_lock` yields: a way for the body to ask that the lock *file* be removed as part
    of the lock's own teardown, the instant after the fd is closed.

    It exists for `delete_session` on the platform where its first attempt cannot work (#469).
    Unlinking while the lock is held is the correct ordering and is still tried first, but it raises
    on Windows, where the old code left the file behind -- so every Windows `session delete` produced
    lock residue that `doctor` then reported against the user, the verb that answers *is this install
    healthy* accusing them of a state Requivo had just created.
    `test_a_session_removed_through_session_delete_leaves_no_lock_residue`.

    Deferring to the teardown is second-best and stated as such: it narrows the release/close/unlink
    window to two syscalls rather than closing it, and it is *not* the ordering #22 and
    `delete_session`'s docstring rejected. **POSIX is unchanged** -- the deferral is requested only
    after an in-lock unlink has actually raised.

    Re-entrancy: the request is recorded against the lock *key*, not against this handle, so an inner
    frame asking is honoured by the outermost frame -- the only one that owns the fd and can close it.
    """

    __slots__ = ("_key", "_requests")

    def __init__(self, key: tuple, requests: set):
        self._key = key
        self._requests = requests

    def unlink_on_release(self) -> None:
        """Remove the lock file as soon as this lock's fd is closed. Idempotent."""
        self._requests.add(self._key)


# The POSIX poll interval (#265). `flock(LOCK_EX | LOCK_NB)` succeeds immediately or raises, so
# contention is a poll loop and this interval trades latency for CPU. Fixed rather than backed off:
# writes hold this lock for milliseconds, so contention outlasting a handful of polls is already the
# pathological case the deadline exists for, and a growing interval would add latency only to the
# ordinary case it does not help. At 20ms the full deadline is ~1500 wakeups.
_LOCK_POLL_INTERVAL_S = 0.02


def _acquire(fd: int, slug: str) -> None:
    """Take the OS lock on `fd`, bounded by `_LOCK_TIMEOUT_SECONDS` on every platform (#265).

    The two branches used to disagree about what a stuck holder looks like: `msvcrt.locking` polls on
    its own, but POSIX's `fcntl.flock(fd, LOCK_EX)` is a single call that blocks until it succeeds,
    so a stuck holder hung the CLI silently and forever on the two primary platforms while Windows
    raised the structured `SessionLockedError`. `LOCK_EX | LOCK_NB` makes both symmetric.
    `test_a_contended_lock_raises_within_the_deadline_instead_of_hanging`; re-entrancy (invariant 9)
    is decided by `session_lock`'s own depth counter before this function is ever called, per
    `test_reentrant_acquisition_within_a_thread_still_never_touches_the_lock_twice`.

    **`BlockingIOError`, not a bare `OSError`.** `flock(..., LOCK_NB)` raises exactly that when and
    only when the lock is genuinely held elsewhere; a bare `except OSError` would also catch
    `ENOLCK`, `EBADF` or a filesystem that refuses `flock` outright, none of which ever resolve by
    waiting -- masking those behind 30 seconds of retries and relabelling them "locked by another
    process" trades a loud honest failure for a quiet misleading one, the same rule
    `_replace_with_retry`'s narrow `except PermissionError` states two functions up."""
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    if fcntl is not None:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SessionLockedError(
                        f"session '{slug}' is locked by another process; retry in a moment",
                        details={"slug": slug}) from None
                time.sleep(_LOCK_POLL_INTERVAL_S)
    if msvcrt is not None:  # pragma: no cover - Windows
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # blocks ~10s per attempt, then raises
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise SessionLockedError(
                        f"session '{slug}' is locked by another process; retry in a moment",
                        details={"slug": slug}) from None


def _release(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class Store:
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

    def _lock_key(self, slug: str) -> tuple[str, str]:
        """The re-entrancy key for `slug` in *this* store -- see the class docstring for why root
        identity, not `id(self)`, is what has to decide it. `self._root_key` is resolved once, at
        construction (`__init__`), not recomputed here -- see that comment for why."""
        return (self._root_key, slug)

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

    def lock_path(self, slug: str) -> Path:
        """The write lock for `slug`: `<workspace>/.requivo/locks/<slug>.lock`.

        **Outside the session directory, which is the whole of #113's fix.** `lock_root()` carries why;
        the short version is that a lock inside a directory `session import --force` renames is a claim
        on an inode that every writer under it has already stopped agreeing with.

        Validated exactly as `canonical_dir` and `artifact_path` validate theirs, and for the same
        reason: the slug reaches here from `session_lock`, whose callers include the service layer and
        therefore, under invariant 14, an external consumer. The pattern already makes a separator or a
        dot segment unrepresentable; `is_contained` is the belt to that pair of braces, and it is the
        one shared statement of that rule rather than a fourth local one."""
        root = self.lock_root()
        slug = _slug_shape(slug)
        # Checked against the *session* root, never against `root` above (#372): what decides whether
        # #221's refusal applies is whether a session already claims this name, not whether a
        # `<slug>.lock` file does -- which it never does on a first lock. Otherwise `session export`'s
        # read-consistency lock would be the one thing standing between an already-on-disk reserved
        # name and its data, even though locking creates nothing under that name.
        # `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted`.
        #
        # `<slug>.lock` is itself a reserved-stem-shaped name on Windows -- `con.lock` matches the same
        # before-the-first-dot rule `validate_filename` enforces for artifact names (raised in review).
        # Not a live gap: reaching this line at all requires a session already occupying `slug` on
        # disk, and Windows's own `CreateDirectory` refuses to *materialize* a `con` directory in the
        # first place, independent of anything this file does -- so a reserved-named session cannot
        # exist on a real Windows filesystem for this branch to be reached from, which is also why the
        # sibling tests that build one are POSIX-only.
        _refuse_new_reserved_slug(slug, self.session_root() / slug)
        p = root / (slug + ".lock")
        if not is_contained(p, root):
            raise InvalidSlugError(f"slug {slug!r} does not resolve to a lock file inside {root}",
                                   details={"slug": slug})
        return p

    @contextmanager
    def session_lock(self, slug: str) -> Iterator[_LockHandle]:
        """Hold the exclusive lock on a session for the duration of the block.

        Re-entrant within a thread: a service that wraps a whole update can take the lock once, and the
        core calls inside it (`save_revision`, `save_session_artifact`) re-enter without deadlocking.
        Across threads and across processes the lock is genuinely exclusive.

        **The lock file lives outside the session** (`lock_path`), so this never touches the session
        directory: that is what lets `session import --force` hold it across the swap (#113), and it
        retires #22's coupling structurally rather than guarding it —`session_lock` can no longer
        produce a directory under the session root for `create_session`'s rename to lose to.

        **A session must still exist to be locked, and that check is taken *after* the lock is held**
        — invariant 9's rule applied to it like any other write precondition. It used to be closed by
        accident, by `os.open` raising `FileNotFoundError` on a lock file that lived inside the
        session; opening `.requivo/locks/<slug>.lock` establishes nothing about `<slug>`.
        `test_a_session_deleted_before_the_lock_is_granted_is_refused`.

        **The check before the open stays and is deliberately not authoritative**: it buys one thing,
        refusing a slug with no session without leaving an empty lock file behind. It can be wrong in
        exactly one direction — `_swap_in` holds this lock across two renames, so a caller sampling
        that instant refuses about a session merely being replaced — and a refusal is the safe
        direction, because this check can decline a lock and never grant one. An already-on-disk
        reserved Windows device name reaches this far on purpose (#372, `lock_path`); a genuinely new
        one is still refused by `canonical_dir` (#221,
        `test_reserved_windows_device_names_are_refused_as_slugs`).

        Neither the lock file nor a session directory is removed here: unlinking a lock file a
        concurrent process may hold is legal on POSIX and silently breaks mutual exclusion, which is
        the repair #22 rejected. Legacy `.lock` files inside existing session directories are inert.

        **Re-entrancy is keyed by root identity plus slug, not by slug alone** (#272) — the class
        docstring says why.
        """
        depths: dict[tuple[str, str], int] = getattr(_held_locks, "depths", None) or {}
        _held_locks.depths = depths
        # Thread-local beside `depths`, and for the same reason: a lock is held by a thread, so the
        # request to unlink its file on release belongs to that thread too. Not `or set()` -- an
        # existing *empty* set would be silently replaced, and while every frame captures its own
        # reference and so is unharmed, a reader should not have to establish that to trust the line.
        requests = getattr(_held_locks, "unlink_requests", None)
        if requests is None:
            requests = set()
            _held_locks.unlink_requests = requests
        key = self._lock_key(slug)
        handle = _LockHandle(key, requests)
        if depths.get(key):
            depths[key] += 1
            try:
                yield handle
            finally:
                depths[key] -= 1
            return

        if not self.session_exists(slug):     # cheap, non-authoritative — see above
            raise self._no_session(slug)
        p = self.lock_path(slug)
        # Outside the `try` on purpose (#320). That handler says "could not open the write lock", and
        # `ensure_store_dir` fails about the store root or the privacy marker — reporting one operation's
        # failure under the other's name sends the reader to the wrong file. It raises a structured error
        # of its own, so nothing is swallowed by moving it out.
        self.ensure_store_dir(p.parent)
        try:
            fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            # Not `SessionNotFoundError`: the session's existence is not what this failed to establish.
            # The old code mapped a `FileNotFoundError` here onto "no such session" because the lock file
            # lived inside the session; it no longer does, so that mapping would now be a sentence about
            # a session naming a cause that is not the cause — the shape #114 was filed for.
            raise SessionUnreadableError(
                f"could not open the write lock for session '{slug}': {e}", details={"slug": slug}) from e
        acquired = False
        try:
            _acquire(fd, slug)
            acquired = True
            if not self.session_exists(slug):
                raise self._no_session(slug)
            depths[key] = 1
            yield handle
        finally:
            depths[key] = 0
            try:
                if acquired:
                    _release(fd)
            finally:
                os.close(fd)
                # Immediately after the close and inside the same `finally`, so nothing -- not a
                # generator resume, not a return into the caller, not a `lock_path` revalidation --
                # runs between the two. See `_LockHandle` for why that distance is the whole point,
                # and why only a caller whose in-lock unlink already raised ever gets here.
                if key in requests:
                    requests.discard(key)  # cleared first: a failed unlink is never retried later
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass  # best-effort; a leftover lock file is inert, see delete_session

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

    def _scan_session_root(self) -> tuple[list[str], list[Path], list[UnexaminableEntry]]:
        """One listing of the session root, partitioned three ways: the canonical sessions, everything
        else, and the entries whose examination raised.

        **Three outcomes, because the predicate can fail** (#80). `Path.exists()` does not swallow
        `EACCES`, so one directory the process cannot stat into aborted the partition for *every*
        entry and `session list` exited 1 with an empty stdout. The third answer belongs in neither
        neighbour: in `others` it never comes back from `list_session_slugs`, which is the invisible
        entry #67 exists to close; in `slugs` it is claimed to *be* a session, the one thing the failed
        probe did not establish. `test_the_partition_answers_in_three_states_and_the_third_is_neither_neighbour`
        and `test_list_session_slugs_still_answers_only_what_is_known_to_be_a_session`, with
        `test_the_probe_the_partition_makes_really_raises_here` as the control.

        Dot-prefixed entries are in none of the three, on purpose: a slug cannot start with a dot, so
        they are `create_session`'s staging areas — a session in flight, and reporting one is a race
        the reader cannot act on.

        A root that does not exist is an empty workspace. A root that cannot be *listed* still raises:
        that failure is genuinely the whole root, there is no entry to name it against, and per-entry
        and whole-root are two claims this function must not merge in either direction."""
        root = self.session_root()
        if not root.exists():
            return [], [], []
        slugs: list[str] = []
        others: list[Path] = []
        unexaminable: list[UnexaminableEntry] = []
        for p in sorted(root.iterdir(), key=lambda p: p.name):
            if p.name.startswith("."):
                continue
            try:
                is_session = (p / "session.json").exists()
            except Exception as e:  # noqa: BLE001 - the third outcome, not a failure of the listing
                # `Exception` rather than `OSError`, for `_describe_non_session`'s reason one function
                # down: the set of ways a probe of a name off a directory listing can fail is open —
                # EACCES here, and on Linux a filename that is not valid UTF-8 comes back from
                # `iterdir` carrying surrogates, which every path operation on `p` is a candidate for.
                # Whatever it was, it lands in a state this partition now has. `BaseException` is not
                # caught: a `KeyboardInterrupt` is not an unexaminable directory.
                unexaminable.append(UnexaminableEntry(p.name, str(e)))
                continue
            if is_session:
                slugs.append(p.name)
            else:
                others.append(p)
        return slugs, others, unexaminable

    def list_session_slugs(self) -> list[str]:
        """Slugs of all canonical sessions, sorted — the backbone of `session list`.

        **Names known to be sessions, and this contract does not widen.** `doctor`, `session verify` and
        every read path reason over what comes back here, so an entry the partition could not examine is
        deliberately not in it — see `list_unexaminable_entries`, which is where it goes instead."""
        return self._scan_session_root()[0]

    def scan_session_root(self) -> tuple[list[str], list[NonSessionEntry], list[UnexaminableEntry]]:
        """All three parts of the session root from **one** listing — and the only way to reach the
        second one, since #300 (see below).

        **One listing, because two scans are two instants** (#300): `doctor` asks all three questions,
        and a `session.json` landing between two scans puts a name in *neither* answer — the invisible
        state #67 is about, reintroduced by the report meant to close it.
        `test_the_parts_of_the_session_root_are_one_partition`.

        **The second part is what nothing could see before #67**, and its cost is not in this module's
        output but at the next `create_session` on that name: the rename that *is* the claim on a slug
        (invariant 11) loses to a directory already there, and `SessionService` falls through to its
        `<slug>-<identity hash>` candidate — a session under a name the user did not ask for, with
        nothing explaining why. `test_the_silent_slug_substitution_the_report_names_is_the_one_that_happens`
        and `test_doctor_names_what_is_under_the_session_root_and_is_not_a_session`.

        **A report, not a repair.** This reads; it never deletes, moves or rewrites. Clearing residue
        on sight is the mistake #22 rejected pointing the other way, and nothing in the directory tells
        a ghost from a half-extracted archive.

        Second of three since #80, not the other half of two — an entry whose examination raised is the
        third part, and folding it into this one would hide it from `session list` for want of a
        `session.json` nobody could look for. The describe step is here rather than in
        `_scan_session_root` so `list_session_slugs` keeps paying nothing for it; the third part carries
        no describe step at all, since whatever we would ask it we have just failed to ask once."""
        slugs, others, unexaminable = self._scan_session_root()
        return slugs, [_describe_non_session(p) for p in others], unexaminable

    def list_unexaminable_entries(self) -> list[UnexaminableEntry]:
        """Names under the session root whose examination raised — the partition's third answer (#80).

        Neither `list_session_slugs` nor `scan_session_root`'s second part returns one, and that is the
        point — `_scan_session_root`'s docstring carries why. It reaches a surface as a fact of its own:
        a degraded row on `session list`, its own line under `doctor`'s sessions check
        (`test_the_repository_exposes_the_third_bucket`,
        `test_doctor_reports_the_entry_instead_of_declaring_the_whole_root_unreadable`).

        **A report, not a repair**: a name and the reason the probe failed, nothing chmod-ed. A caller
        that wants the other parts too takes `scan_session_root()` — this one scans on its own."""
        return self.scan_session_root()[2]

    def scan_lock_root(self) -> tuple[list[str], list[str], list[UnexaminableEntry]]:
        """Partition `lock_root()` three ways, for `doctor`'s lock-residue check (#180): the slugs a
        `<slug>.lock` file names, the entries that are neither that nor a recognised
        `<slug>.discovering` guard file (#209, #391), and the entries whose examination raised. The
        session-root sibling of `_scan_session_root`, one root over.

        **Two regular-file shapes are what this store writes here, and both are recognised** -- a
        `<slug>.lock` from `lock_path`, and `services.discovery`'s `<slug>.discovering` guard file,
        which is deliberately never unlinked (#209) and so outlives every discovery it served. This
        function predates the second shape and #209 never came back to teach it (#391), so every guard
        file read as "not a lock file Requivo recognises" -- about a file this store had just written,
        on the first ordinary discovery a workspace ever ran.
        `test_an_ordinary_discover_leaves_no_lock_residue_doctor_flags`, with
        `test_an_entry_under_lock_root_that_is_not_a_lock_file_is_named_as_unexpected` and
        `test_a_symlink_at_a_lock_name_is_reported_and_not_followed` for what is still reported.

        **The stem question is `_is_lock_stem`'s, and it is shape alone** (#401, corrected by #409):
        asked as `validate_slug`'s creation-time refusal it reported a reserved-name session's own
        files as residue, and asked as the read-time rule it made a fixed file's classification flip
        when an unrelated session was later deleted. `_is_lock_stem` carries the argument;
        `test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue` and
        `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted`.

        **What a matching slug means is left to the caller, deliberately.** This answers only *is there
        a `<slug>.lock` file*, never *is `slug` still a session* — conflating them would make this
        function's answer depend on a root it does not take. `doctor._lock_health` is where the two
        lists meet.

        Three outcomes per entry, on the same reasoning `_scan_session_root` gives for its own third
        bucket (#80): `p.is_symlink()`/`p.is_file()` raise on the errnos `Path.exists()` does not
        swallow. That bucket had a second source from #401 to #409 -- `_is_lock_stem` statting the
        session root -- and #409 removed it, so the file-type probe is now its only one:
        `test_a_reserved_lock_stem_no_longer_probes_the_session_root`. A root that cannot be *listed*
        is left to raise for the caller to report as `readable: False`, never as a clean scan of
        nothing: `test_the_lock_root_being_unlistable_is_not_reported_as_no_residue`."""
        root = self.lock_root()
        if not root.exists():
            return [], [], []
        lock_slugs: list[str] = []
        unexpected: list[str] = []
        unexaminable: list[UnexaminableEntry] = []
        for p in sorted(root.iterdir(), key=lambda p: p.name):
            lock_slug = p.name[: -len(".lock")] if p.name.endswith(".lock") else None
            # `_discovery_guard_path` (services/discovery.py, #209) writes this second shape and never
            # unlinks it -- recognised and excluded from `unexpected`, not folded into `lock_slugs`:
            # it is not a `<slug>.lock` file and answers a different question (#391).
            guard_slug = p.name[: -len(".discovering")] if p.name.endswith(".discovering") else None
            try:
                is_ordinary_file = p.is_file() and not p.is_symlink()
                # `_is_lock_stem`, not `is_slug` (#401), and shape alone -- not a read of the session
                # root (#409). This is a classification, not a creation: whether a writer *here* could
                # have produced this file is a fact about its own name and nothing else. See this
                # method's docstring for what each of the other two questions broke.
                if is_ordinary_file and lock_slug and _is_lock_stem(lock_slug):
                    lock_slugs.append(lock_slug)
                    continue
                if is_ordinary_file and guard_slug and _is_lock_stem(guard_slug):
                    continue
            except Exception as e:  # noqa: BLE001 - the third outcome, not a failure of the listing
                unexaminable.append(UnexaminableEntry(p.name, str(e)))
                continue
            unexpected.append(p.name)
        return lock_slugs, unexpected, unexaminable

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


# A slug becomes a directory name, so it is bounded by what the filesystem accepts (~255 bytes on ext4
# and APFS, and the whole *path* on Windows). 80 leaves generous room for the session subtree beneath
# it. `derive_slug()` stays under the smaller base ceiling so a uniqueness suffix still fits inside the cap.
MAX_SLUG_LENGTH = 80
_SLUG_BASE_LENGTH = 64


# Latin letters NFKD cannot decompose, spelled out before the fold runs. NFKD splits a letter into a
# base plus a combining mark and the ASCII fold then drops the mark; a letter carrying no mark
# decomposes to *itself*, so the fold has nothing to do but delete it — 'Straßenverkehr' arrived as
# `stra-enverkehr`, which is the same mid-word mangling as `syst-me` one letter along. Lower-case
# only, because the fold runs after `.lower()`. Pinned by
# `test_folding_expands_a_latin_letter_that_carries_no_combining_mark`.
_LATIN_EXPANSIONS = str.maketrans({
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i",
})

# Function words dropped before the five tokens are taken (#245). English plus the three other Latin
# languages this project's users actually write requests in — the slug is a handle in whatever
# language the request arrived in, and folding accents without also dropping `nous`/`un`/`des` just
# moves the junk one character along.
#
# Two rules kept this list from becoming a general-purpose stoplist. A word is in it only if it is a
# function word in *some* in-scope language and not a content word in *any* of them — which is why
# `son`, `hay`, `sin`, `man`, `war`, `bin` and `hat` are deliberately absent despite being ordinary
# function words in French, Spanish or German. And nothing is here for being *common*: `system`,
# `data`, `report` and `user` open a great many requests and are exactly what the handle should say.
#
# The exclusion rule is prose, so it has a guard rather than a promise:
# `test_the_stopword_list_keeps_the_words_its_own_comment_promises_to_keep` asserts those seven are
# absent. `son` was in the Spanish half anyway, two lines under the paragraph saying it was not.
#
# Two accepted costs, stated rather than discovered. **`die`** is the German article and an English
# verb; the article is far the more frequent in a request opening, so the trade is taken knowingly.
# And **matching is case-folded ASCII, so a short function word collides with an acronym** — `er`
# eats the ER in "an ER diagram", as do `im`, `am`, `us`, `et`, `est`, `par`. Not fixable by pruning:
# dropping `er` costs German requests more often than "ER diagram" costs English ones. The
# fewer-than-two-survivors fallback below keeps it survivable, and `--slug` is the way past it.
_SLUG_STOPWORDS = frozenset("""
    a an and are as at be been being but by can could d did do does for from had has have i if in
    into is it its like ll m me my need needed needs of on or our ours ourselves please re s should
    so some t that the their them then there these they this those to us ve want wanted wants was
    way ways we were what when where which who whose will with would you your
    au aux avec avoir avons besoin ce ces cet cette dans de des du elle elles en est et etaient
    etait ete etre faut ils je la le les leur leurs ne nos notre nous ou par pas plus pour qui quoi
    sa se ses sommes sont sur tu un une vos votre vous y aimerions aimerait souhaitons souhaiterions
    voudrais voudrions voulons
    al como con del el ella ellos es esta estas este esto estos la las lo los mi necesita necesitamos
    necesito nuestra nuestro para podemos podria podriamos por que queremos quiero se ser su sus
    tiene tenemos un una unas unos deberiamos
    aber alle als am auch auf aus bei benotigen benotigt brauche brauchen braucht das dass dem den
    der des die dies diese ein eine einem einen einer eines er es fur haben ich ihr ihre im ist kein
    keine mit mochte mochten nach nicht oder sein sich sie sind uber um und von vor wenn wie wir
    wollen wurde wurden zu zum zur
""".split())


def derive_slug(text: str) -> str:
    """Derive a session directory name from arbitrary text — the one producer of the canonical shape.

    Public because it is consumed outside this module: `SessionService.slug_hint` is the surface's
    route to it, and `validate_slug` below is written against exactly what this emits.

    Three steps, and the order between the first two is load-bearing (#245). **Fold, then filter,
    then take five.** Taking five tokens verbatim off the front of a request named the greeting
    rather than the subject — `we-need-a-way-to`, from "We need a way to track vendor invoices" — so
    two unrelated requests differed only by the collision hash; filtering before folding would leave
    `syst`/`me` in the stream as two words neither list can match. Below two survivors the
    *unfiltered* words are used, because an all-function-word request is a real shape ("We need it")
    and an empty token list falls through to `discovery`, this function's own defect reintroduced by
    its fix.

    **The residual limit, documented because it is not fixed here:** the ASCII fold deletes
    non-Latin scripts, so a Japanese or Cyrillic request still lands on `discovery-<hash>` —
    transliteration needs a dependency this package does not carry.
    `test_a_non_latin_request_still_derives_the_documented_discovery_fallback`.

    Nothing re-derives a slug for a session that already exists, so a session on disk keeps its name;
    what did change is idempotent re-discovery, which `docs/compatibility.md` carries alongside the
    other "two versions, one workspace" promises. The alphabet is unchanged (`[a-z0-9-]`).
    """
    folded = unicodedata.normalize(
        "NFKD", text.lower().translate(_LATIN_EXPANSIONS)).encode("ascii", "ignore").decode("ascii")
    tokens = re.findall(r"[a-z0-9]+", folded)
    content = [w for w in tokens if w not in _SLUG_STOPWORDS]
    words = (content if len(content) >= 2 else tokens)[:5]
    base = "-".join(words) or "discovery"
    if len(base) <= _SLUG_BASE_LENGTH:
        return base
    # Five words are usually short, but nothing guarantees it: one 300-character token yields a
    # 300-character directory name and the filesystem refuses it with a bare OSError. Truncate
    # deterministically, then re-attach identity as a short hash so two different long requests can
    # never collapse onto the same session directory.
    keep = base[:_SLUG_BASE_LENGTH - 7].rstrip("-")
    return f"{keep}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:6]}"


def load_model(path: Path) -> EngineOutput:
    """Load a saved model so artifacts can be regenerated without redoing discovery.

    Read through `PersistedEngineOutput` — still an `EngineOutput`, so the annotation holds — because
    a model on disk may have been written by a newer Requivo, and refusing an unknown key there costs
    the reader a session they can otherwise understand completely. The block at the foot of
    `contracts.py` says why the disk side and the provider side answer that question oppositely.

    The explicit codec is #11's and is not optional here either: `_atomic_write` writes UTF-8, so a
    read that takes the platform default decodes a model holding an accented value into mojibake that
    is still valid JSON, on exactly the platforms this repo now has CI legs for."""
    return _read_model(path)


def _read_model(path: Path, *, slug: Optional[str] = None, revision: Optional[int] = None) -> EngineOutput:
    """Read and validate a persisted model, turning every way that can fail into one structured error.

    One helper rather than three call sites, and that is the point rather than tidiness: a guard added
    at two of the three doors is one the third quietly does without, and which door a given verb takes
    is not visible from the verb. The bare `model_validate_json` this replaced sent a truncated
    `model.json` to the operator as a raw pydantic traceback from three CLI verbs and a generic 500
    from the web session page -- `ValidationError` is not a `RequivoError` -- while the remedy sat on
    disk in `revisions/` with nothing saying so (#204).

    `OSError` is caught alongside the parse failures because "there but unreadable" is the same fact
    about the store as "there but unparseable"; a *missing* file is decided by the callers above,
    which raise `SessionNotFoundError` because that has a different remedy. Pinned by
    `test_a_corrupt_model_is_a_structured_error_from_every_door`.
    """
    try:
        return PersistedEngineOutput.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError) as e:
        details: dict = {"path": str(path)}
        if slug is not None:
            details["slug"] = slug
        if revision is not None:
            details["revision"] = revision
        what = f"revision {revision} of session '{slug}'" if revision is not None else (
            f"the model of session '{slug}'" if slug is not None else "the model file")
        remedy = (
            f" Run `requivo session verify {slug}` for the full picture; the session's `revisions/` "
            "directory holds every model that was applied, so an earlier one can be recovered from "
            "there." if slug is not None else ""
        )
        raise ModelUnreadableError(
            f"Could not read {what}: {path} is truncated, mis-encoded, or not a valid model "
            f"({type(e).__name__}).{remedy}",
            details=details,
        ) from e


# ── Canonical session store (.requivo/sessions/<slug>/) ────────────────────────
# The versioned, forward-compatible layout: a session is a directory holding session.json (the
# metadata + provenance), request.md, model.json (the current model), revisions/NNNN-model.json (the
# history, one file per applied revision), and artifacts/ (generated views, each tied to the revision
# it was produced from). Every write is atomic; a revision is preserved before the model is replaced.
# Legacy `out/<slug>/` sessions are read-only and are copied in here only by the explicit
# `requivo session migrate` (`migrate_legacy`). Nothing has read that layout implicitly since 0.9.8.


class ArtifactStatus(BaseModel):
    """Per-artifact provenance in session.json: which model revision produced it, its file, when it
    was written, and whether the model has since moved past that revision (stale)."""
    revision: int
    filename: str
    updated_at: str
    stale: bool = False


class RevisionRecord(BaseModel):
    """Provenance for one applied revision: who produced it and from what. A session's model can be
    moved by more than one surface over its life (the Anthropic provider, a Claude Code turn, the CLI,
    later the Web), so provenance belongs to each *revision*, not just the session's creation. `extra`
    is allowed so a newer Requivo can add a provenance field an older reader simply carries through."""
    model_config = ConfigDict(extra="allow")

    revision: int
    created_at: str
    previous_revision: Optional[int] = None   # the revision this one succeeded (None for the first)
    provider: Optional[str] = None            # "anthropic", "claude-code", "cli", …
    model_name: Optional[str] = None          # the reasoning model, when one produced it
    surface: Optional[str] = None             # the reasoning surface, e.g. "cli-discover", "requivo-answer"
    prompt_version: Optional[str] = None      # "sha256:…" of the prompt, when known
    model_hash: str = ""                      # "sha256:…" of the model payload — content identity
    # Token/rate provenance for a provider-backed apply (#292) — absent for a deterministic apply
    # (session import, a hand-authored `model apply`, a Claude Code turn, which spends no API tokens)
    # and for any revision written before this field existed. Never zero-filled: invariant 6 says
    # provenance is real or absent, and a revision that genuinely spent 0 tokens does not exist.
    usage_input_tokens: Optional[int] = None
    usage_output_tokens: Optional[int] = None
    usage_cache_read_tokens: Optional[int] = None
    usage_cache_write_tokens: Optional[int] = None
    # The rate this revision's calls were actually billed at, `(input, output)` USD per million
    # tokens — stamped rather than looked up again at render time, so a later price-table edit
    # cannot retroactively change what an old revision is reported to have cost (`usage.py`'s own
    # "cost is arithmetic here and nowhere else"). `None` when the calls behind this revision did not
    # all agree on one rate — a genuine disagreement is refused rather than guessed at.
    usage_rate_per_mtok: Optional[tuple[float, float]] = None
    usage_priced_as_of: Optional[str] = None   # the rate table's own date, alongside the rate itself


class SessionMeta(BaseModel):
    """The versioned session metadata (`session.json`). `migrate_session()` is the explicit version
    frontier.

    `extra="allow"` — matching `RevisionRecord` — so a field a *newer* Requivo added survives a
    round-trip through an older one. Under `extra="ignore"` the older reader loaded the session fine
    and then dropped the unknown field the moment it wrote the file back, which turns "an old reader
    tolerates a new field" into "an old reader silently destroys it on first use". Forward
    compatibility is a promise about the file, not just about the load."""
    model_config = ConfigDict(extra="allow")

    format_version: int = SESSION_FORMAT_VERSION
    requivo_version: str = __version__
    session_id: str
    slug: str
    created_at: str
    updated_at: str
    provider: Optional[str] = None          # "anthropic", "claude-code", or None (informational)
    model_name: Optional[str] = None        # the reasoning model, when a provider set one
    context_cards: Optional[list[str]] = None  # the card selection; None == all cards
    request_hash: str = ""               # "sha256:…" of the originating request
    schema_version: int = SCHEMA_VERSION
    # (A session-level `prompt_versions` map lived here and was never written. Prompt identity belongs
    # to the revision that was reasoned with it, not to the session — see RevisionRecord.prompt_version.
    # It is listed in _RETIRED_KEYS so `extra="allow"` doesn't carry the dead key forever.)
    current_revision: int = 0            # 0 == session created but no model applied yet
    revisions: list[RevisionRecord] = Field(default_factory=list)  # provenance log, one per applied revision
    artifact_status: dict[str, ArtifactStatus] = Field(default_factory=dict)


def _now() -> str:
    """UTC, second precision, Z-suffixed — one timestamp format across the whole session file."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def content_hash(text: str) -> str:
    """The persisted hash format — `sha256:<hex>` — as `model_hash` and `request_hash` carry it on disk.

    Public because `integrity.py` recomputes it to check a session against its own recorded hashes.
    A second implementation of this line would drift, and a drifted rehash reports
    `revision_hash_mismatch` against a file nobody touched.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# A slug names a directory under the session root; it must never be able to escape it. `derive_slug()` and
# `resolve_slug()` always emit this shape, but an *explicit* `--slug` (or a future API caller) is
# untrusted input — so the two path constructors below validate before joining. The pattern forbids
# every traversal vector at once: `/`, `\`, `.`, `..`, a leading root, and the empty string.
#
# `\Z` and not `$`, here and on `_FILENAME_RE` below (#40, adjacent): Python's `$` also matches just
# before a trailing newline, so a guard whose stated job is to make a control character
# unrepresentable admitted exactly one.
# `test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline`.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")

# Windows refuses to create a file or directory named one of these, case-insensitively and whatever
# the extension (the OS matches the component *before the first dot*). Both name patterns admit them,
# so without this a session slugged 'con' is valid on macOS/Linux, exports fine, and then cannot be
# materialized by `session import` on a colleague's Windows machine -- a portability hole the session
# format's own promise never mentions (#221).
# `test_reserved_windows_device_names_are_refused_as_slugs`.
#
# Refused on *every* platform: refusing only on Windows would still let a POSIX user create an
# archive Windows can never open, which relocates the defect rather than closing it. `com0`/`lpt0`
# and a bare `com`/`lpt` are deliberately absent -- only `com1`-`com9` and `lpt1`-`lpt9` are real
# devices, and a check wider than the real set refuses a name nobody needed refused.
#
# This refuses *creation*. A session already on disk under a reserved name is data rather than a
# request to make anything, and reading it is a narrower conditional exception -- see
# `_refuse_new_reserved_slug` (#372).
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def _reserved_stem(name: str) -> bool:
    """Is the component of `name` before its first dot a Windows reserved device name?

    Shared by `validate_slug` (whose "stem" is the whole slug -- a slug never carries a dot) and
    `validate_filename` (whose stem is genuinely the part before the first `.`, so `con.tar.gz` is
    caught on `con` and not on `con.tar`)."""
    stem = name.split(".", 1)[0]
    return stem.lower() in _RESERVED_DEVICE_NAMES


def _raise_reserved_slug(slug: str) -> None:
    """The one wording for "this slug is a reserved Windows device name" -- shared by `validate_slug`
    (which raises it unconditionally) and `_refuse_new_reserved_slug` (which raises it only when
    nothing already claims the name, #372), so the two paths cannot drift into two different
    sentences for what is, from the caller's side, the identical refusal."""
    raise InvalidSlugError(
        f"invalid session slug {slug!r}: {slug.lower()!r} is a reserved Windows device name and "
        "cannot be created as a directory there",
        details={"slug": slug})


def _slug_shape(slug: str) -> str:
    """Pattern and length -- the parts of slug validity that hold regardless of what is already on
    disk. `validate_slug` layers the reserved-device-name refusal on top of this unconditionally;
    `_child_of` and `lock_path` layer a *conditional* version of it instead (#372, see
    `_refuse_new_reserved_slug`)."""
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise InvalidSlugError(
            f"invalid session slug {slug!r}; expected kebab-case [a-z0-9-], e.g. 'leave-approval'",
            details={"slug": slug})
    # Length is part of validity, not a separate concern: an over-long slug is a directory name the
    # filesystem rejects, and it fails deep inside a write as an OSError instead of at the boundary.
    # `derive_slug()` never emits one; an explicit --slug or an API caller can.
    if len(slug) > MAX_SLUG_LENGTH:
        raise InvalidSlugError(
            f"session slug is {len(slug)} characters; the maximum is {MAX_SLUG_LENGTH}",
            details={"slug": slug[:MAX_SLUG_LENGTH], "length": len(slug),
                     "max_length": MAX_SLUG_LENGTH})
    return slug


def _refuse_new_reserved_slug(slug: str, existing_check: Path) -> None:
    """Refuse a reserved Windows device name (#221) unless something already occupies
    `existing_check` -- the creation/read split #372 draws. A genuinely *new* reserved slug is refused
    exactly as strictly as before, because `_probe` finds nothing there; what changes is a name a
    session already occupies on disk, which is data rather than a request to create anything and was
    otherwise stranded behind the very guard meant to keep it portable.
    `test_reserved_windows_device_names_are_refused_as_slugs` and
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken`.

    Routed through `_probe` rather than a bare `.exists()` so the third answer stays a third answer:
    a stat this cannot make surfaces as `SessionUnreadableError` instead of this function picking a
    side of a question nobody could decide."""
    if _reserved_stem(slug) and not _probe(existing_check, slug):
        _raise_reserved_slug(slug)


def validate_slug(slug: str) -> str:
    """Return `slug` if it is a safe session identifier, else raise `InvalidSlugError`. Lives in Core
    so every surface (CLI, provider, a future web service) inherits the same directory-traversal guard,
    not just FastAPI. Belt-and-suspenders: callers additionally confirm the resolved path stays under
    the root, but the pattern alone already makes a separator or dot segment unrepresentable.

    **The reserved-device-name refusal here is unconditional, on purpose** (#372): a caller who
    *names* a slug directly is asking to create or address one deliberately, and widening this
    function would widen every creation path with it. `_child_of` and `lock_path` are where an
    *existing* session earns the narrower read-only exception -- see `_refuse_new_reserved_slug`, and
    `test_reserved_windows_device_names_are_refused_as_slugs` for this half."""
    slug = _slug_shape(slug)
    # A slug never carries a dot (the pattern above forbids it), so this is a whole-slug check --
    # see `_reserved_stem` and #221.
    if _reserved_stem(slug):
        _raise_reserved_slug(slug)
    return slug


def is_slug(name: str) -> bool:
    """Whether `name` is a slug that could be *created* right now — the same question `validate_slug`
    answers, as a predicate: the unconditional, creation-time form, which refuses a reserved
    Windows device name whether or not anything already occupies it.

    Deliberately implemented by *calling* it rather than by re-testing `_SLUG_RE`: validity is the
    pattern **and** the length, and an earlier predicate written against the pattern alone marked an
    81-character kebab-case directory as a name a session would silently lose, when `canonical_dir`
    refuses it outright and loudly. One rule, one place, found by review.

    **Has no caller in this codebase as of #408, and that is correct rather than dead weight.** The
    two callers it used to have were both describing an entry that already exists on disk, so their
    question is `_shape_only`'s, not this one's -- see
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken` for what that cost.
    `is_slug` stays as the creation-time predicate `validate_slug` is missing a bool form of, for a
    caller that genuinely asks about a fresh name."""
    try:
        validate_slug(name)
    except InvalidSlugError:
        return False
    return True


def _shape_only(name: str) -> bool:
    """Whether `name` matches `_slug_shape` -- pattern and length -- with no reserved-device-name
    question asked at all, as a bool.

    The read-time predicate for a caller that already knows, by construction, that something occupies
    the path it would ask `_refuse_new_reserved_slug` about -- so that conditional check could only
    ever answer "does not refuse". `_is_lock_stem` (#409) and `_describe_non_session`'s `slug_shaped`
    (#408) are both this now; see each for why its own "something occupies the path" holds, and
    `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted` and
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken` for the two defects
    the other predicates caused there. Not `_slug_shape` bare, which raises rather than a bool."""
    try:
        _slug_shape(name)
    except InvalidSlugError:
        return False
    return True


def _is_lock_stem(stem: str) -> bool:
    """Whether a `<stem>.lock` or `<stem>.discovering` under `lock_root()` is one this store could
    have written -- the **stem** half of what `lock_path` and
    `services.discovery._discovery_guard_path` each validate before joining their own suffix.

    **Shape alone, since #409 -- not `_slug_shape` plus a read of `session_root()`** (#401). A lock
    file's provenance is fixed the moment it is written, so asking the read-time question made the
    classification of a fixed fact depend on whether an unrelated directory still exists *now*:
    `nul.lock`, written while a `nul` session was open, read as unrecognised the moment that session
    was deleted -- invariant 17's shape, a verdict decided by a resource the answer does not name,
    and a violation of `scan_lock_root`'s own "never *is `slug` still a session*". Whether a session
    still matches a stem is `_lock_health`'s question, asked separately. Pinned by
    `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted` and
    `test_a_lock_file_for_a_reserved_name_with_no_session_on_disk_is_recognised_not_residue`.

    **Not `lock_path(stem)` in a `try` either**, which reads as the tidier "one rule, one place" and
    imports a third check about a *path*: `lock_path` ends in `is_contained(root / (stem + ".lock"))`,
    so asked the `.discovering` question it answered about a **different file** and an unrelated
    symlink at `<stem>.lock` flipped a real guard file into `unexpected` -- invariant 17 one layer
    down, a verdict about one entry decided by a sibling's state.
    `test_a_symlink_at_the_lock_name_does_not_sink_the_guard_file_beside_it`. Containment is not
    missing here, it is inapplicable: entries come out of `iterdir(root)`, `_slug_shape` makes a
    separator unrepresentable in a stem, and a symlink at the entry itself is already excluded.

    **Not `is_slug` either**, whose refusal is unconditional because it guards *creation*: asking the
    creation-time question reported a reserved-name session's own files as residue nobody recognises
    (#401) -- `test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue`.

    **No longer probes the filesystem at all**, so `scan_lock_root`'s `unexaminable` bucket has lost
    a source by design: `test_a_reserved_lock_stem_no_longer_probes_the_session_root`."""
    return _shape_only(stem)


# A filename is the *other* half of an artifact write target, and it was unvalidated while its slug
# sibling on the same call was not. The same shape as `_SLUG_RE`, one separator class wider: a name is
# runs of [a-z0-9] joined by single `.`, `-` or `_`. That forbids every vector at once — `/`, `\`, a
# `..` segment (two dots in a row cannot be written), a leading or trailing separator, a leading dot,
# and the empty string — while still admitting every name the store actually writes
# (`solution-assessment.md`, `acceptance-criteria.md`, `epic.github.json`).
#
# Deliberately lowercase-only, matching the slug: a rejection is loud and one edit away, whereas a
# permissive pattern is the thing being removed here. Every filename in `ARTIFACT_FILENAMES` and every
# epic export name already fits.
_FILENAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")

# Room for the whole name plus the unique scratch suffix `_atomic_write` appends (a dot, the pid, 8
# hex and `.tmp` — about 20 characters), inside the ~255-byte ceiling ext4 and APFS impose.
MAX_FILENAME_LENGTH = 120


def validate_filename(filename: str) -> str:
    """Return `filename` if it is a safe bare filename, else raise `InvalidFilenameError`.

    The sibling of `validate_slug`, and it exists for the reason stated there: the guard belongs in
    Core so every surface inherits it, not in the callers that happen to be careful. Every in-repo
    caller passes a literal or an `ARTIFACT_FILENAMES` lookup — which is precisely why this was
    missing, and precisely the argument invariant 14 makes for putting it here anyway: the threat
    model is the external consumer calling the service directly, not the CLI."""
    if not isinstance(filename, str) or not _FILENAME_RE.match(filename):
        raise InvalidFilenameError(
            f"invalid artifact filename {filename!r}; expected a bare lowercase name such as "
            "'prd.md' — no directories, no dot segments, no leading dot",
            details={"filename": filename})
    # The stem before the first dot, not the whole filename: `con.tar.gz` is reserved on the `con`
    # component alone, and Windows refuses it regardless of what follows (#221, see `_reserved_stem`).
    if _reserved_stem(filename):
        stem = filename.split(".", 1)[0]
        raise InvalidFilenameError(
            f"invalid artifact filename {filename!r}: {stem.lower()!r} is a reserved Windows device "
            "name and cannot be created as a file there",
            details={"filename": filename})
    # Length is part of validity for the same reason it is for a slug: an over-long name is refused by
    # the filesystem deep inside the write, as a bare OSError, instead of at the boundary.
    if len(filename) > MAX_FILENAME_LENGTH:
        raise InvalidFilenameError(
            f"artifact filename is {len(filename)} characters; the maximum is {MAX_FILENAME_LENGTH}",
            details={"filename": filename[:MAX_FILENAME_LENGTH], "length": len(filename),
                     "max_length": MAX_FILENAME_LENGTH})
    return filename


def _resolve(path: Path) -> Path:
    """Canonicalise `path` for a containment comparison — `os.path.realpath`, deliberately, and not
    `Path.resolve()`.

    **On Windows under CPython 3.9 the two disagree in the direction that matters, and the
    disagreement was a hole in the containment check below**: `Path.resolve()` asks
    `nt._getfinalpathname`, which has to *open* the path and so fails on a dangling symlink, after
    which the non-strict branch re-joins the unresolvable tail and the link reports itself as living
    exactly where it sits — so `is_relative_to` says yes however far out of the root it points.
    `realpath` reads the reparse point instead. Seen as `DID NOT RAISE InvalidSlugError` on the
    `py3.9, windows-latest` leg and on no other (#3, #11); simulated on every leg by
    `_blind_to_dangling_links` in `tests/test_integrity.py`, with
    `test_a_dangling_symlink_is_refused_where_the_platform_cannot_resolve_it`.

    A second thing the switch quietly fixed: on 3.9 a **symlink loop** makes `Path.resolve()` raise
    `RuntimeError`, which no caller here catches, while `realpath` collapses the path lexically and
    returns an answer the containment check can act on. Nothing here needs anything newer than 3.9 --
    `os.path.realpath` is non-strict by default, and the `strict=` keyword is 3.10+ and must not be
    reached for.
    """
    return Path(os.path.realpath(path))


def is_contained(child: Path, parent: Path) -> bool:
    """Is `child` genuinely inside `parent`? The one containment decision in the store.

    `_child_of`, `artifact_path` and `check_session_dir` each used to state this in their own words,
    and each then had to be corrected for the same two defects in turn — the race below and the
    dangling link above (invariant 17). Three statements of one rule is three places for the next
    correction to miss. Public for that reason: `check_session_dir` in `integrity.py` imports this
    rather than restating it, so the name is a cross-module contract.

    The resolution happens **only when `child` is there in some form**, which is load-bearing rather
    than an optimisation: two independent resolutions of paths where one is derived from the other
    give a verdict that depends on what the filesystem looked like between the two calls, so
    `canonical_dir("s")` raised *you gave me a bad slug* about a good slug because somebody else was
    creating a session at that moment (#3). `test_a_session_path_is_not_resolved_before_it_exists`,
    with `test_a_symlink_out_of_the_session_root_is_still_refused` as the must-fire half.

    Answering True for an absent child is a claim about the caller too: every caller has been through
    `validate_slug`/`validate_filename`, so what was joined is a single flat component one level below
    `parent`, the only way out is a symlink at `child` itself, and an absent path is not a symlink —
    hence `is_symlink()` as well as `exists()`, since `exists()` follows the link and reports a
    dangling one as absent.

    False therefore means *not confirmed to be inside* — two situations, deliberately one answer: the
    resolved path is elsewhere, or the resolver could not tell. Folding the second in with *inside* is
    what the Windows 3.9 defect was, a guard that could not look reporting what it reports when it
    looked and found nothing. Callers word their refusal to cover both.
    """
    if not (child.exists() or child.is_symlink()):
        return True
    root = _resolve(parent)
    resolved = _resolve(child)
    # A resolver that could not follow the link hands back the link's own location — literally so on
    # 3.9/Windows, whose non-strict branch re-joins the unresolvable tail to the parent it *could*
    # resolve (see `_resolve`). A symlink never legitimately resolves to where it sits, so the
    # equality is a reliable tell and not a heuristic, and the answer to it is to refuse.
    #
    # This is what keeps the guarantee off the platform. Without it the containment decision rests on
    # the resolver being able to follow a link, which is an assumption that held on twelve legs of
    # thirteen and was invisible on the twelfth. `child.parent` is `parent` at all three call sites,
    # so `root / child.name` costs no third resolution; anywhere it is not, the equality simply does
    # not match and the containment test below answers on its own.
    if child.is_symlink() and resolved == root / child.name:
        return False
    return resolved.is_relative_to(root)


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


def _probe(marker: Path, slug: str) -> bool:
    """Is `marker` there? — with the third answer routed out through the error channel.

    `Path.exists()` has two returns and three outcomes: it swallows `ENOENT`/`ENOTDIR` into `False`
    and **re-raises everything else**, which escaped as a bare `PermissionError` traceback — the
    identical unguarded probe #80 removed from `_scan_session_root`, hit again by the `session verify
    <slug>` footer #80 itself prints (#97). `test_session_exists_answers_could_not_tell_through_the_error_channel`
    and `test_absent_is_still_false_because_absent_is_a_real_answer`.

    **The bool is not widened, because a bool cannot hold three states.** `cli.py` and
    `session import --force` read these to decide whether to *create or overwrite*, so answering
    `False` would turn *I could not tell* into a write proceeding on an unknown. The third state
    leaves as `SessionUnreadableError`; `ENOENT` still returns `False`, because absent is a real
    answer and the commonest one."""
    try:
        return marker.exists()
    except OSError as e:
        raise SessionUnreadableError(
            f"could not determine whether session '{slug}' exists: {e}",
            details={"slug": slug}) from e


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
_RETIRED_KEYS = ("prompt_versions",)


def migrate_session(data: dict) -> SessionMeta:
    """The version frontier: turn a raw session.json dict into a `SessionMeta`, upgrading old formats.
    Only v1 exists today, but the boundary is explicit — a session written by a *newer* Requivo is
    rejected clearly rather than silently mis-read. Unknown keys are carried through untouched (see
    `SessionMeta`); known-retired ones are dropped."""
    fv = data.get("format_version", SESSION_FORMAT_VERSION)
    if fv > SESSION_FORMAT_VERSION:
        raise UnsupportedFormatVersionError(
            f"session format v{fv} is newer than this Requivo understands (v{SESSION_FORMAT_VERSION}) "
            "— upgrade requivo.",
            details={"format_version": fv, "supported_format_version": SESSION_FORMAT_VERSION},
        )
    # The slot vocabulary is a second, independent contract, and it was recorded on every session and
    # then read by nothing. A model authored against a newer schema can hold slots this build has no
    # definition for; without this check the first symptom is an `unknown_slot` error naming a slot the
    # user never typed. An *older* schema is fine — that is ordinary backward compatibility.
    sv = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(sv, int) and sv > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"this session was authored against slot schema v{sv}, newer than this Requivo understands "
            f"(v{SCHEMA_VERSION}) — upgrade requivo.",
            details={"schema_version": sv, "supported_schema_version": SCHEMA_VERSION},
        )
    return SessionMeta.model_validate({k: v for k, v in data.items() if k not in _RETIRED_KEYS})


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
_NON_SESSION_SAMPLE = 5


@dataclass(frozen=True)
class NonSessionEntry:
    """Something under the session root that is **not** a session, described and not interpreted.

    A directory holding only `.lock` is almost certainly what `session_lock` left behind before #22,
    and *almost certainly* is not a licence to say so: a half-extracted archive, an interrupted copy
    and a hand-made directory are the same shape from here, and `integrity.py`'s rule is that the
    evidence is the directory and only the directory. So every field is an observation and there is
    deliberately no field spelling a conclusion — a reader acts on the name of the field, not on the
    paragraph beside it. `test_a_symlink_is_reported_as_one_and_its_target_is_not_read`.

    `slug_shaped` is the one derived value, and it is about the *name*: whether `create_session`'s
    rename would reach this directory and collide with it, which is what decides whether the entry
    costs anybody anything. It is `_shape_only` — pattern *and* length, since the pattern alone once
    marked an 81-character name as one a session would silently lose (`test_a_name_too_long_to_be_a_slug_is_not_marked_as_taken`)
    — and deliberately not `is_slug`, whose unconditional creation-time refusal read a *taken*
    reserved name as unreachable and left `doctor`'s `[name taken]` hint silent about the one
    directory it exists to name (#408,
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken`).

    `entries` is capped at `_NON_SESSION_SAMPLE` and `entry_count` is the true total. Three states,
    as everywhere: populated with `error` None (we looked inside); None with an `error` (we could
    not, which must not render like an empty directory —
    `test_an_entry_that_could_not_be_looked_inside_is_not_reported_as_empty`); and None with no
    `error` on a `file` or `other`, where there was nothing to look inside. Telling *empty* from
    *could not look* matters because an empty directory costs nothing on POSIX, where `rename(2)`
    replaces it, and everything on Windows, where `MoveFileEx` refuses any existing destination —
    which is also why `slug_shaped` does not exempt an empty one.
    """
    name: str
    kind: str
    entries: list[str] | None
    entry_count: int | None
    error: str | None
    slug_shaped: bool

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "entries": self.entries,
                "entry_count": self.entry_count, "error": self.error,
                "slug_shaped": self.slug_shaped}


@dataclass(frozen=True)
class UnexaminableEntry:
    """A name under the session root whose examination **raised** — the partition's third outcome.

    Not a session, and not *not* a session: unknown. The probe that decides which one it is failed,
    so both of the other answers would be claims nobody established.

    `error` is the exception's own text rather than a code, for the reason every other third state
    in this codebase keeps it: *permission denied on this path* is a remedy and `unexaminable` is
    not. It carries the path, which is the part a user acts on."""
    name: str
    error: str

    def to_dict(self) -> dict:
        return {"name": self.name, "error": self.error}


def _scan_session_root() -> tuple[list[str], list[Path], list[UnexaminableEntry]]:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store._scan_session_root`, which this delegates to."""
    return _default_store()._scan_session_root()


def list_session_slugs() -> list[str]:
    """Ambient-default wrapper (#272) -- see `Store.list_session_slugs`."""
    return _default_store().list_session_slugs()


def _describe_non_session(p: Path) -> NonSessionEntry:
    """Describe one entry, and **never raise**.

    Totality is the point, not politeness. This runs inside the one `try` in `_session_health` that
    also holds the session listing, so an exception escaping here discards a session report that had
    already succeeded and tells the reader the whole root was unlistable — a claim broader than what
    failed, which is invariant 15's shape one layer down. The two arms below are therefore `Exception`
    rather than `OSError`, and each still lands in a state this entry already has: *we could not stat
    it* and *we could not list it*. That is not the guard-that-provably-cannot-fire invariant 15 warns
    against — it is the same third state reached from a wider set of causes, and the cause I could not
    rule out is real: on Linux a filename that is not valid UTF-8 comes back from `iterdir` carrying
    surrogates, and every consumer of `p.name` downstream is a candidate. APFS refuses such a name, so
    it could not be constructed here to be ruled out either way.

    **`slug_shaped` is `_shape_only(p.name)`, not the full read-time rule, and not `is_slug`** (#408).
    `p` came out of `iterdir()` under `session_root()`, so it already occupies the one path
    `_refuse_new_reserved_slug` would be asked to probe -- that probe could only ever answer "does
    not refuse", so calling it would be a filesystem read this "never raise" function would then have
    to guard, for an answer `p`'s existence already implies. `NonSessionEntry`'s own docstring carries
    what `is_slug` broke here; pinned by
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken`."""
    slug_shaped = _shape_only(p.name)
    try:
        # `is_symlink` first, and it does not follow. `is_dir()` does: a symlink at a slug name
        # pointing anywhere else reported as a plain `directory`, and then `iterdir` listed the
        # **target's** filenames into a report about this workspace. A symlink is a third shape, not
        # a directory, and this file already treats one as the single case a containment guard has to
        # answer for (invariant 17). Found by review.
        if p.is_symlink():
            return NonSessionEntry(p.name, "symlink", None, None, None, slug_shaped)
        kind = "directory" if p.is_dir() else ("file" if p.is_file() else "other")
    except Exception as e:  # noqa: BLE001 - a describe that raises blanks a report that succeeded
        # `Path.is_dir()` swallows only what `_ignore_error` covers — ENOENT, ENOTDIR, ELOOP — and
        # re-raises the rest, EACCES among them. A stat we are not allowed to make lands here, and
        # what this is is then genuinely unknown: answering `other` would be a claim we cannot make.
        return NonSessionEntry(p.name, "unknown", None, None, str(e), slug_shaped)
    if kind != "directory":
        return NonSessionEntry(p.name, kind, None, None, None, slug_shaped)
    try:
        names = sorted(c.name for c in p.iterdir())
    except Exception as e:  # noqa: BLE001 - same reason; the kind is known, the contents are not
        return NonSessionEntry(p.name, kind, None, None, str(e), slug_shaped)
    return NonSessionEntry(p.name, kind, names[:_NON_SESSION_SAMPLE], len(names), None, slug_shaped)


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
