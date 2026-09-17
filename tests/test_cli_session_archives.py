"""`session import`/`export`: archive-shape validation (#141, #101), the concurrency and mid-operation races
(#113, #111, #114) and the retried rename (#483)."""
from __future__ import annotations

import io
import json
import shutil
import threading
import time
import zipfile
from contextlib import redirect_stderr
from pathlib import Path

import pytest
from _cli_harness import _SESSIONS_ROW
from _fakes import full_model, out, run_cli, run_cli_exit, run_cli_json, run_cli_stdin

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.errors import (
    ImportDestinationOccupiedError,
    InconsistentArchiveError,
    InvalidArchiveError,
    InvalidModelError,
    InvalidSessionError,
    InvalidSlugError,
    RequivoError,
    SessionExistsError,
    UnreadableArchiveError,
)
from requivo.core.persistence import _REPLACE_ATTEMPTS
from requivo.deterministic.sessions import archives as det

pytestmark = pytest.mark.usefixtures("workspace")


def _zip(path, entries: dict) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return path


def _meta(slug: str, revision: int = 0, **fields) -> str:
    return json.dumps({"format_version": 1, "session_id": "abc", "slug": slug, "created_at": "t", "updated_at": "t",
                       "current_revision": revision, **fields})


def _good_entries(slug="imported", revision=0):
    entries = {f"{slug}/session.json": _meta(slug, revision), f"{slug}/request.md": "A request."}
    if revision:
        entries[f"{slug}/model.json"] = json.dumps(full_model())
    return entries


def _dir_entries(slug: str, n: int) -> dict:
    """`n` bare directory entries under `<slug>/nested/`."""
    return {f"{slug}/nested/d{i:05d}/": "" for i in range(n)}


def _import(archive, *flags: str) -> dict:
    return run_cli_json(["session", "import", str(archive), *flags, "--json"])


def _import_error(archive, *flags: str) -> dict:
    """The structured envelope a `--json` consumer sees, once the CLI has refused."""
    stdout, code = run_cli_exit(["session", "import", str(archive), *flags, "--json"])
    assert code == 1
    return json.loads(stdout)


def _seeded(slug: str, request: str, monkeypatch) -> None:
    run_cli(["session", "init", request, "--slug", slug, "--json"])
    run_cli_stdin(["model", "apply", slug, "-", "--json"], json.dumps(full_model()), monkeypatch)


def _replace_fails(monkeypatch, denials: int | None) -> dict:
    """`Path.replace` raises `PermissionError` on the first `denials` calls (every call when None); returns the counter."""
    attempts = {"n": 0}
    real_replace = Path.replace

    def flaky(self, dst):
        attempts["n"] += 1
        if denials is None or attempts["n"] <= denials:
            raise PermissionError(13, "Access is denied")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky)
    return attempts


# ── the round trip and the envelope ─────────────────────────────────────────────


def test_export_import_round_trip(tmp_path, monkeypatch):
    _seeded("s", "Something.", monkeypatch)
    run_cli(["session", "export", "s", "-o", str(tmp_path / "s.zip"), "--json"])
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path / "elsewhere"))
    r = _import(tmp_path / "s.zip")
    assert r["slug"] == "s" and r["replaced"] is False
    assert store.read_meta("s").current_revision == 1


def test_import_json_names_the_session_and_its_directory_the_way_its_siblings_do(tmp_path, monkeypatch):
    """#84: `slug` and `path` mean what they mean on `session init`, never `imported`/`into`."""
    init = run_cli_json(["session", "init", "Something.", "--slug", "s", "--json"])
    run_cli(["session", "export", "s", "-o", str(tmp_path / "s.zip"), "--json"])
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path / "elsewhere"))
    r = _import(tmp_path / "s.zip")
    assert r.keys() == {"slug", "path", "replaced"} and r["slug"] == "s"
    assert r["path"] == str(store.canonical_dir("s")) and Path(r["path"]).name == "s"
    assert set(init) >= {"slug", "path"} and Path(init["path"]).name == "s"


# ── the archive-shape refusals name the archive, not the model (#101) ───────────


