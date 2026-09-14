"""#260 — a session recording an artifact type this build does not know.

`docs/compatibility.md` lists "a new artifact type" among the changes that need no `format_version`
bump, and it documents two Requivo versions sharing one workspace as a supported configuration. The
integrity checker refused such a type outright, so the *first* generator a later Requivo ships would
make every session it touched read as broken on an older install: `session verify` non-zero, `doctor`
naming it, `session import` refusing a colleague's archive — while `read_meta` opens the very same
file without complaint. That last part is what makes it the worse of the two answers: the diagnostic
disagreeing with the loader about one file, which is invariant 8's own sentence and the correction
#14 already made for a *model* key one field along.

**One file for one promise, deliberately.** The rule is a single sentence — a type this build cannot
name is named, not refused — and it has to hold in four places at once: the core checker, `session
verify`, `doctor`, and the `session import` gate. Split across four modules by subject, each half
reads as an isolated assertion and nothing says that they are one guarantee; the surface tests in
particular would look like CLI plumbing rather than the compatibility promise they pin. The four
sibling modules keep their own subjects and this file keeps the promise.

Offline throughout: no API, no provider, no network.
"""
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
    """Record an artifact type this build has no generator for, exactly as a newer Requivo would have
    left it: an `artifact_status` entry plus the file it names.

    Written straight into `session.json` because there is no other way to produce one — the whole
    point is a type this build's vocabulary does not contain, so no service call can make it. The
    entry is copied off the real `prd` row, so every other field (revision, stale flag) is whatever a
    genuine save wrote."""
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

    # The positive control. Both assertions above are absences, and an absence also arrives from a
    # checker that stopped looking at artifacts altogether — so a *known* type recorded under the
    # wrong filename must still block. Renaming a populated field's meaning is the bump case, which
    # is why that row stays a problem while the one above became a note.
    p = store.canonical_dir("s") / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"]["prd"]["filename"] = "epic.md"
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert "artifact_filename_mismatch" in _codes()


def test_a_tolerated_artifact_type_is_held_to_every_other_check(workspace, monkeypatch):
    """Tolerating is not trusting (invariant 14). Not knowing the *type* relaxes nothing else about
    the row: the file it names still has to be there, and the revision it claims still has to exist.
    A guard that let an unknown type skip the rest of the loop would buy a crafted archive a way past
    checks that have nothing to do with the type at all."""
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
    """The filename half of the same row, which the note must not have relaxed. It is the sibling of
    `test_an_unknown_artifact_type_does_not_fall_through_to_the_filesystem` in `test_integrity.py`,
    and what it pins is that the *tolerance* did not open that path back up."""
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
    """The other half of *tolerating is not trusting*, and the reason it needs saying: before #260 an
    archive carrying arbitrary `artifact_status` keys was refused outright by `session import`, and
    tolerating the plausible ones widens that door. A key that is not shaped like an artifact type is
    not a newer Requivo's generator — it is junk or a forgery — so it stays a refusal, under a code
    of its own so a consumer can tell the two apart."""
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

    # must fire: the same command still refuses a session that really is inconsistent, so the exit-0
    # assertion above is not a verb that quietly stopped checking.
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
    # The code and the pointer, not the type name: this report has always spoken in codes and sent
    # the reader to `session verify`, which is where the type itself is spelled out.
    assert "unknown_artifact_type" in out
    assert "requivo session verify s" in out


def test_a_future_artifact_type_survives_an_export_import_round_trip(workspace, tmp_path,
                                                                     monkeypatch):
    """The acceptance criterion this issue is really about: a colleague on a newer Requivo hands you
    an archive, and it imports. `session import` holds an archive to exactly the standard a live
    session is held to, so this passes for the same reason `session verify` does — and it is refused
    for the same reasons too, which the control below keeps true."""
    _session("s", monkeypatch)
    _record_future_artifact()
    dest = tmp_path / "s.zip"
    _run(["session", "export", "s", "-o", str(dest), "--json"])

    _run(["session", "import", str(dest), "--force", "--json"])
    meta = json.loads((store.canonical_dir("s") / "session.json").read_text(encoding="utf-8"))
    assert meta["artifact_status"]["risk-register"]["filename"] == "risk-register.md"
    assert (store.canonical_dir("s") / "artifacts" / "risk-register.md").is_file()

    # must fire: an archive whose unknown type is *not* plausible is still refused, so the import
    # above is not an import that stopped validating.
    _session("t", monkeypatch)
    _record_future_artifact("t", atype="../escape")
    bad = tmp_path / "t.zip"
    _run(["session", "export", "t", "-o", str(bad), "--json"])
    with redirect_stdout(io.StringIO()), pytest.raises(SystemExit):
        app(["session", "import", str(bad), "--force", "--json"], client=None)
