"""The session lock (#22, #113, #265) and `delete_session` (#238) -- the two guards around a
session's lifecycle rather than its content. Split from a single `core/persistence/` guards module
by #550 and again by #555; `test_persistence.py`, `test_persistence_slugs.py` and
`test_persistence_store.py` cover the rest of the store.

Invariant 9: a precondition is held across the writes it authorises. The lock file lives outside
the session directory since #113 (`.requivo/locks/<slug>.lock`), so a delete's own unlink races the
same hazard a session directory rename does -- both are pinned here, together.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from conftest import full_model as _full_model
from conftest import slot as _slot
from requivo.core import persistence as store
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import RequivoError
from requivo.core.integrity import check_session

# `_acquire`/`_release`/`_LOCK_TIMEOUT_SECONDS` moved to `core/persistence/lock.py` by #550, and
# `Store.session_lock` (in `lock.py`'s own `_LockMixin`) reads them off *that* module's globals --
# patching the package-level re-export (`store._acquire`) is a second binding that does not reach
# the call site, so the lock-behaviour tests below patch this module directly instead.
from requivo.core.persistence import lock as store_lock
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService


def _engine_output(**overrides):
    from requivo.core.contracts import EngineOutput
    return EngineOutput.model_validate(_full_model(**overrides))



def _legacy(slug: str, marker: str) -> None:
    """A legacy out/<slug>/ session whose `problem` slot is identifiable."""
    d = store.legacy_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.json").write_text(json.dumps(
        _full_model(**{"problem": _slot(10, "inferred", "low", marker)})))


def _problem(slug: str, revision: int | None = None) -> str:
    out = (store.load_session_model(slug) if revision is None
           else store.load_revision_model(slug, revision))
    return out.model["problem"].value


# ── #22: session_lock() must not materialise the session it guards ──────────────


def _ghost_locking_calls() -> dict:
    """Every route that takes the session lock on a slug the caller has not proven exists.

    Named as a table rather than tested one by one because the defect was never in any of them: it
    was in the lock, and each of these is only a way to reach it. A route added later that locks
    before it reads belongs here, not in a test of its own."""

    def take_the_lock(slug):
        with store.session_lock(slug):
            pass

    # The keys become slugs, so they are hyphenated: an underscore is not a legal slug character and
    # `validate_slug` would refuse the name before the lock could be reached at all.
    return {
        "session-lock": take_the_lock,
        "save-revision": lambda slug: store.save_revision(slug, _engine_output()),
        "save-session-artifact": lambda slug: store.save_session_artifact(
            slug, "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n", source_revision=1),
        "mark-stale": lambda slug: ArtifactService(FileSessionRepository()).mark_stale(slug, ["problem"]),
    }


def test_a_lock_on_a_slug_with_no_session_leaves_no_trace(workspace):
    """The lock created `canonical_dir(slug)` before opening `.lock` inside it, so taking it on a slug
with no session left a directory behind holding nothing else. That directory is invisible to
`list_session_slugs` (no session.json) and non-empty, so `create_session`'s rename — the *only*
claim on a slug under invariant 11 — lost to a session nobody had created, and the user was told
one already existed that neither they nor the tool could see."""
    SessionService().create_session("A real request.", slug="real")
    before = sorted(p.name for p in store.session_root().iterdir())
    assert before == ["real"], "the control session is not where this test is looking"

    for name, call in _ghost_locking_calls().items():
        with pytest.raises(RequivoError) as ei:
            call(f"ghost-{name}")
        assert ei.value.code == "session_not_found", name
        assert not store.canonical_dir(f"ghost-{name}").exists(), name

    assert sorted(p.name for p in store.session_root().iterdir()) == before
    assert store.list_session_slugs() == ["real"]


def test_a_session_deleted_before_the_lock_is_granted_is_refused(workspace, monkeypatch):
    """The race an existence check taken *before* the lock cannot close. This used to be closed by