@pytest.mark.parametrize("entry", [
    "../escape/session.json", "/absolute/session.json", "..\\windows\\session.json", "loose.json",
], ids=["parent-segment", "absolute", "windows-separator", "loose-at-root"])
def test_import_refuses_unsafe_entries(tmp_path, entry):
    """Traversal, an absolute path, a separator zipfile does not treat as a boundary, an entry outside any session."""
    err = _import_error(_zip(tmp_path / "evil.zip", {entry: "{}"}))
    assert err["code"] == "invalid_archive"
    assert err["details"]["problem"] == ("entry_outside_session_directory" if entry == "loose.json" else "unsafe_entry")
    assert store.list_session_slugs() == []


@pytest.mark.parametrize("build, problem", [
    (lambda mp: {f"s/artifacts/f{i}.md": "x" for i in range(det.MAX_ARCHIVE_FILES + 1)}, "too_many_files"),
    (lambda mp: {"s/session.json": "0" * (64 * 1024 * 1024 + 1)}, "too_large"),
    (lambda mp: (mp.setattr(det, "MAX_ARCHIVE_BYTES", 32), {"s/session.json": "0" * 64})[1], "too_large"),
    (lambda mp: (mp.setattr(det, "MAX_ARCHIVE_ENTRIES", 50), {**_good_entries("s"), **_dir_entries("s", 500)})[1],
     "too_many_entries"),
    (lambda mp: (mp.setattr(det, "MAX_ARCHIVE_ENTRIES", 50), {**_good_entries("s"), **_dir_entries("s", 30)})[1], None),
], ids=["files-ceiling", "bytes-ceiling-zip-bomb", "bytes-ceiling-lowered", "entries-ceiling-#219", "under-the-entries-cap"])
def test_import_refuses_an_archive_bounded_by_files_and_bytes_but_not_by_directory_entries(tmp_path, monkeypatch, build, problem):
    """The three ceilings, the expanded size being the one a zip bomb tests, and the positive control under the cap (#219)."""
    archive = _zip(tmp_path / "case.zip", build(monkeypatch))
    if problem is None:
        assert _import(archive)["slug"] == "s" and store.list_session_slugs() == ["s"]
        return
    err = _import_error(archive)
    assert err["code"] == "invalid_archive" and err["details"]["problem"] == problem
    assert store.list_session_slugs() == [] and not (store.session_root() / "s").exists()


@pytest.mark.parametrize("entries, code, problem", [
    ({}, "invalid_archive", "empty"),
    ({"s/notes.md": "hello"}, "inconsistent_archive", None),
    ({"s/session.json": "{not json"}, "inconsistent_archive", None),
    ({"claimed/session.json": _meta("something-else")}, "inconsistent_archive", None),
    ({"s/session.json": _meta("s", 3)}, "inconsistent_archive", None),
    ({**_good_entries("s", revision=1), "s/session.json": _meta("s", 2)}, "inconsistent_archive", None),
    ({**_good_entries("one"), **_good_entries("two")}, "invalid_archive", "multiple_sessions"),
    (_good_entries("bad slug"), "invalid_slug", None),
    (None, "unreadable_archive", None),
], ids=["empty", "no-metadata", "unparseable-metadata", "slug-mismatch", "model-claimed-not-carried",
        "history-missing", "two-sessions", "invalid-slug", "not-a-zip"])
def test_import_refuses_an_archive_that_is_not_a_session(tmp_path, entries, code, problem):
    """Extraction succeeding is not having imported a session; nothing is written and no scratch is left (#101)."""
    archive = tmp_path / "case.zip"
    if entries is None:
        archive.write_text("this is not a zip", encoding="utf-8")
    else:
        _zip(archive, entries)
    err = _import_error(archive)
    assert err["code"] == code
    if problem is not None:
        assert err["details"]["problem"] == problem
    assert store.list_session_slugs() == [] and run_cli_json(["session", "list", "--json"])["sessions"] == []
    assert list(store.workspace_root().joinpath(".requivo").glob(".import-*")) == []


def test_import_refuses_a_collision_unless_forced(tmp_path, monkeypatch):
    """An occupied slug is `session_exists`, a conflict with the store (#101); `--force` genuinely replaces."""
    _seeded("dup", "The original.", monkeypatch)
    archive = _zip(tmp_path / "dup.zip", _good_entries("dup"))
    err = _import_error(archive)
    assert err["code"] == "session_exists" and err["details"] == {"slug": "dup"}
    assert store.read_meta("dup").current_revision == 1 and "The original." in store.session_request("dup")
    assert _import(archive, "--force")["replaced"] is True
    assert store.read_meta("dup").current_revision == 0 and "A request." in store.session_request("dup")


