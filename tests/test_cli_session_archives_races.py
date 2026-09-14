"""`session import`/`session export` under concurrency and mid-operation races — #113, #111, #114.

Split out of `test_cli_session_archives.py` by #555, once that file outgrew one module; the sibling
covers the archive-shape validation itself (size/count/entry caps, unsafe paths, the `invalid_archive`
taxonomy). `_zip`/`_good_entries`/`_import_error` are duplicated from there rather than imported, per
this suite's own convention of keeping test-module helpers local (`tests/_fakes.py` makes the
argument).

Three stories, in order: #113 (a forced replacement is serialised against the writers of the session
it replaces), #111 (a session created while the archive was being read is not destroyed), and #114 (a
stray directory at the target slug answers the same on every platform). The shared harness is
`tests/_cli_harness.py`.
"""
from __future__ import annotations

import io
import json
import shutil
import threading
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _cli_harness import _full_model, _run, _run_json, _run_stdin

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.persistence import _REPLACE_ATTEMPTS


def _zip(path, entries: dict) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)


def _good_entries(slug="imported", revision=0):
    meta = {"format_version": 1, "session_id": "abc", "slug": slug, "created_at": "t",
            "updated_at": "t", "current_revision": revision}
    entries = {f"{slug}/session.json": json.dumps(meta), f"{slug}/request.md": "A request."}
    if revision:
        entries[f"{slug}/model.json"] = json.dumps(_full_model())
    return entries


def _import_error(archive) -> dict:
    """Drive the real CLI and return the structured envelope a `--json` consumer sees."""
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "import", str(archive), "--json"], client=None)
    assert e.value.code == 1
    return json.loads(buf.getvalue())


# ── #113: a forced replacement is serialised against the writers of the session it replaces ─────
#
# `save_revision` resolves the session directory once and then writes by *pathname*; `session_lock`
# holds an fd on an *inode*. Those two descriptions agree only while nothing renames the directory,
# and `_swap_in` renames it. So a writer sitting inside `save_revision` while a forced import runs
# goes on writing into the *newly imported* directory, and a third process opening the lock finds a
# different inode and acquires immediately — two writers holding one slug's lock, which is invariant
# 9's own failure mode.
#
# The guard is that the lock no longer lives inside the directory being renamed (`.requivo/locks/`),
# so `_swap_in` holds it like every other compound write. A test asserting only that the import
# succeeded passes on the defect; these two assertions do not.


def _paused_between_the_writes(monkeypatch, slug: str, at_the_gate, release):
    """Freeze `save_revision` on `slug` between its file writes and its metadata write.

    That gap is where the damage lands: revisions/NNNN-model.json and model.json are already on
    disk, `session.json` is about to be written through a freshly resolved `canonical_dir(slug)`,
    and the lock is held across the whole of it. Patching `write_meta` rather than sleeping keeps
    the window deterministic on every leg instead of timing-dependent on the slow ones.

    **Patched on the `Store` class, not the module function** (#272). `store.save_revision` is now
    a thin ambient-default wrapper over `Store.save_revision`, which calls `self.write_meta(...)` —
    a method lookup on the class, not the module-level `write_meta` name -- so patching the module
    function no longer intercepts it. The class attribute is what every instance's `self.write_meta`
    actually resolves to, ambient or explicit alike, which is what makes this the correct target now."""
    real_write_meta = store.Store.write_meta

    def paused(self, s, meta):
        if s == slug and not at_the_gate.is_set():
            at_the_gate.set()
            assert release.wait(20), "the test never released the paused writer"
        return real_write_meta(self, s, meta)

    monkeypatch.setattr(store.Store, "write_meta", paused)


