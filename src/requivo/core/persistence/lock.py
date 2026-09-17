"""Session locking and path containment (#550): the OS-level lock primitives, `is_contained`
(invariant 17), and `_LockMixin`, the `Store` methods that use them.
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
# A session mutation is a compound write, and `expected_revision` is checked and then acted on: the
# gap is the race these close (invariant 9). `flock` is held by the open file description, so a
# crash cannot leave a session locked; held locks are tracked per thread so the service can nest.

_LOCK_TIMEOUT_SECONDS = 30.0
_held_locks = threading.local()


class _LockHandle:
    """What `session_lock` yields: a way to ask that the lock *file* be removed in the lock's own
    teardown, the instant after the fd is closed. For `delete_session` on Windows, where the in-lock
    unlink raises (#469); second-best, and POSIX is unchanged. Recorded against the lock *key*, so
    an inner frame's request is honoured by the outermost. `test_a_session_removed_through_session_delete_leaves_no_lock_residue`."""

    __slots__ = ("_key", "_requests")

    def __init__(self, key: tuple, requests: set):
        self._key = key
        self._requests = requests

    def unlink_on_release(self) -> None:
        """Remove the lock file as soon as this lock's fd is closed. Idempotent."""
        self._requests.add(self._key)


# The POSIX poll interval (#265): fixed rather than backed off, since writes hold the lock for milliseconds.
_LOCK_POLL_INTERVAL_S = 0.02


def _acquire(fd: int, slug: str) -> None:
    """Take the OS lock on `fd`, bounded by `_LOCK_TIMEOUT_SECONDS` on every platform (#265):
    `test_a_contended_lock_raises_within_the_deadline_instead_of_hanging`. `BlockingIOError`, not
    a bare `OSError`: `ENOLCK`/`EBADF` never resolve by waiting."""
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
    """Canonicalise `path` for a containment comparison: `os.path.realpath`, not `Path.resolve()`,
    which on Windows 3.9 reports a dangling symlink as living where it sits and raises on a loop
    (#3, #11). `test_a_dangling_symlink_is_refused_where_the_platform_cannot_resolve_it`."""
    return Path(os.path.realpath(path))


def is_contained(child: Path, parent: Path) -> bool:
    """Is `child` genuinely inside `parent`? The one containment decision in the store, shared with
    `integrity.py`. Resolved only when `child` exists or is a symlink (invariant 17, #3:
    `test_a_session_path_is_not_resolved_before_it_exists`); an absent child is True because every
    caller validated a single flat component. False means *not confirmed inside*: elsewhere, or the
    resolver could not tell."""
    if not (child.exists() or child.is_symlink()):
        return True
    root = _resolve(parent)
    resolved = _resolve(child)
    # A resolver that could not follow the link hands back the link's own location; a symlink never
    # legitimately resolves to where it sits, so the equality is a tell and the answer is to refuse.
    if child.is_symlink() and resolved == root / child.name:
        return False
    return resolved.is_relative_to(root)




class _LockMixin:
    """The locking third of `Store`; root identity, not `id(self)`, decides re-entrancy."""

    if TYPE_CHECKING:  # what `Store` provides; declared so pyright can read the mixin alone
        _root_key: str

        def session_root(self) -> Path: ...
        def lock_root(self) -> Path: ...
        def ensure_store_dir(self, path: Path) -> Path: ...
        def session_exists(self, slug: str) -> bool: ...
        def _no_session(self, slug: str) -> SessionNotFoundError: ...

    def _lock_key(self, slug: str) -> tuple[str, str]:
        """The re-entrancy key for `slug` in this store; `_root_key` is resolved once at construction."""
        return (self._root_key, slug)

    def lock_path(self, slug: str) -> Path:
        """The write lock for `slug`: `<workspace>/.requivo/locks/<slug>.lock`, outside the session
        directory (#113). Validated as `canonical_dir` validates its slug (invariant 14)."""
        root = self.lock_root()
        slug = _slug_shape(slug)
        # Checked against the *session* root (#372): what decides the reserved-name refusal is whether a
        # session already claims this name. `con.lock` is not a live gap: Windows cannot materialize a
        # `con` directory for this branch to be reached from.
        # `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted`.
        _refuse_new_reserved_slug(slug, self.session_root() / slug)
        p = root / (slug + ".lock")
        if not is_contained(p, root):
            raise InvalidSlugError(f"slug {slug!r} does not resolve to a lock file inside {root}",
                                   details={"slug": slug})
        return p


    @contextmanager
    def session_lock(self, slug: str) -> Iterator[_LockHandle]:
        """Hold the exclusive lock on a session for the block: re-entrant within a thread, exclusive
        across threads and processes. The lock file lives outside the session (#113). A session must
        exist to be locked, checked *after* the lock is held (invariant 9,
        `test_a_session_deleted_before_the_lock_is_granted_is_refused`); the check before the open is
        not authoritative and only avoids an empty lock file. Nothing is removed here (#22).
        Re-entrancy is keyed by root identity plus slug (#272)."""
        depths: dict[tuple[str, str], int] = getattr(_held_locks, "depths", None) or {}
        _held_locks.depths = depths
        # Thread-local beside `depths`: a lock is held by a thread. Not `or set()`, which would replace an empty set.
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
        # Outside the `try` (#320): its failure is about the store root, not the lock.
        self.ensure_store_dir(p.parent)
        try:
            fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            # Not `SessionNotFoundError`: the lock file no longer lives inside the session (#114's shape).
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
                # Immediately after the close, inside the same `finally`, so nothing runs between the two.
                if key in requests:
                    requests.discard(key)  # cleared first: a failed unlink is never retried later
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass  # best-effort; a leftover lock file is inert, see delete_session