def test_every_refusal_on_the_import_path_names_what_it_is_about(tmp_path, monkeypatch):
    """The table in `docs/cli.md` under *Importing a session*, and where each code sits in the vocabulary (#101, #114)."""
    assert _import_error(tmp_path / "nowhere.zip")["code"] == "session_not_found"
    run_cli(["session", "init", "The original.", "--slug", "taken", "--json"])
    assert _import_error(_zip(tmp_path / "taken.zip", _good_entries("taken")))["code"] == "session_exists"
    # passed every check and could not be moved into place: `Path.replace` refuses the one target
    archive = _zip(tmp_path / "movefail.zip", _good_entries("move-fails"))
    doomed, real_replace = store.canonical_dir("move-fails"), Path.replace

    def _refuse_only_the_target(self, dest):
        if Path(dest) == doomed:
            raise OSError(39, "Directory not empty")
        return real_replace(self, dest)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "replace", _refuse_only_the_target)
        assert _import_error(archive)["code"] == "import_move_failed"
    assert _import(archive)["slug"] == "move-fails", "the patch really was the cause"
    store.canonical_dir("held").mkdir(parents=True)
    assert _import_error(_zip(tmp_path / "held.zip", _good_entries("held")))["code"] == "import_destination_occupied"

    for cls in (UnreadableArchiveError, InvalidArchiveError, InconsistentArchiveError):
        assert issubclass(cls, InvalidSessionError), cls.__name__
    assert InvalidArchiveError.code == "invalid_archive" and not issubclass(InvalidArchiveError, InvalidModelError)
    assert not issubclass(InvalidSlugError, InvalidSessionError)
    assert not issubclass(SessionExistsError, InvalidSessionError)
    assert ImportDestinationOccupiedError.code == "import_destination_occupied"
    assert issubclass(ImportDestinationOccupiedError, RequivoError)
    assert not issubclass(ImportDestinationOccupiedError, (InvalidSessionError, SessionExistsError))
    assert _import(_zip(tmp_path / "good.zip", _good_entries("ok-one")))["slug"] == "ok-one"


_FORGED_SLUG = "ok-session\nAll clear, nothing to see.\n  ✅ sessions        0 in this workspace"


def test_an_archive_directory_name_cannot_write_a_line_of_the_refusal_reporting_it(tmp_path):
    """A directory name inside an archive is caller text not yet validated (#40), found by the audit of #101."""
    archive = _zip(tmp_path / "forged.zip", {f"{_FORGED_SLUG}/session.json": "{}", "other/session.json": "{}"})
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit):
        app(["session", "import", str(archive)], client=None)
    rendered = err.getvalue()
    assert "session directories" in rendered and "other" in rendered and "ok-session" in rendered
    assert "\nAll clear, nothing to see." not in rendered
    assert not any(_SESSIONS_ROW.match(line) for line in rendered.splitlines()), rendered
    envelope = _import_error(archive)
    assert envelope["code"] == "invalid_archive" and _FORGED_SLUG in envelope["details"]["slugs"]


def test_a_failed_forced_replacement_puts_the_original_back(tmp_path, monkeypatch):
    """`--force` used to `rmtree` the existing session and *then* move the new one in."""
    _seeded("dup", "The original.", monkeypatch)
    archive = _zip(tmp_path / "dup.zip", _good_entries("dup"))
    real_replace = Path.replace

    def failing_replace(self, target):
        if ".import-" in str(self):
            raise OSError("simulated failure moving the imported session into place")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    assert _import_error(archive, "--force")["code"]
    assert store.session_exists("dup") and store.read_meta("dup").current_revision == 1
    assert "The original." in store.session_request("dup")
    assert run_cli_json(["session", "verify", "dup", "--json"])["ok"] is True


# ── #113: a forced replacement is serialised against the writers of the session it replaces ─────


def _capture(failures: list):
    def wrap(fn):
        def run():
            try:
                fn()
            except BaseException as e:  # noqa: BLE001 - reported, not swallowed
                failures.append(e)
        return run
    return wrap


