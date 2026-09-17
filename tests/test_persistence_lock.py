"""The session lock (#22, #113, #265; invariants 9 and 11), `delete_session` (#238, #469), the races they close,
and `_atomic_write` (#464; invariant 18)."""
from __future__ import annotations

import errno
import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from _fakes import full_model, out, slot
from test_persistence import _legacy, _migrate, _problem

from requivo.core import persistence as store
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import RequivoError, RevisionConflictError
from requivo.core.integrity import check_session
from requivo.core.persistence import lock as store_lock  # `_acquire`/`_release`/`_LOCK_TIMEOUT_SECONDS` since #550
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")
POSIX_ONLY = pytest.mark.skipif(store.fcntl is None, reason="POSIX-only branch; the msvcrt branch already had a "
                                "bounded wait. REASONED, NOT OBSERVED on Windows (#265).")


def _session(slug: str, **slots) -> SessionService:
    svc = SessionService()
    svc.create_session("A real request.", slug=slug)
    svc.update_model(slug, full_model(**slots))
    return svc


def _race(n: int, work) -> list:
    """`work(i)` on `n` threads released together; every outcome, a crash reported as `crash:<type>`."""
    start, outcomes, guard = threading.Barrier(n), [], threading.Lock()

    def run(i):
        start.wait()
        try:
            outcome = work(i)
        except BaseException as e:  # noqa: BLE001 - the point is to catch anything else
            outcome = f"crash:{type(e).__name__}"
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return outcomes


# ── the lock materialises nothing (#22, #113) ─────────────────────────────────


def test_a_lock_on_a_slug_with_no_session_leaves_no_trace():
    """#22: the lock created `canonical_dir(slug)` before opening `.lock` inside it (invariant 11)."""
    SessionService().create_session("A real request.", slug="real")
    def take_the_lock(slug):
        with store.session_lock(slug):
            pass  # pragma: no cover - must never be granted

    routes = {
        "session-lock": take_the_lock,
        "save-revision": lambda slug: store.save_revision(slug, out({})),
        "save-session-artifact": lambda slug: store.save_session_artifact(
            slug, "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n", source_revision=1),
        "mark-stale": lambda slug: ArtifactService(FileSessionRepository()).mark_stale(slug, ["problem"]),
    }
    for name, call in routes.items():
        with pytest.raises(RequivoError) as ei:
            call(f"ghost-{name}")
        assert ei.value.code == "session_not_found", name
        assert not store.canonical_dir(f"ghost-{name}").exists(), name
    assert sorted(p.name for p in store.session_root().iterdir()) == ["real"]

    # And a slug a failed lock touched can still be created, and still migrated rather than reported as skipped.
    meta = store.create_session("ghost-session-lock", "A request that arrives afterwards.")
    assert meta.current_revision == 0 and store.session_request("ghost-session-lock") == "A request that arrives afterwards."
    _legacy("stale-lock", "LEGACY")
    with pytest.raises(RequivoError):
        store.save_revision("stale-lock", out({}))
    receipt, _ = _migrate()
    assert (receipt["migrated"], receipt["skipped_already_present"]) == (["stale-lock"], [])
    assert _problem("stale-lock") == "LEGACY"


def test_a_session_deleted_before_the_lock_is_granted_is_refused(monkeypatch):
    """#113: the race an existence check taken *before* the lock cannot close."""
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
    assert not store.canonical_dir("vanishing").exists() and store.list_session_slugs() == []
    assert store.lock_path("vanishing").exists()   # left outside the session root, so it takes no slug with it
    assert not store.lock_root().is_relative_to(store.session_root())