accident: the lock file lived inside the session, so `os.open` raised `FileNotFoundError` when
the directory had gone and that arm mapped it onto "no such session". #113 moved the lock out of
the session directory, and with it that accident — opening `.requivo/locks/<slug>.lock` says
nothing at all about whether `<slug>` is a session."""
    SessionService().create_session("A real request.", slug="vanishing")
    real_acquire = store_lock._acquire

    def deleting_acquire(fd, slug):
        shutil.rmtree(store.canonical_dir(slug))
        return real_acquire(fd, slug)

    monkeypatch.setattr(store_lock, "_acquire", deleting_acquire)

    with pytest.raises(RequivoError) as ei:
        with store.session_lock("vanishing"):
            pass                                    # pragma: no cover - the lock must not be granted
    assert ei.value.code == "session_not_found"
    assert not store.canonical_dir("vanishing").exists(), "the refusal must not recreate it"
    assert store.list_session_slugs() == []
    # And the lock file it left behind is outside the session root, so it takes no slug with it.
    assert store.lock_path("vanishing").exists()
    assert not store.lock_root().is_relative_to(store.session_root())


def test_a_slug_a_failed_lock_touched_can_still_be_created(workspace):
    """The reproduction from the issue, end to end. `list_session_slugs` and `create_session` have to
    agree about whether a slug is taken — the refusal was false precisely because they did not."""
    with pytest.raises(RequivoError):
        store.save_session_artifact("later", "brief", ARTIFACT_FILENAMES["brief"], "x", source_revision=1)

    assert "later" not in store.list_session_slugs()
    meta = store.create_session("later", "A request that arrives afterwards.")
    assert meta.current_revision == 0
    assert store.list_session_slugs() == ["later"]
    assert store.session_request("later") == "A request that arrives afterwards."


def test_a_migration_onto_such_a_slug_is_performed_not_reported_as_skipped(workspace, capsys):
    """Why this is more than a misleading message. `migrate_legacy` claims its slug through
    `create_session`, and the bulk sweep turns `SessionExistsError` into `skipped_already_present` —
    a row that reads as a decision. A ghost directory made the sweep report a session it had refused
    to migrate as one that was already there, and the legacy work silently never landed."""
    from requivo.deterministic.sessions import _cmd_session_migrate

    _legacy("stale-lock", "LEGACY")
    with pytest.raises(RequivoError):
        store.save_revision("stale-lock", _engine_output())

    _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["migrated"] == ["stale-lock"]
    assert receipt["skipped_already_present"] == []
    assert _problem("stale-lock") == "LEGACY"


def test_the_lock_still_guards_a_session_that_exists(workspace):
    """The other direction, and the one a fix here can break silently. The lock's job is the compound
mutations on sessions that *do* exist: `save_revision` and `save_session_artifact` write files
under a session directory while holding it, and the service layer nests it around several core
calls. See #113."""
    svc = SessionService()
    svc.create_session("A real request.", slug="live")
    svc.update_model("live", _full_model(**{"problem": _slot(80, "explicit", "high", "REAL")}))

    lock_file = store.lock_path("live")
    assert lock_file.exists(), "a writer that holds the lock leaves the lockfile behind"
    assert lock_file == store.lock_root() / "live.lock"
    assert not (store.canonical_dir("live") / ".lock").exists(), (
        "the lock is back inside the directory `session import --force` renames")

    with store.session_lock("live"):
        # Re-entrant within the thread: the service takes it around a whole update and every core
        # call inside takes it again. A guard that refused the second acquisition would deadlock.
        store.save_session_artifact("live", "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n",
                                    source_revision=1)
        rev, meta = store.save_revision(
            "live", _engine_output(**{"problem": _slot(90, "explicit", "high", "REAL v2")}))

    assert (rev, meta.current_revision) == (2, 2)
    assert _problem("live") == "REAL v2"
    assert store.read_meta("live").artifact_status["brief"].revision == 1
    assert [p.code for p in check_session("live")] == []


@pytest.mark.skipif(store.fcntl is None, reason="POSIX-only branch: fcntl.flock has no Windows "
                     "equivalent here, and the msvcrt branch already had a bounded wait. "
                     "REASONED, NOT OBSERVED on Windows -- see #265.")