def test_a_forced_import_serialises_against_a_concurrent_writer(tmp_path, monkeypatch):
    """Invariant 9: the lock lives outside the directory being renamed, so a paused writer still holds it (#113)."""
    _seeded("dup", "The original.", monkeypatch)
    resident_id = store.read_meta("dup").session_id
    archive = _zip(tmp_path / "dup.zip", _good_entries("dup"))
    assert resident_id != "abc", "the two sessions must be distinguishable by identity"
    at_the_gate, release, imported, probe_acquired = (threading.Event() for _ in range(4))
    real_write_meta = store.Store.write_meta

    def paused(self, s, meta):
        # Freeze `save_revision` between its file writes and its metadata write (#272).
        if s == "dup" and not at_the_gate.is_set():
            at_the_gate.set()
            assert release.wait(20), "the test never released the paused writer"
        return real_write_meta(self, s, meta)

    monkeypatch.setattr(store.Store, "write_meta", paused)
    failures: list[BaseException] = []
    capture = _capture(failures)

    def _import_then_signal():
        _import(archive, "--force")
        imported.set()

    def _take_the_lock():
        with store.session_lock("dup"):
            probe_acquired.set()

    writer = threading.Thread(target=capture(lambda: store.save_revision("dup", out({}))), daemon=True)
    writer.start()
    assert at_the_gate.wait(10), "the writer never reached the gap between its writes"
    importer = threading.Thread(target=capture(_import_then_signal), daemon=True)
    importer.start()
    assert not imported.wait(1.5), "the forced import replaced the session while a writer held its lock"
    probe = threading.Thread(target=capture(_take_the_lock), daemon=True)
    probe.start()
    assert not probe_acquired.wait(0.5), "a third caller acquired the lock while a writer held it"
    release.set()
    for t in (writer, importer, probe):
        t.join(timeout=20)
    assert not [t for t in (writer, importer, probe) if t.is_alive()] and failures == []
    assert imported.is_set() and probe_acquired.is_set(), "the positive controls: released, both happen"
    meta = store.read_meta("dup")
    assert meta.session_id == "abc" and meta.current_revision == 0 and meta.revisions == []
    assert store.list_session_slugs() == ["dup"]
    assert run_cli_json(["session", "verify", "dup", "--json"])["ok"] is True


def test_export_excludes_the_lock_file_and_waits_for_the_writer(tmp_path, monkeypatch):
    """An export reads several files that must agree, so it takes the lock; legacy `.lock` residue is not shipped (#293)."""
    _seeded("s", "Something.", monkeypatch)
    assert store.lock_path("s").exists() and not (store.canonical_dir("s") / ".lock").exists()
    (store.canonical_dir("s") / ".lock").touch()
    acquired, held = threading.Event(), threading.Event()

    def hold_the_lock():
        with store.session_lock("s"):
            acquired.set()
            time.sleep(0.4)
            held.set()

    t = threading.Thread(target=hold_the_lock)
    t.start()
    assert acquired.wait(10), "the writer never took the lock"
    dest = tmp_path / "s.zip"
    run_cli(["session", "export", "s", "-o", str(dest), "--json"])
    assert held.is_set(), "the export read the session while a writer held it"
    t.join(timeout=10)
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
    assert not [n for n in names if ".lock" in n]
    assert "s/model.json" in names and "s/revisions/0001-model.json" in names


def test_session_export_survives_a_transient_permission_error(tmp_path, monkeypatch):
    """The same cause invariant 18's `_atomic_write` and `session restore` retry (#483)."""
    _seeded("s", "Something.", monkeypatch)
    attempts = _replace_fails(monkeypatch, 2)
    dest = tmp_path / "s.zip"
    run_cli(["session", "export", "s", "-o", str(dest), "--json"])
    assert attempts["n"] == 3 and dest.exists()
    with zipfile.ZipFile(dest) as z:
        assert {"s/model.json", "s/revisions/0001-model.json"} <= set(z.namelist())


def test_session_export_still_gives_up_on_a_permanent_permission_error(tmp_path, monkeypatch):
    """Bounded by `_REPLACE_ATTEMPTS`, and no completed archive is left under a scratch name (#483)."""
    _seeded("s", "Something.", monkeypatch)
    attempts = _replace_fails(monkeypatch, None)
    dest = tmp_path / "s.zip"
    with pytest.raises(PermissionError):
        app(["session", "export", "s", "-o", str(dest)], client=None)
    assert attempts["n"] == _REPLACE_ATTEMPTS
    assert not dest.exists() and not list(tmp_path.glob(".*.part"))