def test_the_lock_still_guards_a_session_that_exists():
    """#113: the lock lives at `.requivo/locks/<slug>.lock`, outside what `session import --force` renames."""
    _session("live", problem=slot(80, "explicit", "high", "REAL"))
    assert store.lock_path("live") == store.lock_root() / "live.lock" and store.lock_path("live").exists()
    assert not (store.canonical_dir("live") / ".lock").exists()

    with store.session_lock("live"):   # re-entrant within the thread: the service holds it, every core call takes it again
        store.save_session_artifact("live", "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n", source_revision=1)
        rev, meta = store.save_revision("live", out({"problem": slot(90, "explicit", "high", "REAL v2")}))
    assert (rev, meta.current_revision, _problem("live")) == (2, 2, "REAL v2")
    assert store.read_meta("live").artifact_status["brief"].revision == 1
    assert check_session("live") == []


@POSIX_ONLY
def test_a_contended_lock_raises_within_the_deadline_instead_of_hanging(monkeypatch):
    """#265: `_LOCK_TIMEOUT_SECONDS` was honoured only in the `msvcrt` branch."""
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
    assert ei.value.code == "session_locked" and "contended" in str(ei.value)
    assert 0.25 <= elapsed < 5.0, elapsed          # bounded, not instant, not the unbounded hang it replaces
    with store.session_lock("contended"):          # once the holder releases, an ordinary acquisition succeeds
        pass


def test_reentrant_acquisition_within_a_thread_still_never_touches_the_lock_twice(monkeypatch):
    """#265: the POSIX branch moved from one blocking `flock` to a polling loop; re-entrancy stayed above it."""
    _session("nested")
    monkeypatch.setattr(store_lock, "_LOCK_TIMEOUT_SECONDS", 0.3)
    calls: list[str] = []
    real_acquire = store_lock._acquire
    monkeypatch.setattr(store_lock, "_acquire", lambda fd, slug: (calls.append(slug), real_acquire(fd, slug))[1])
    with store.session_lock("nested"), store.session_lock("nested"):
        store.save_session_artifact("nested", "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n", source_revision=1)
    assert calls == ["nested"]


@POSIX_ONLY
def test_a_non_contention_lock_error_fails_immediately_instead_of_waiting_out_the_deadline(monkeypatch):
    """A kernel resource error surfaces as what it is, at once, not relabelled as `session_locked` after 10s."""
    SessionService().create_session("A real request.", slug="broken-lock")
    monkeypatch.setattr(store_lock, "_LOCK_TIMEOUT_SECONDS", 10.0)
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
    assert ei.value.errno == errno.ENOLCK and not isinstance(ei.value, RequivoError)
    assert time.monotonic() - started < 1.0


# ── the races the lock closes (invariants 9 and 11) ───────────────────────────


def test_racing_applies_conflict_cleanly_instead_of_crashing():
    """Invariant 9: a dozen writers from one revision, one lands, the rest are told they lost (#286)."""
    svc = _session("s")
    n = 12

    def apply(i):
        try:
            svc.update_model("s", full_model(workflow=slot(80, "explicit", "high", str(i))), expected_revision=1)
            return "applied"
        except RevisionConflictError:
            return "conflict"

    outcomes = _race(n, apply)
    assert (outcomes.count("applied"), outcomes.count("conflict")) == (1, n - 1), outcomes
    meta = store.read_meta("s")
    assert (meta.current_revision, len(meta.revisions)) == (2, 2)
    assert (store.canonical_dir("s") / "revisions" / "0002-model.json").exists()


def test_racing_creations_of_one_session_all_agree_on_it():
    """Invariant 11: creation is one atomic rename, so concurrent creations of one request agree on one session."""
    svc = SessionService()
    got = _race(12, lambda i: svc.create_session("Same request.", slug="s", provider=f"p{i}").session_id)
    assert len(set(got)) == 1 and not str(got[0]).startswith("crash:"), got
    assert store.read_meta("s").session_id == got[0]
    assert store.list_session_slugs() == ["s"]               # no staging directory left behind


# ── session delete (#238, #469) ───────────────────────────────────────────────


def _platform_unlinks_a_file_it_still_holds(tmp_path) -> bool:
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


