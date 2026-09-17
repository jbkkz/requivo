"""#260 — a session recording an artifact type this build does not know."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest
from _cli_harness import _full_model, _run, _run_json, _run_stdin

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.integrity import SEVERITY_NOTE, check_session, inspect_session


def _session(slug: str, monkeypatch) -> None:
    """A healthy session at revision 1 with one real artifact on disk."""
    _run(["session", "init", "Something.", "--slug", slug, "--json"])
    _run_stdin(["model", "apply", slug, "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _run_stdin(["artifact", "save", slug, "--type", "prd", "--file", "-", "--revision", "1",
                "--json"], "# PRD", monkeypatch)


def _record_future_artifact(slug: str = "s", atype: str = "risk-register",
                            filename: str = "risk-register.md", *, write_file: bool = True) -> None:
    """Record an artifact type this build has no generator for, exactly as a newer Requivo would have left it:
    an `artifact_status` entry plus the file it names."""
    d = store.canonical_dir(slug)
    p = d / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"][atype] = dict(raw["artifact_status"]["prd"], filename=filename)
    p.write_text(json.dumps(raw), encoding="utf-8")
    if write_file:
        (d / "artifacts" / filename).write_text("# Risk register\n", encoding="utf-8")


def _codes(slug: str = "s") -> set:
    return set(f.code for f in check_session(slug))


# ── the core checker ──────────────────────────────────────────────────────────


def test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect(workspace, monkeypatch):
    """The promise itself. A plausible unknown type blocks nothing, and is still *named*."""
    _session("s", monkeypatch)
    assert check_session("s") == []                        # must fire: the fixture is healthy first
    _record_future_artifact()

    assert check_session("s") == []                        # nothing blocks
    notes = [f for f in inspect_session("s") if f.severity == SEVERITY_NOTE]
    assert [f.code for f in notes] == ["unknown_artifact_type"]
    assert "risk-register" in notes[0].message             # named, never silently dropped

    # The positive control.
    p = store.canonical_dir("s") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"]["prd"]["filename"] = "epic.md"
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert "artifact_filename_mismatch" in _codes()


def test_a_tolerated_artifact_type_is_held_to_every_other_check(workspace, monkeypatch):
    """Tolerating is not trusting (invariant 14)."""
    _session("s", monkeypatch)
    _record_future_artifact(write_file=False)
    assert "missing_artifact_file" in _codes()

    _session("t", monkeypatch)
    _record_future_artifact("t")
    p = store.canonical_dir("t") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"]["risk-register"]["revision"] = 9
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert "artifact_revision_out_of_range" in _codes("t")


def test_an_unsafe_artifact_filename_on_an_unknown_type_is_still_refused(workspace, tmp_path,
                                                                        monkeypatch):
    """The filename half of the same row, which the note must not have relaxed."""
    outside = tmp_path / "outside.md"
    outside.write_text("x\n", encoding="utf-8")
    _session("s", monkeypatch)
    _record_future_artifact(filename=str(outside), write_file=False)

    codes = _codes()
    assert "unsafe_artifact_filename" in codes             # a problem, so it still blocks
    assert "missing_artifact_file" not in codes            # and the outside path was never stat-ed


@pytest.mark.parametrize("atype", [
    "Risk-Register",                     # not lowercase, so not a token this vocabulary can hold
    "risk register",                     # a space
    "../escape",                         # a path, dressed as a type
    "risk\nregister",                    # a line break, which would forge a row of doctor's output
    "x" * 200,                           # unbounded length, on what is now a *passing* code path
])
def test_an_artifact_type_that_is_not_a_plausible_token_is_still_a_problem(workspace, monkeypatch,
                                                                          atype):
    """The other half of *tolerating is not trusting*, and the reason it needs saying (#260)."""
    _session("s", monkeypatch)
    _record_future_artifact(atype=atype)
    codes = _codes()
    assert "unsafe_artifact_type" in codes
    assert "unknown_artifact_type" not in codes


# ── the surfaces ──────────────────────────────────────────────────────────────


def test_session_verify_passes_and_still_names_the_unknown_type(workspace, monkeypatch):
    _session("s", monkeypatch)
    _record_future_artifact()

    report = _run_json(["session", "verify", "s", "--json"])
    assert report["ok"] is True
    assert report["problems"] == []
    assert [n["code"] for n in report["notes"]] == ["unknown_artifact_type"]

    out = _run(["session", "verify", "s"])
    assert "risk-register" in out                          # named on the human surface too

    # must fire: the same command still refuses a session that really is inconsistent.
    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()
    with redirect_stdout(io.StringIO()), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 1


def test_doctor_names_the_unknown_type_without_calling_the_session_inconsistent(workspace,
                                                                               monkeypatch):
    _session("s", monkeypatch)
    _record_future_artifact()

    r = _run_json(["doctor", "--json"])["sessions"]
    assert r["inconsistent"] == dict()
    assert r["notes"] == dict(s=["unknown_artifact_type"])

    out = _run(["doctor"])
    # The code and the pointer, not the type name: this report has always spoken in codes and sent the reader to `session verify`, which is where the type itself is spelled out.
    assert "unknown_artifact_type" in out
    assert "requivo session verify s" in out


def test_a_future_artifact_type_survives_an_export_import_round_trip(workspace, tmp_path,
                                                                     monkeypatch):
    """The acceptance criterion this issue is really about."""
    _session("s", monkeypatch)
    _record_future_artifact()
    dest = tmp_path / "s.zip"
    _run(["session", "export", "s", "-o", str(dest), "--json"])

    _run(["session", "import", str(dest), "--force", "--json"])
    meta = json.loads((store.canonical_dir("s") / "session.json").read_text(encoding="utf-8"))
    assert meta["artifact_status"]["risk-register"]["filename"] == "risk-register.md"
    assert (store.canonical_dir("s") / "artifacts" / "risk-register.md").is_file()

    # must fire: an archive whose unknown type is *not* plausible is still refused.
    _session("t", monkeypatch)
    _record_future_artifact("t", atype="../escape")
    bad = tmp_path / "t.zip"
    _run(["session", "export", "t", "-o", str(bad), "--json"])
    with redirect_stdout(io.StringIO()), pytest.raises(SystemExit):
        app(["session", "import", str(bad), "--force", "--json"], client=None)