def test_a_forced_import_serialises_against_a_concurrent_writer(workspace, tmp_path, monkeypatch):
    """#113, both halves."""
    _run(["session", "init", "The original.", "--slug", "dup", "--json"])
    _run_stdin(["model", "apply", "dup", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    resident_id = store.read_meta("dup").session_id
    _zip(tmp_path / "dup.zip", _good_entries("dup"))          # a different session, session_id "abc"
    assert resident_id != "abc", "the two sessions must be distinguishable by identity"

    at_the_gate, release = threading.Event(), threading.Event()
    _paused_between_the_writes(monkeypatch, "dup", at_the_gate, release)

    failures: list[BaseException] = []

    def _capture(fn):
        def run():
            try:
                fn()
            except BaseException as e:                        # noqa: BLE001 - reported, not swallowed
                failures.append(e)
        return run

    writer = threading.Thread(target=_capture(
        lambda: store.save_revision("dup", _engine_output_for_dup())), daemon=True)
    writer.start()
    assert at_the_gate.wait(10), "the writer never reached the gap between its writes"

    imported = threading.Event()

    def _import_then_signal():
        _run(["session", "import", str(tmp_path / "dup.zip"), "--force", "--json"])
        imported.set()

    importer = threading.Thread(target=_capture(_import_then_signal), daemon=True)
    importer.start()

    # Long enough for an unguarded import to have finished the whole swap; under the guard it is
    # still blocked on the writer's lock. Measured at ~0.1s unguarded, so 1.5s is not a close call.
    assert not imported.wait(1.5), (
        "the forced import replaced the session while a writer held its lock")

    probe_acquired = threading.Event()
    probe = threading.Thread(target=_capture(
        lambda: _take_the_lock("dup", probe_acquired)), daemon=True)
    probe.start()
    assert not probe_acquired.wait(0.5), (
        "a third caller acquired the lock while a writer held it — the swap moved the lock away")

    release.set()
    for t in (writer, importer, probe):
        t.join(timeout=20)
    assert not [t for t in (writer, importer, probe) if t.is_alive()], "a thread did not finish"
    assert failures == [], f"a worker raised: {failures}"
    # The positive controls for the two negatives above: released, both must actually happen.
    assert imported.is_set(), "the positive control failed: the import never completed at all"
    assert probe_acquired.is_set(), "the positive control failed: the probe never took the lock at all"

    meta = store.read_meta("dup")
    assert meta.session_id == "abc", "the import inherited the replaced session's identity"
    assert meta.current_revision == 0 and meta.revisions == []
    assert store.list_session_slugs() == ["dup"]
    assert _run_json(["session", "verify", "dup", "--json"])["ok"] is True


def _take_the_lock(slug: str, acquired) -> None:
    with store.session_lock(slug):
        acquired.set()


def _engine_output_for_dup():
    from requivo.core.contracts import EngineOutput

    return EngineOutput.model_validate(_full_model())



def test_export_excludes_the_lock_file_and_waits_for_the_writer(workspace, tmp_path, monkeypatch):
    """An export reads several files that must agree: outside the lock it can combine an old
    `session.json` with a new `model.json`. `.lock` has no meaning outside this machine's own
    coordination, so the fixture plants the residue rather than waiting for a writer to. **#293:**
    `held.is_set()` read after `t.join()` is vacuously True either way; `acquired` is the real
    control."""
    import time

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    assert store.lock_path("s").exists(), "the writer left its lock outside the session"
    assert not (store.canonical_dir("s") / ".lock").exists()
    (store.canonical_dir("s") / ".lock").touch()               # legacy residue, as an older one left it

    acquired = threading.Event()
    held = threading.Event()

    def hold_the_lock():
        with store.session_lock("s"):
            acquired.set()
            time.sleep(0.4)
            held.set()

    t = threading.Thread(target=hold_the_lock)
    t.start()
    assert acquired.wait(10), "the writer never took the lock — contention was never reached"
    dest = tmp_path / "s.zip"
    _run(["session", "export", "s", "-o", str(dest), "--json"])
    # Read before join(): join() waits for the writer to finish regardless of whether export waited
    # for it, so held would read True either way by the time join() returns. Read here, it can only
    # be set if export's own read blocked until the writer released the lock.
    assert held.is_set(), "the export read the session while a writer held it"
    t.join(timeout=10)

    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
    assert not [n for n in names if ".lock" in n]
    assert "s/model.json" in names and "s/revisions/0001-model.json" in names


def test_session_export_survives_a_transient_permission_error(workspace, tmp_path, monkeypatch):
    """The same cause invariant 18's `_atomic_write` and `session restore`'s `_replace_with_retry`
    already retry: on Windows the final `rename` of the freshly written archive can fail with a
    transient `PermissionError` when a scanner or the Search Indexer briefly opens it microseconds
    after it lands (#524). `_cmd_session_export`'s `tmp.replace(dest)` now goes through the same
    helper, the identical shape to `test_session_restore_survives_a_transient_permission_error`."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)

    attempts = {"n": 0}
    real_replace = Path.replace

    def flaky(self, dst):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError(13, "Access is denied")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky)
    dest = tmp_path / "s.zip"
    _run(["session", "export", "s", "-o", str(dest), "--json"])
    assert attempts["n"] == 3, "the export did not actually go through the retry path"
    assert dest.exists(), "the retried rename never landed the archive"
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
    assert "s/model.json" in names and "s/revisions/0001-model.json" in names


def test_session_export_still_gives_up_on_a_permanent_permission_error(workspace, tmp_path,
                                                                          monkeypatch):
    """Bounded, and the bound is the point: a genuinely unwritable destination must still fail
    loudly and quickly, and leave no completed archive behind under a scratch name — the failure
    mode #524 was filed for is a finished export that reports a traceback while `finally:
    tmp.unlink(missing_ok=True)` quietly deletes it."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)

    attempts = {"n": 0}

    def always_denied(self, dst):
        attempts["n"] += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", always_denied)
    dest = tmp_path / "s.zip"
    with pytest.raises(PermissionError):
        app(["session", "export", "s", "-o", str(dest)], client=None)
    # The attempt count is what makes this a test of the *retry* rather than of `replace`: without
    # it every assertion here holds identically with the loop deleted (found in review of #483).
    assert attempts["n"] == _REPLACE_ATTEMPTS, (
        f"expected exactly {_REPLACE_ATTEMPTS} attempts before giving up, got {attempts['n']}")
    assert not dest.exists()
    assert not list(tmp_path.glob(".*.part")), "scratch left behind after a failed export"


# ── #111: a session created while the archive was being read is not destroyed ───
#
# `session import` used to decide the collision question twice: `session_exists(slug) and not
# --force` before the extraction, and `replaced = target.exists()` after it. Between those two
# decisions sits the whole unzip, and a session created in that window was moved aside and then
# `rmtree`d — destroyed by an import whose user was never asked for `--force`, because at the moment
# they would have been asked there was nothing to force past.
#
# Invariant 9 in the one verb that writes a whole session: a precondition is held across the writes
# it authorises. The two arms below are what holds it — the free-slug arm claims by rename, which is
# invariant 11's rule and the only thing that makes the window safe rather than merely narrow.


def test_a_session_created_during_the_extraction_window_is_refused_not_destroyed(
        workspace, tmp_path, monkeypatch):
    """The defect, driven through the real CLI.

    `_validate_extracted` runs after the archive is on disk and before anything is moved into place,
    so patching it is the honest way to stand inside the window without reaching into the store."""
    from requivo.deterministic.sessions import archives as det

    _zip(tmp_path / "race.zip", _good_entries("race"))
    real = det._validate_extracted

    def _claim_the_slug_mid_import(d, slug):
        real(d, slug)
        # A concurrent creator wins the slug while this import is still in scratch space.
        if not store.session_exists(slug):
            _run(["session", "init", "The one that was already here.", "--slug", slug, "--json"])
    monkeypatch.setattr(det, "_validate_extracted", _claim_the_slug_mid_import)

    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "race.zip"), "--json"])

    # The whole point: the session that appeared is still there, unmodified.
    assert store.session_exists("race") is True
    assert "The one that was already here." in store.session_request("race")