def test_only_a_delete_removes_a_lock_file():
    """`delete_session` removes the directory and the lock file; an ordinary lock take leaves its file behind."""
    _session("gone-soon")
    _session("survivor")
    assert store.canonical_dir("gone-soon").exists() and store.lock_path("gone-soon").exists()
    store.delete_session("gone-soon")
    assert not store.canonical_dir("gone-soon").exists() and not store.lock_path("gone-soon").exists()
    assert store.list_session_slugs() == ["survivor"]
    with store.session_lock("survivor"):
        pass
    assert store.lock_path("survivor").exists()


def test_deleting_a_nonexistent_slug_is_refused_with_session_not_found():
    with pytest.raises(RequivoError) as ei:
        store.delete_session("never-existed")
    assert ei.value.code == "session_not_found"
    assert not store.canonical_dir("never-existed").exists() and not store.lock_path("never-existed").exists()


def test_deleting_then_recreating_the_same_slug_succeeds():
    """#238's own acceptance criterion: the slug claim is genuinely released."""
    svc = _session("reused", problem=slot(80, "explicit", "high", "FIRST"))
    store.delete_session("reused")
    meta = svc.create_session("A completely different request.", slug="reused")
    assert meta.current_revision == 0
    assert store.session_request("reused") == "A completely different request."
    assert store.list_session_slugs() == ["reused"]


def test_a_writer_racing_an_in_flight_delete_is_refused_rather_than_writing_into_a_half_removed_directory(monkeypatch):
    SessionService().create_session("A real request.", slug="racer")
    writer_may_start, writer_finished, outcome = threading.Event(), threading.Event(), {}
    real_acquire = store_lock._acquire

    def signalling_acquire(fd, slug):
        result = real_acquire(fd, slug)
        writer_may_start.set()   # the delete now holds the lock; let the writer contend for it
        return result

    monkeypatch.setattr(store_lock, "_acquire", signalling_acquire)

    def write_after_signal():
        writer_may_start.wait(timeout=5)
        try:
            with store.session_lock("racer"):
                pass  # pragma: no cover - must never be entered; the session is already gone
            outcome["ok"] = True
        except RequivoError as e:
            outcome.update(ok=False, code=e.code)
        finally:
            writer_finished.set()

    threading.Thread(target=write_after_signal, daemon=True).start()
    store.delete_session("racer")
    assert writer_finished.wait(timeout=5), "the racing writer never finished"
    assert outcome == {"ok": False, "code": "session_not_found"}
    assert not store.canonical_dir("racer").exists()


def test_the_lock_file_is_gone_before_the_lock_is_released_not_after(tmp_path, monkeypatch):
    """#22: unlinked inside the lock where the platform allows it; the other ordering is unsafe."""
    SessionService().create_session("A real request.", slug="ordered")
    lock_path, observed = store.lock_path("ordered"), {}
    real_release = store_lock._release

    def observing_release(fd):
        observed["existed_at_release"] = lock_path.exists()
        return real_release(fd)

    monkeypatch.setattr(store_lock, "_release", observing_release)
    store.delete_session("ordered")
    # The third state is a claim, not a shrug (#469): a platform that refuses the in-lock unlink still had the file.
    assert observed.get("existed_at_release") is (not _platform_unlinks_a_file_it_still_holds(tmp_path))
    assert not lock_path.exists() and not store.canonical_dir("ordered").exists()


def test_a_delete_whose_in_lock_unlink_is_refused_still_removes_the_lock_file(monkeypatch):
    """#469: on Windows `os.open` takes no share-delete, so the in-lock unlink is refused; the teardown removes it."""
    _session("refused")                                # create_session is lock-free; an apply mints the lock file
    lock_path, refusals = store.lock_path("refused"), []
    assert lock_path.exists()
    real_unlink = Path.unlink

    def windows_shaped_unlink(self, *args, **kwargs):
        if self == lock_path and not refusals:
            refusals.append(self)
            raise PermissionError(13, "Access is denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", windows_shaped_unlink)
    store.delete_session("refused")
    assert refusals, "the staged refusal never fired, so this measured the ordinary path"
    assert not lock_path.exists() and not store.canonical_dir("refused").exists()


