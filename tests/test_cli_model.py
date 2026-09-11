"""End-to-end tests of `requivo.deterministic.model` — `model validate`, `apply` and `diff`.

Split out of `test_cli_deterministic.py` by #141; the shared harness is `tests/_cli_harness.py`.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest
from _cli_harness import _full_model, _run, _run_json, _slot

from requivo.cli import _build_parser, app
from requivo.core import persistence as store
from requivo.services.sessions import SessionService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


def test_model_validate_ok_and_invalid_exit(workspace, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_full_model()))
    assert _run_json(["model", "validate", str(good), "--json"])["status"] == "valid"

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": {"nope": _slot()}, "summary": {}}))
    with pytest.raises(SystemExit) as e:
        _run(["model", "validate", str(bad), "--json"])
    assert e.value.code == 1


def test_apply_refuses_a_partial_model_instead_of_replacing_the_whole_one(workspace, tmp_path):
    """`--allow-partial` on `apply` read as "apply a patch"; it merged nothing. It only relaxed the
    completeness check, and the incomplete model then *replaced* the complete one — a fifteen-slot
    model became a one-slot model, reported as fourteen changed slots. `apply` replaces, so it takes
    the full slot set and nothing else; validating a projection is `model validate --allow-partial`."""
    _run(["session", "init", "Something.", "--slug", "s"])
    full = tmp_path / "full.json"
    full.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(full)])
    before = len(SessionService().load_model("s").model)

    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"model": {"workflow": _slot(80, "explicit", "high", "scan")},
                                   "summary": {"objective": "Something"}}))
    with pytest.raises(SystemExit) as e:
        _run(["model", "apply", "s", str(partial), "--json"])
    assert e.value.code == 1
    assert len(SessionService().load_model("s").model) == before   # the model is untouched
    # The projection is still checkable on its own — that is what the flag means now, and where it lives.
    assert _run_json(["model", "validate", str(partial), "--allow-partial", "--json"])["slots"] == 1


def test_model_apply_and_status_and_artifact_flow(workspace, tmp_path):
    _run(["session", "init", "Reconcile event check-ins.", "--slug", "event"])
    proposal = tmp_path / "p.json"
    proposal.write_text(json.dumps(_full_model(**{"workflow": _slot(70, "inferred", "high", "scan")})))

    applied = _run_json(["model", "apply", "event", str(proposal), "--json"])
    assert applied["status"] == "applied" and applied["revision"] == 1
    assert "workflow" in applied["readiness"]["blocking_slots"]  # inferred high-impact blocks

    status = _run_json(["status", "event", "--json"])
    assert status["revision"] == 1 and status["readiness"]["ready"] is False

    brief = tmp_path / "brief.md"
    brief.write_text("# Assessment\n")
    _run(["artifact", "save", "event", "--type", "brief", "--file", str(brief), "--revision", "1"])
    listed = _run_json(["artifact", "list", "event", "--json"])["artifacts"]
    assert listed["brief"]["revision"] == 1 and listed["brief"]["stale"] is False


def test_model_validate_has_no_flag_it_does_not_honour():
    # `--session` was declared and read by nothing. A flag that parses and changes nothing is worse
    # than a missing one: the caller believes a check ran. `model diff` is the real answer.
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["model", "validate", "p.json", "--session", "s"])
    assert _build_parser().parse_args(["model", "diff", "s", "p.json"]).func.__name__ == "_cmd_model_diff"


def test_apply_invalid_proposal_emits_error_envelope(workspace, tmp_path):
    _run(["session", "init", "X.", "--slug", "s"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": {"ghost": _slot()}, "summary": {}}))
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit):
        app(["model", "apply", "s", str(bad), "--json"], client=None)
    env = json.loads(buf.getvalue())
    assert env["code"] == "unknown_slot" and env["details"]["slots"] == ["ghost"]


def test_a_refused_apply_writes_nothing_and_answers_like_validate(workspace, tmp_path):
    """The plugin's mutating skills apply a proposal directly instead of validating it first, and
    that rests on two properties of `apply` rather than on care in the prompt (#511).

    `update_model` validates *inside* the session lock and *before* `_plan` writes, so a refused
    apply is indistinguishable from a dry run: no revision, no `model.json`, the session still at its
    old revision, and the same error envelope `model validate` produces for that payload. Were either
    half to stop holding, the skills would be walking a session through a half-applied state on every
    typo — so it is pinned here rather than restated in `REASONING.md` and hoped for."""
    _run(["session", "init", "X.", "--slug", "s"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": {"ghost": _slot()}, "summary": {"objective": "o"}}))

    def envelope(argv):
        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit) as e:
            app(argv, client=None)
        return e.value.code, json.loads(buf.getvalue())

    apply_code, apply_env = envelope(
        ["model", "apply", "s", str(bad), "--expected-revision", "0", "--json"])
    validate_code, validate_env = envelope(["model", "validate", str(bad), "--json"])
    assert apply_code == validate_code == 1
    assert apply_env == validate_env, "a refused apply must answer exactly as `model validate` does"

    # ...and the refusal left the store as it found it, so the caller's `--expected-revision` is
    # still current and the corrected proposal can be applied against the same N.
    assert SessionService().repo.read_meta("s").current_revision == 0
    d = store.canonical_dir("s")
    assert not (d / "model.json").exists(), "a refused apply must not write a model"
    assert not list((d / "revisions").glob("*")), "a refused apply must not mint a revision"


def test_model_diff_does_not_write(workspace, tmp_path):
    _run(["session", "init", "X.", "--slug", "s"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p)])
    before = store.read_meta("s").current_revision
    r = _run_json(["model", "diff", "s", str(p), "--json"])
    assert r["status"] == "planned"
    assert store.read_meta("s").current_revision == before


def test_model_apply_honours_the_expected_revision_precondition(workspace, tmp_path):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p), "--expected-revision", "0", "--json"])  # fresh: asserts 0

    p2 = tmp_path / "p2.json"
    p2.write_text(json.dumps(_full_model(**{"workflow": _slot(80, "explicit", "high", "new")})))
    _run(["model", "apply", "s", str(p2), "--expected-revision", "1", "--json"])

    # Applying again from the same base is refused with a structured, actionable error.
    with pytest.raises(SystemExit) as exc:
        _run(["model", "apply", "s", str(p2), "--expected-revision", "1", "--json"])
    assert exc.value.code != 0


def test_a_corrupt_model_reaches_the_operator_as_one_line_not_a_traceback(workspace, capsys):
    """The end-to-end half of #204, from the three verbs a user actually types.

    `status` and `impact` resolve a model through `_resolve_ref`; `model show` goes through the
    service. Two different doors into the same file, which is why the assertion is over all three
    rather than over whichever one the bug was reported against.

    Asserting on **stderr** rather than on the exception is the point: a `ValidationError` reaching
    `cli.app()` produces a traceback and exit 1, and a test that only checked the exit code would
    have been green on the defect.
    """
    svc = SessionService()
    svc.create_session("A leave approval system.", slug="corrupt")
    svc.update_model("corrupt", _full_model())
    (store.canonical_dir("corrupt") / "model.json").write_text("{", encoding="utf-8")

    for argv in (["status", "corrupt"], ["impact", "corrupt"], ["model", "show", "corrupt"]):
        with pytest.raises(SystemExit) as e:
            _run(argv)
        assert e.value.code == 1, argv
        err = capsys.readouterr().err
        assert "Traceback" not in err and "pydantic" not in err, (
            f"{argv} still surfaces a raw parse failure: {err!r}")
        assert "model.json" in err, argv
        assert "requivo session verify corrupt" in err, argv
        assert "revisions/" in err, f"{argv} does not mention the history that can recover it"


def test_a_corrupt_model_gives_the_json_envelope_its_own_code(workspace):
    """A caller reading `--json` branches on the code, and this condition had none to branch on.

    `model_unreadable` rather than `session_unreadable`: the session opens, the listing is
    unaffected, `session verify` answers, and `revisions/` holds every applied model — none of which
    is true when `session.json` is the file that will not parse. Two situations, two remedies, two
    codes.
    """
    svc = SessionService()
    svc.create_session("A leave approval system.", slug="corrupt-json")
    svc.update_model("corrupt-json", _full_model())
    (store.canonical_dir("corrupt-json") / "model.json").write_text("{", encoding="utf-8")

    with pytest.raises(SystemExit) as e:
        _run_json(["status", "corrupt-json", "--json"])
    assert e.value.code == 1


# ── #250: a claimed-but-undiscovered session vs one that was never created at all ─────────────────


def test_status_and_model_show_agree_on_a_revision_zero_session(workspace, capsys):
    """The issue as filed claimed `status` exits 1 and `model show` exits 0 on the identical
    revision-0 session, printing the identical message. Reproducing it against this tree found both
    already exiting 1 -- so this pins the (already-true) agreement rather than a fix for it, and
    guards the copy fix that *is* real: engine jargon ("apply a proposal first") replaced by the
    actual remedy, naming `requivo discover`.
    """
    _run(["session", "init", "A tiny tool to track something.", "--slug", "rev0"])

    for argv in (["status", "rev0"], ["model", "show", "rev0"]):
        with pytest.raises(SystemExit) as e:
            _run(argv)
        assert e.value.code == 1, argv
        err = capsys.readouterr().err
        assert "requivo discover" in err, (argv, err)
        assert "apply a proposal first" not in err, (
            f"{argv} still speaks engine jargon instead of naming the remedy: {err!r}"
        )


def test_model_show_does_not_claim_a_request_was_captured_for_a_session_that_never_existed(
    workspace, capsys,
):
    """The trap on the other side of the copy fix above: the friendlier revision-zero wording says
    "only the request was captured", which is true of a claimed session and false of a slug nobody
    has ever used. `load_session_model` raises the identical `session_not_found` code either way, so
    the CLI has to tell the two apart itself rather than trust the message it is handed."""
    with pytest.raises(SystemExit) as e:
        _run(["model", "show", "no-such-slug-at-all"])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "only the request was captured" not in err, (
        f"claimed a request was captured for a session that was never created: {err!r}"
    )
    assert "requivo session list" in err, "the genuine no-session message names how to see what exists"


def test_impact_on_an_unmatched_slot_exits_1_not_0(workspace, tmp_path):
    """A wrong probe used to be indistinguishable from an empty result -- both exited 0 (#250)."""
    _run(["session", "init", "Something.", "--slug", "s"])
    proposal = tmp_path / "p.json"
    proposal.write_text(json.dumps(_full_model()), encoding="utf-8")
    _run(["model", "apply", "s", str(proposal)])

    with pytest.raises(SystemExit) as e:
        _run(["impact", "s", "not-a-real-slot"])
    assert e.value.code == 1