def test_that_window_refusal_names_the_conflict_rather_than_a_move_failure(
        workspace, tmp_path, monkeypatch):
    """`session_exists` / 409, the same code the guard would have raised — not `import_move_failed`.

    The caller is entitled to the answer they would have got had the timing been different, and the
    remedy is the same one: pass `--force`. `import_move_failed` would send them looking at their
    filesystem for a fault that is not there."""
    from requivo.deterministic.sessions import archives as det

    _zip(tmp_path / "race.zip", _good_entries("race"))
    real = det._validate_extracted

    def _claim_the_slug_mid_import(d, slug):
        real(d, slug)
        if not store.session_exists(slug):
            _run(["session", "init", "Mine.", "--slug", slug, "--json"])
    monkeypatch.setattr(det, "_validate_extracted", _claim_the_slug_mid_import)

    envelope = _import_error(tmp_path / "race.zip")
    assert envelope["code"] == "session_exists", envelope
    assert envelope["details"]["slug"] == "race"
    assert "--force" in envelope["message"]


def test_the_ordinary_import_arms_still_work_so_the_guard_is_not_a_blanket_refusal(
        workspace, tmp_path, monkeypatch):
    """The positive control for both arms. A fix that refused every import, or that never replaced,
    would pass the two tests above and fail here."""
    _zip(tmp_path / "fresh.zip", _good_entries("fresh"))
    r = _run_json(["session", "import", str(tmp_path / "fresh.zip"), "--json"])
    assert r["replaced"] is False
    assert store.session_exists("fresh") is True

    r = _run_json(["session", "import", str(tmp_path / "fresh.zip"), "--force", "--json"])
    assert r["replaced"] is True
    assert store.session_exists("fresh") is True


# ── a stray directory at the slug answers the same on every platform (#114) ─────
#
# The free-slug arm claims the slug with `os.replace`, and that call is where the platforms part
# company. On POSIX an **empty** destination directory is replaced silently; on Windows `os.replace`
# is `MoveFileExW` with `MOVEFILE_REPLACE_EXISTING`, which Microsoft documents as unusable when
# either name is a directory, so *any* existing destination fails there — empty or not. One stray
# `mkdir` therefore imported on macOS and failed on Windows, and the Windows failure arrived as
# `import_move_failed`: *could not move the imported session into place*. Both halves are defects,
# and the second is the worse one, because it names a cause that is not the cause.