def test_a_contended_lock_raises_within_the_deadline_instead_of_hanging(workspace, monkeypatch):
    """#265. `_LOCK_TIMEOUT_SECONDS` was honoured only in the `msvcrt` branch; on POSIX,
`fcntl.flock(fd, fcntl.LOCK_EX)` blocked forever with no message, so a stuck holder (a SIGSTOPped
process, a debugger, an NFS-mounted workspace) froze the CLI on the primary platforms instead of
raising the `SessionLockedError` Windows already had. The deadline is shortened so this proves
the bound rather than the hang."""
    SessionService().create_session("A real request.", slug="contended")
    monkeypatch.setattr(store_lock, "_LOCK_TIMEOUT_SECONDS", 0.3)

    lock_file = store.lock_path("contended")
    store.ensure_store_dir(lock_file.parent)
    holder_fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    store.fcntl.flock(holder_fd, store.fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with pytest.raises(RequivoError) as ei:
            with store.session_lock("contended"):
                pass  # pragma: no cover - must never be granted while the holder is live
        elapsed = time.monotonic() - started
    finally:
        store.fcntl.flock(holder_fd, store.fcntl.LOCK_UN)
        os.close(holder_fd)

    assert ei.value.code == "session_locked"
    assert "contended" in str(ei.value)
    # Bounded, not instant (a spin that returns before the holder ever really contended would prove
    # nothing) and not the unbounded hang it replaces (an unpatched 30s deadline here would make
    # this assertion the reason the whole suite takes half a minute to fail).
    assert 0.25 <= elapsed < 5.0, elapsed

    # The session is otherwise unharmed: once the holder releases, an ordinary acquisition succeeds.
    with store.session_lock("contended"):
        pass


def test_reentrant_acquisition_within_a_thread_still_never_touches_the_lock_twice(workspace,
                                                                                   monkeypatch):
    """The POSIX branch moved from one blocking `flock` call to a polling loop (#265); this pins that
the re-entrancy invariant 9 relies on is unaffected, because it is decided one layer above
`_acquire` and never reaches it on a nested call. `session_lock`'s own `_held_locks` depth
counter is what makes nested acquisition safe -- a second `with session_lock(slug):` on the same
thread increments the counter and returns without calling `_acquire` again at all."""
    svc = SessionService()
    svc.create_session("A real request.", slug="nested")
    svc.update_model("nested", _full_model())
    monkeypatch.setattr(store_lock, "_LOCK_TIMEOUT_SECONDS", 0.3)
    calls: list[str] = []
    real_acquire = store_lock._acquire

    def counting_acquire(fd, slug):
        calls.append(slug)
        return real_acquire(fd, slug)

    monkeypatch.setattr(store_lock, "_acquire", counting_acquire)

    with store.session_lock("nested"):
        with store.session_lock("nested"):
            store.save_session_artifact("nested", "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n",
                                        source_revision=1)

    assert calls == ["nested"], (
        f"a nested acquisition on the same thread must not call _acquire again: {calls}")


@pytest.mark.skipif(store.fcntl is None, reason="POSIX-only branch. REASONED, NOT OBSERVED on "
                     "Windows -- see #265.")
def test_a_non_contention_lock_error_fails_immediately_instead_of_waiting_out_the_deadline(
        workspace, monkeypatch):
    """Caught in review before this shipped: a first draft caught a bare `OSError` around the poll loop,
which also catches `ENOLCK`, `EBADF` or a filesystem that refuses `flock` outright -- none of
which will ever resolve by waiting. Masking one of those behind the retry loop for up to 30
seconds and then raising `SessionLockedError` ("locked by another process") would trade a loud,
honest failure for a quiet, misleading one. Only `BlockingIOError`"""
    import errno

    SessionService().create_session("A real request.", slug="broken-lock")
    monkeypatch.setattr(store_lock, "_LOCK_TIMEOUT_SECONDS", 10.0)  # would dominate the test if hit
    real_flock = store.fcntl.flock

    def refusing_flock(fd, op):
        if op & store.fcntl.LOCK_EX:
            raise OSError(errno.ENOLCK, "No locks available")
        return real_flock(fd, op)

    monkeypatch.setattr(store.fcntl, "flock", refusing_flock)

    started = time.monotonic()
    with pytest.raises(OSError) as ei:
        with store.session_lock("broken-lock"):
            pass  # pragma: no cover - must never be granted
    elapsed = time.monotonic() - started

    assert ei.value.errno == errno.ENOLCK
    assert not isinstance(ei.value, RequivoError), (
        "a kernel resource error must surface as what it is, not be relabelled as SessionLockedError")
    # Immediate, not the 10s deadline this test set specifically so a masked error would be visible.
    assert elapsed < 1.0, elapsed


# ── #238: session delete ──────────────────────────────────────────────────────


def _platform_unlinks_a_file_it_still_holds(tmp_path) -> bool:
    """Does this platform permit unlinking a file this process holds an open fd on?

    Measured, not derived from `sys.platform`: the question is what the filesystem and the open mode
    actually allow here, and a name-based guess is the shape invariant 17's sequel was written about
    -- a check whose answer depends on where it runs rather than on what it looked at."""
    probe = tmp_path / "held.probe"
    fd = os.open(probe, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        probe.unlink()
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
        probe.unlink(missing_ok=True)


def test_delete_session_removes_the_directory_and_the_lock_file(workspace):
    """The uncontended positive control every negative test below needs: deleting a session that
    nothing else is touching must actually remove both the directory and the lock file the issue's
    own acceptance criteria name, not merely refuse to error."""
    svc = SessionService()
    svc.create_session("A real request.", slug="gone-soon")
    svc.update_model("gone-soon", _full_model())
    assert store.canonical_dir("gone-soon").exists()
    assert store.lock_path("gone-soon").exists()

    store.delete_session("gone-soon")

    assert not store.canonical_dir("gone-soon").exists()
    assert not store.lock_path("gone-soon").exists()
    assert "gone-soon" not in store.list_session_slugs()


def test_deleting_a_nonexistent_slug_is_refused_with_session_not_found(workspace):
    with pytest.raises(RequivoError) as ei:
        store.delete_session("never-existed")
    assert ei.value.code == "session_not_found"
    # Refusing must not conjure anything -- no directory, no lock file, for a slug nothing ever
    # claimed (the same must-not-fire shape `test_a_lock_on_a_slug_with_no_session_leaves_no_trace`
    # already pins for the lock alone).
    assert not store.canonical_dir("never-existed").exists()
    assert not store.lock_path("never-existed").exists()


def test_deleting_then_recreating_the_same_slug_succeeds(workspace):
    """The issue's own acceptance criterion, verbatim: the slug claim is genuinely released.
    Invariant 11's claim on a slug is `create_session`'s rename -- if delete left anything behind
    that rename could lose to (a ghost directory, a stale lock treated as an occupant), re-creating
    the identical slug would either fail or silently inherit residue from the deleted session."""
    svc = SessionService()
    svc.create_session("The first occupant of this slug.", slug="reused")
    svc.update_model("reused", _full_model(**{"problem": _slot(80, "explicit", "high", "FIRST")}))
    store.delete_session("reused")

    meta = svc.create_session("A completely different request.", slug="reused")
    assert meta.current_revision == 0
    assert store.session_request("reused") == "A completely different request."
    assert store.list_session_slugs() == ["reused"]


def test_a_writer_racing_an_in_flight_delete_is_refused_rather_than_writing_into_a_half_removed_directory(
        workspace, monkeypatch):
    """The issue's own acceptance criterion: a delete racing a concurrent writer must not leave a
half-removed directory."""
    SessionService().create_session("A real request.", slug="racer")
    writer_may_start = threading.Event()
    real_acquire = store_lock._acquire

    def signalling_acquire(fd, slug):
        result = real_acquire(fd, slug)
        writer_may_start.set()   # the delete now holds the lock; let the writer contend for it
        return result

    monkeypatch.setattr(store_lock, "_acquire", signalling_acquire)

    outcome: dict = {}
    writer_finished = threading.Event()

    def write_after_signal():
        writer_may_start.wait(timeout=5)
        try:
            with store.session_lock("racer"):
                pass  # pragma: no cover - must never be entered; the session is already gone
            outcome["ok"] = True
        except RequivoError as e:
            outcome["ok"] = False
            outcome["code"] = e.code
        finally:
            writer_finished.set()

    t = threading.Thread(target=write_after_signal, daemon=True)
    t.start()
    store.delete_session("racer")
    assert writer_finished.wait(timeout=5), "the racing writer never finished"

    assert outcome.get("ok") is False, f"a writer racing an in-flight delete must be refused: {outcome}"
    assert outcome["code"] == "session_not_found"
    assert not store.canonical_dir("racer").exists()


def test_the_lock_file_is_gone_before_the_lock_is_released_not_after(workspace, tmp_path,
                                                                    monkeypatch):
    """Found in review: the first draft unlinked the lock file *after* `session_lock`'s own release,
which reopens the exact "unlinking a lock file a concurrent process may be holding" hazard
`session_lock`'s own docstring says #22 rejected as a repair -- because invariant 11 lets a
second actor `create_session` the identical slug the instant `session_exists` goes false, which
is the moment `rmtree` returns, still inside this method's own critical section."""
    SessionService().create_session("A real request.", slug="ordered")
    lock_path = store.lock_path("ordered")
    real_release = store_lock._release
    observed: dict = {}

    def observing_release(fd):
        observed["lock_file_existed_at_release"] = lock_path.exists()
        return real_release(fd)

    monkeypatch.setattr(store_lock, "_release", observing_release)

    store.delete_session("ordered")

    if _platform_unlinks_a_file_it_still_holds(tmp_path):
        assert observed.get("lock_file_existed_at_release") is False, (
            "the lock file must already be gone by the time the lock is released, not unlinked "
            "afterwards -- see delete_session's own docstring for why the other ordering is unsafe")
    else:
        # The third state, and it is a claim rather than a shrug (#469). Where the platform refuses a
        # same-process unlink of a held file, the zero-window ordering above is not merely untested --
        # it is unreachable, and asserting it here would redden a store behaving as correctly as the
        # platform permits. What must still hold is that the file does not survive the call, which the
        # assertion below states for both branches. The fallback path's own guard,
        # test_a_delete_whose_in_lock_unlink_is_refused_still_removes_the_lock_file, stages this
        # refusal on every platform, so nothing about it goes unexercised on the legs that pass here.
        assert observed.get("lock_file_existed_at_release") is True, (
            "this platform refuses to unlink a file it still holds, so the in-lock unlink cannot "
            "have succeeded -- if it did, this probe is measuring the wrong thing")
    assert not lock_path.exists()
    assert not store.canonical_dir("ordered").exists()


def test_a_delete_whose_in_lock_unlink_is_refused_still_removes_the_lock_file(workspace, tmp_path,
                                                                              monkeypatch):
    """#469. On Windows `os.open` takes no share-delete, so `delete_session`'s unlink of the lock file
it is still holding is refused every time -- not "can raise instead", which is how the method's
docstring first put it. The old code swallowed that `OSError` and left the file, so every Windows
`session delete` left residue `doctor --json` then reported as `unmatched`: the install
diagnostic accusing the user of a state Requivo had just created."""
    svc = SessionService()
    svc.create_session("A real request.", slug="refused")
    svc.update_model("refused", _full_model())   # create_session is lock-free (invariant 11); an
    lock_path = store.lock_path("refused")       # apply is what actually mints the lock file
    assert lock_path.exists()

    real_unlink = Path.unlink
    refusals: list = []

    def windows_shaped_unlink(self, *args, **kwargs):
        if self == lock_path and not refusals:
            refusals.append(self)
            raise PermissionError(13, "Access is denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", windows_shaped_unlink)

    store.delete_session("refused")

    assert refusals, (
        "the staged refusal never fired, so this test measured the ordinary path and says nothing "
        "about the fallback it exists for")
    assert not lock_path.exists(), (
        "a platform that refuses the in-lock unlink must still not be left holding the lock file -- "
        "it is removed as the lock tears down, see _LockHandle.unlink_on_release")
    assert not store.canonical_dir("refused").exists()


def test_a_lock_taken_without_a_delete_never_removes_its_lock_file(workspace):
    """The must-not-fire twin of the fallback above. `_LockHandle` records its request against the
    lock *key* on a thread-local, so a bug that let the flag survive one `session_lock` into the next
    would silently unlink the lock file of every subsequently locked session -- reopening exactly the
    "unlinking a lock file a concurrent process may be holding" hazard #22 rejected, from the
    opposite direction and with nothing else in the suite watching for it."""
    svc = SessionService()
    svc.create_session("A real request.", slug="deleted-one")
    svc.create_session("Another real request.", slug="survivor")
    svc.update_model("deleted-one", _full_model())
    store.delete_session("deleted-one")

    with store.session_lock("survivor"):
        pass
    assert store.lock_path("survivor").exists(), (
        "an ordinary lock take must leave its lock file behind; only a delete removes one")


def test_delete_waits_for_a_concurrent_writer_then_removes_what_it_wrote(workspace):
    """The paired must-succeed half of the race above: a writer already using the session when
    delete is asked for must not be interrupted mid-write, and delete must still succeed once the
    writer is done -- genuinely serialised, not merely lucky. The middle assertion is the one that
    would catch a regression to an unlocked or best-effort delete: it proves `delete_session` is
    still blocked while the writer holds the lock, not that it happens to finish after it."""
    svc = SessionService()
    svc.create_session("A real request.", slug="patient")
    svc.update_model("patient", _full_model())
    writer_holds_lock = threading.Event()
    writer_may_finish = threading.Event()

    def hold_and_write():
        with store.session_lock("patient"):
            store.save_session_artifact("patient", "brief", ARTIFACT_FILENAMES["brief"],
                                        "# Brief\n", source_revision=1)
            writer_holds_lock.set()
            writer_may_finish.wait(timeout=5)

    writer = threading.Thread(target=hold_and_write, daemon=True)
    writer.start()
    assert writer_holds_lock.wait(timeout=5), "the writer never took the lock"

    delete_finished = threading.Event()

    def do_delete():
        store.delete_session("patient")
        delete_finished.set()

    deleter = threading.Thread(target=do_delete, daemon=True)
    deleter.start()

    # Must genuinely be waiting on the writer's lock, not racing ahead of it.
    assert not delete_finished.wait(timeout=0.2), (
        "delete_session returned while the writer still held the lock -- it is not serialised")

    writer_may_finish.set()
    writer.join(timeout=5)
    assert delete_finished.wait(timeout=5), "delete_session never finished once the writer released"

    assert not store.canonical_dir("patient").exists()
    assert not store.lock_path("patient").exists()
