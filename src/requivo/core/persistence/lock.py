"""Session locking and path containment.

Split out of `core/persistence.py` by #550 (the lean pass, #548): the OS-level lock primitives
(`_LockHandle`, `_acquire`, `_release`), the path-containment guard `is_contained` invariant 17
is built around, and `_LockMixin` -- the `Store` methods that use them (`_lock_key`, `lock_path`,
`session_lock`). `Store` (in `store.py`) inherits this mixin; every method body below is unchanged,
only its enclosing class moved.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from requivo.core.errors import InvalidSlugError, SessionLockedError, SessionUnreadableError

if TYPE_CHECKING:
    from requivo.core.errors import SessionNotFoundError
from requivo.core.persistence.identifiers import _refuse_new_reserved_slug, _slug_shape

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

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




class _LockMixin:
    """The locking third of `Store` -- see that class's own docstring for why root identity, not
    `id(self)`, decides re-entrancy. Composed into `Store` (`store.py`) rather than duplicated."""

    if TYPE_CHECKING:  # what `Store` provides; declared so pyright can read the mixin alone
        _root_key: str

        def session_root(self) -> Path: ...
        def lock_root(self) -> Path: ...
        def ensure_store_dir(self, path: Path) -> Path: ...
        def session_exists(self, slug: str) -> bool: ...
        def _no_session(self, slug: str) -> SessionNotFoundError: ...

    def _lock_key(self, slug: str) -> tuple[str, str]:
        """The re-entrancy key for `slug` in *this* store -- see the class docstring for why root
        identity, not `id(self)`, is what has to decide it. `self._root_key` is resolved once, at
        construction (`__init__`), not recomputed here -- see that comment for why."""
        return (self._root_key, slug)

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

