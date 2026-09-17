"""End-to-end tests of `requivo.deterministic.model` — `model validate`, `apply` and `diff` (#141)."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest
from _cli_harness import _full_model, _run, _run_json, _slot

from requivo.cli import _build_parser, app
from requivo.core import persistence as store
from requivo.services.sessions import SessionService


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
    """`--allow-partial` on `apply` read as "apply a patch"; it merged nothing."""
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
    # `--session` was declared and read by nothing.
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
    """The plugin's mutating skills apply a proposal directly instead of validating it first (#511)."""
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

    # ...and the refusal left the store as it found it, so the caller's `--expected-revision` is still current and the corrected proposal can be applied against the same N.
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
    """The end-to-end half of #204, from the three verbs a user actually types."""
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
    """A caller reading `--json` branches on the code, and this condition had none to branch on."""
    svc = SessionService()
    svc.create_session("A leave approval system.", slug="corrupt-json")
    svc.update_model("corrupt-json", _full_model())
    (store.canonical_dir("corrupt-json") / "model.json").write_text("{", encoding="utf-8")

    with pytest.raises(SystemExit) as e:
        _run_json(["status", "corrupt-json", "--json"])
    assert e.value.code == 1


# ── #250: a claimed-but-undiscovered session vs one that was never created at all ─────────────────


def test_status_and_model_show_agree_on_a_revision_zero_session(workspace, capsys):
    """The issue as filed claimed `status` exits 1 and `model show` exits 0 on the identical revision-0
    session, printing the identical message."""
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
    """The trap on the other side of the copy fix above."""
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


def test_an_exclusion_only_invalidation_is_still_announced_on_the_apply_path(workspace, tmp_path):
    """#599: the service reported an unseated exclusion and this printer did not."""
    _run(["session", "init", "Something.", "--slug", "exc"])
    first = tmp_path / "first.json"
    first.write_text(json.dumps(_full_model(
        **{"workflow": _slot(80, "explicit", "high", "manual scan")},
        )), encoding="utf-8")
    # The exclusion rests on `workflow` and nothing else does: no decision, no challenge.
    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["exclusions"] = [{"option": "Bulk import", "reason": "Out of scope for v1",
                              "rests_on": ["workflow"]}]
    first.write_text(json.dumps(payload), encoding="utf-8")
    _run(["model", "apply", "exc", str(first)])

    moved = tmp_path / "moved.json"
    moved.write_text(json.dumps(_full_model(
        **{"workflow": _slot(80, "explicit", "high", "an approval queue instead")},
        )), encoding="utf-8")
    out = _run(["model", "apply", "exc", str(moved)])
    assert "exclusions to reconsider: 1" in out, (
        f"a change that unseats only an exclusion said nothing on the text path: {out!r}")


def test_a_threshold_only_invalidation_is_still_announced_on_the_apply_path(workspace, tmp_path):
    """#604, mirroring #599's fix: the same half-registered shape (invariant 1) is checked in advance for the
    fifth reasoning collection rather than found by a second-pass reviewer."""
    _run(["session", "init", "Something.", "--slug", "thr"])
    first = tmp_path / "first.json"
    first.write_text(json.dumps(_full_model(
        **{"workflow": _slot(80, "explicit", "high", "manual scan")},
        )), encoding="utf-8")
    # The threshold rests on `workflow` and nothing else does: no decision, no challenge, no exclusion.
    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["thresholds"] = [{"condition": "CAC exceeds the stated budget ceiling",
                              "measure": "cost per paid signup", "action": "stop the paid channel",
                              "rests_on": ["workflow"]}]
    first.write_text(json.dumps(payload), encoding="utf-8")
    _run(["model", "apply", "thr", str(first)])

    moved = tmp_path / "moved.json"
    moved.write_text(json.dumps(_full_model(
        **{"workflow": _slot(80, "explicit", "high", "an approval queue instead")},
        )), encoding="utf-8")
    out = _run(["model", "apply", "thr", str(moved)])
    assert "thresholds to reconsider: 1" in out, (
        f"a change that unseats only a threshold said nothing on the text path: {out!r}")