def test_delete_waits_for_a_concurrent_writer_then_removes_what_it_wrote():
    """The paired must-succeed half of the race above: a delete is serialised behind a live writer."""
    _session("patient")
    writer_holds_lock, writer_may_finish, delete_finished = threading.Event(), threading.Event(), threading.Event()

    def hold_and_write():
        with store.session_lock("patient"):
            store.save_session_artifact("patient", "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n", source_revision=1)
            writer_holds_lock.set()
            writer_may_finish.wait(timeout=5)

    writer = threading.Thread(target=hold_and_write, daemon=True)
    writer.start()
    assert writer_holds_lock.wait(timeout=5), "the writer never took the lock"
    threading.Thread(target=lambda: (store.delete_session("patient"), delete_finished.set()), daemon=True).start()
    assert not delete_finished.wait(timeout=0.2), "delete_session returned while the writer still held the lock"
    writer_may_finish.set()
    writer.join(timeout=5)
    assert delete_finished.wait(timeout=5), "delete_session never finished once the writer released"
    assert not store.canonical_dir("patient").exists() and not store.lock_path("patient").exists()


# ── _atomic_write (#464, invariant 18) ────────────────────────────────────────


def test_atomic_write_passes_newline_empty_to_disable_translation(tmp_path, monkeypatch):
    """#464: universal-newline translation corrupts a lone CR on Windows; and the write still lands byte for byte."""
    captured = {}
    real_open = Path.open

    def spy(self, *args, **kwargs):
        if ".probe.json." in self.name:
            captured["newline"] = kwargs.get("newline", "NOT PASSED")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    store._atomic_write(tmp_path / "probe.json", "a line\r\nwith a CR\r\nand a plain one\n")
    assert captured.get("newline") == "", captured
    assert (tmp_path / "probe.json").read_bytes() == b"a line\r\nwith a CR\r\nand a plain one\n"
    assert list(tmp_path.iterdir()) == [tmp_path / "probe.json"]   # the sidecar is renamed, never left behind


def test_atomic_write_survives_a_transient_permission_error(tmp_path, monkeypatch):
    """Invariant 18: on Windows a scanner holding the destination makes `rename` raise; retried, briefly."""
    target = tmp_path / "model.json"
    target.write_text("old", encoding="utf-8")
    attempts = {"n": 0}
    real_replace = Path.replace

    def flaky(self, dst):
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise PermissionError(13, "Access is denied")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky)
    store._atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert attempts["n"] == 4, "the write did not actually go through the retry path"


def test_atomic_write_still_gives_up_on_a_permanent_permission_error(tmp_path, monkeypatch):
    """Bounded, and the bound is the point: exactly `_REPLACE_ATTEMPTS`, then the original error."""
    target = tmp_path / "model.json"
    target.write_text("old", encoding="utf-8")
    attempts = {"n": 0}

    def always_denied(self, dst):
        attempts["n"] += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", always_denied)
    with pytest.raises(PermissionError):
        store._atomic_write(target, "new")
    assert attempts["n"] == store._REPLACE_ATTEMPTS
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".*tmp")), "scratch left behind after a failed write"


def test_a_failed_atomic_write_leaves_no_scratch_file(workspace):
    d = workspace / "scratch"
    d.mkdir()
    with pytest.raises(TypeError):
        store._atomic_write(d / "model.json", None)  # type: ignore[arg-type]
    assert list(d.iterdir()) == []


def test_concurrent_atomic_writes_do_not_collide_on_a_temp_file(workspace):
    """The scratch file must be private to the call: one writer's payload lands, never a blend."""
    d = workspace / "scratch"
    d.mkdir()
    target, errors = d / "model.json", []

    def write(n: int) -> None:
        try:
            store._atomic_write(target, f"payload-{n}\n" * 200)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(set(target.read_text(encoding="utf-8").splitlines())) == 1
    assert not list(d.glob(".*tmp"))