# ── #111: a session created while the archive was being read is not destroyed ───


def _claim_the_slug_mid_import(monkeypatch, request: str) -> None:
    """A concurrent creator wins the slug while the import is still in scratch space."""
    real = det._validate_extracted

    def _claim(d, slug):
        real(d, slug)
        if not store.session_exists(slug):
            run_cli(["session", "init", request, "--slug", slug, "--json"])

    monkeypatch.setattr(det, "_validate_extracted", _claim)


def test_a_session_created_during_the_extraction_window_is_refused_not_destroyed(tmp_path, monkeypatch):
    """`session import` used to decide the collision question twice; invariant 9 in the one verb that writes a whole session (#111)."""
    archive = _zip(tmp_path / "race.zip", _good_entries("race"))
    _claim_the_slug_mid_import(monkeypatch, "The one that was already here.")
    assert run_cli_exit(["session", "import", str(archive), "--json"])[1] == 1
    assert store.session_exists("race") and "The one that was already here." in store.session_request("race")


def test_that_window_refusal_names_the_conflict_rather_than_a_move_failure(tmp_path, monkeypatch):
    """`session_exists`, the code the guard would have raised -- not `import_move_failed` (#111)."""
    archive = _zip(tmp_path / "race.zip", _good_entries("race"))
    _claim_the_slug_mid_import(monkeypatch, "Mine.")
    envelope = _import_error(archive)
    assert envelope["code"] == "session_exists" and envelope["details"]["slug"] == "race"
    assert "--force" in envelope["message"]


# ── a stray directory at the slug answers the same on every platform (#114) ─────


@pytest.mark.parametrize("label, populate", [
    ("empty - the case the two platforms disagree about", lambda d: None),
    ("holding a file", lambda d: (d / "junk.txt").write_text("x", encoding="utf-8")),
    ("holding only a stray .lock", lambda d: (d / ".lock").write_text("", encoding="utf-8")),
])
def test_a_stray_directory_at_the_slug_is_refused_by_name_on_every_platform(tmp_path, label, populate):
    """All three rows are one answer, `import_destination_occupied`, and the stray is untouched (#114)."""
    archive = _zip(tmp_path / "stray.zip", _good_entries("stray"))
    target = store.canonical_dir("stray")
    target.mkdir(parents=True)
    populate(target)
    before = sorted(p.name for p in target.iterdir())
    err = _import_error(archive)
    assert err["code"] == "import_destination_occupied" and err["details"]["slug"] == "stray", label
    assert str(target) in err["details"]["path"]
    assert "could not move" not in err["message"] and "does not apply here" in err["message"], label
    assert store.list_session_slugs() == [] and sorted(p.name for p in target.iterdir()) == before
    shutil.rmtree(target)
    assert _import(archive)["slug"] == "stray", "with the stray gone the same archive lands"


def test_a_stray_appearing_in_the_rename_window_is_named_rather_than_called_a_move_failure(tmp_path, monkeypatch):
    """The second half of #114, and why the guard is called from two places rather than one."""
    archive = _zip(tmp_path / "late.zip", _good_entries("late"))
    target, real_replace, armed = store.canonical_dir("late"), Path.replace, [True]

    def _a_stray_lands_in_the_window(self, dest):
        if armed[0] and Path(dest) == target:
            target.mkdir(parents=True, exist_ok=True)
            (target / "junk.txt").write_text("x", encoding="utf-8")
        return real_replace(self, dest)

    monkeypatch.setattr(Path, "replace", _a_stray_lands_in_the_window)
    err = _import_error(archive)
    assert err["code"] == "import_destination_occupied" and err["details"]["slug"] == "late"
    assert "could not move" not in err["message"] and store.list_session_slugs() == []
    armed[0] = False
    shutil.rmtree(target)
    assert _import(archive)["slug"] == "late"


def test_import_into_a_fresh_workspace_writes_the_privacy_gitignore(workspace, tmp_path):
    """#211's second door: `create_session` is not the only call that brings `.requivo/` into existence."""
    marker = workspace / ".requivo" / ".gitignore"
    assert not marker.exists()
    _import(_zip(tmp_path / "s.zip", _good_entries("s")))
    assert store.list_session_slugs() == ["s"] and marker.exists()