@pytest.mark.parametrize("label, populate", [
    # ASCII ids on purpose: a parametrize label becomes a node id, which is written to a console
    # whose codepage is not the source file's.
    ("empty - the case the two platforms disagree about", lambda d: None),
    ("holding a file", lambda d: (d / "junk.txt").write_text("x", encoding="utf-8")),
    ("holding only a stray .lock", lambda d: (d / ".lock").write_text("", encoding="utf-8")),
])
def test_a_stray_directory_at_the_slug_is_refused_by_name_on_every_platform(workspace, tmp_path,
                                                                           label, populate):
    """#114. All three rows are one answer now — `import_destination_occupied`, before the rename is
    attempted, so the verdict is the guard's rather than the platform's."""
    _zip(tmp_path / "stray.zip", _good_entries("stray"))
    target = store.canonical_dir("stray")
    target.mkdir(parents=True)
    populate(target)
    before = sorted(p.name for p in target.iterdir())

    err = _import_error(tmp_path / "stray.zip")
    assert err["code"] == "import_destination_occupied", f"{label}: {err}"
    assert err["details"]["slug"] == "stray"
    assert str(target) in err["details"]["path"]
    # the old message sent the reader at their filesystem looking for a fault that is not there
    assert "could not move" not in err["message"], label
    # …and it must not offer the one remedy that cannot work: `--force` replaces a *session*, and
    # the whole point of this arm is that there is no session here
    assert "does not apply here" in err["message"], label

    # nothing was imported, and the directory the caller put there is untouched — an import does not
    # delete a directory it cannot interpret
    assert store.list_session_slugs() == []
    assert sorted(p.name for p in target.iterdir()) == before

    # must fire: with the stray gone the same archive lands. Without this the assertion above would
    # pass just as well against an import broken some other way, or against a harness that never
    # built an archive at all.
    shutil.rmtree(target)
    assert _run_json(["session", "import", str(tmp_path / "stray.zip"), "--json"])["slug"] == "stray"


def test_a_stray_appearing_in_the_rename_window_is_named_rather_than_called_a_move_failure(
        workspace, tmp_path, monkeypatch):
    """The second half of #114, and why the guard is called from two places rather than one."""
    _zip(tmp_path / "late.zip", _good_entries("late"))
    target = store.canonical_dir("late")
    real_replace = Path.replace
    armed = [True]

    def _a_stray_lands_in_the_window(self, dest):
        if armed[0] and Path(dest) == target:
            target.mkdir(parents=True, exist_ok=True)
            (target / "junk.txt").write_text("x", encoding="utf-8")
        return real_replace(self, dest)

    monkeypatch.setattr(Path, "replace", _a_stray_lands_in_the_window)
    err = _import_error(tmp_path / "late.zip")
    assert err["code"] == "import_destination_occupied", err
    assert err["details"]["slug"] == "late"
    assert "could not move" not in err["message"], err
    assert store.list_session_slugs() == []

    # must fire: disarm the patch, clear the stray, and the same archive lands. The patch is turned
    # off with a flag rather than `monkeypatch.undo()`: `workspace` takes the same function-scoped
    # `monkeypatch` object, so undoing here would also revert `REQUIVO_WORKSPACE` and import the
    # session into whatever directory the suite happens to be running from.
    armed[0] = False
    shutil.rmtree(target)
    assert _run_json(["session", "import", str(tmp_path / "late.zip"), "--json"])["slug"] == "late"


def test_the_occupied_destination_is_a_conflict_with_the_store_and_not_a_malformed_session():
    """Where the new code sits in the vocabulary, asserted rather than described."""
    from requivo.core.errors import (
        ImportDestinationOccupiedError,
        InvalidSessionError,
        RequivoError,
        SessionExistsError,
    )

    assert ImportDestinationOccupiedError.code == "import_destination_occupied"
    assert issubclass(ImportDestinationOccupiedError, RequivoError)
    assert not issubclass(ImportDestinationOccupiedError, InvalidSessionError)
    assert not issubclass(ImportDestinationOccupiedError, SessionExistsError)


def test_import_into_a_fresh_workspace_writes_the_privacy_gitignore(workspace, tmp_path):
    """#211's second door: `create_session` is not the only call that brings `.requivo/` into
    existence -- `session import` can reach a workspace with none, and a guarantee written at
    creation alone would be absent for exactly the user who received a colleague's session (which
    holds *their* client's request text verbatim) before running one of their own. Invariant 14's
    sentence, one directory up: creation is not the only door."""
    marker = workspace / ".requivo" / ".gitignore"
    assert not marker.exists()

    _zip(tmp_path / "s.zip", _good_entries("s"))
    _run(["session", "import", str(tmp_path / "s.zip"), "--json"])

    assert store.list_session_slugs() == ["s"]
    assert marker.exists(), "session import created .requivo/ without the privacy marker"
