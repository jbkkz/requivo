"""The plumbing verbs end to end (#141): `model validate/apply/diff`, `artifact save/list/show`, documents on
stdin, the `cli.py`/`deterministic/` axis (#296), and how every verb resolves a session reference (#402, #414)."""
from __future__ import annotations

import argparse
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from _fakes import (
    forge_meta,
    full_model,
    run_cli,
    run_cli_exit,
    run_cli_json,
    run_cli_stdin,
    simulate_py314_denied_path,
    slot,
)

from requivo import cli, deterministic
from requivo.cli import _build_parser, app
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput
from requivo.core.errors import SessionNotFoundError
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

_MOVED = {"workflow": slot(80, "explicit", "high", "new")}


def _write(path: Path, payload) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _init(slug: str = "s", proposal=None, where: Path | None = None) -> None:
    run_cli(["session", "init", "Something.", "--slug", slug])
    if proposal is not None:
        run_cli(["model", "apply", slug, _write((where or Path.cwd()) / f"{slug}-proposal.json", proposal)])


def _fails(argv, client=None) -> str:
    """`app()` under `SystemExit(1)`, returning stderr."""
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err), pytest.raises(SystemExit) as e:
        app(argv, client=client)
    assert e.value.code == 1, argv
    return err.getvalue()


# ── model validate / apply / diff ────────────────────────────────────────────────


def test_model_validate_ok_and_invalid_exit(tmp_path):
    assert run_cli_json(["model", "validate", _write(tmp_path / "good.json", full_model()), "--json"])["status"] == "valid"
    bad = _write(tmp_path / "bad.json", {"model": {"nope": slot()}, "summary": {}})
    assert run_cli_exit(["model", "validate", bad, "--json"])[1] == 1
    with pytest.raises(SystemExit):   # `--session` was declared and read by nothing
        _build_parser().parse_args(["model", "validate", "p.json", "--session", "s"])
    assert _build_parser().parse_args(["model", "diff", "s", "p.json"]).func.__name__ == "_cmd_model_diff"


def test_apply_refuses_a_partial_model_instead_of_replacing_the_whole_one(tmp_path):
    """`--allow-partial` on `apply` read as "apply a patch"; it merged nothing."""
    _init("s", full_model(), tmp_path)
    before = len(SessionService().load_model("s").model)
    partial = _write(tmp_path / "partial.json", {"model": {"workflow": slot(80, "explicit", "high", "scan")}, "summary": {"objective": "Something"}})
    assert run_cli_exit(["model", "apply", "s", partial, "--json"])[1] == 1
    assert len(SessionService().load_model("s").model) == before   # the model is untouched
    assert run_cli_json(["model", "validate", partial, "--allow-partial", "--json"])["slots"] == 1


def test_model_apply_and_status_and_artifact_flow(tmp_path):
    _init("event")
    proposal = _write(tmp_path / "p.json", full_model(workflow=slot(70, "inferred", "high", "scan")))
    applied = run_cli_json(["model", "apply", "event", proposal, "--json"])
    assert applied["status"] == "applied" and applied["revision"] == 1
    assert "workflow" in applied["readiness"]["blocking_slots"]  # inferred high-impact blocks
    status = run_cli_json(["status", "event", "--json"])
    assert status["revision"] == 1 and status["readiness"]["ready"] is False
    brief = tmp_path / "brief.md"
    brief.write_text("# Assessment\n", encoding="utf-8")
    run_cli(["artifact", "save", "event", "--type", "brief", "--file", str(brief), "--revision", "1"])
    listed = run_cli_json(["artifact", "list", "event", "--json"])["artifacts"]
    assert listed["brief"]["revision"] == 1 and listed["brief"]["stale"] is False


def test_a_refused_apply_writes_nothing_and_answers_like_validate(tmp_path):
    """A refused apply answers exactly as `model validate` does and leaves the store as it found it (#511)."""
    _init("s")
    bad = _write(tmp_path / "bad.json", {"model": {"ghost": slot()}, "summary": {"objective": "o"}})

    def envelope(argv):
        out, code = run_cli_exit(argv)
        return code, json.loads(out)

    apply_code, apply_env = envelope(["model", "apply", "s", bad, "--expected-revision", "0", "--json"])
    validate_code, validate_env = envelope(["model", "validate", bad, "--json"])
    assert apply_code == validate_code == 1 and apply_env == validate_env
    assert apply_env["code"] == "unknown_slot" and apply_env["details"]["slots"] == ["ghost"]
    assert SessionService().repo.read_meta("s").current_revision == 0
    d = store.canonical_dir("s")
    assert not (d / "model.json").exists() and not list((d / "revisions").glob("*")), "a refused apply must not write"


def test_model_apply_honours_the_expected_revision_precondition(tmp_path):
    """`diff` writes nothing; `apply --expected-revision N` is refused once N is stale."""
    _init("s")
    p = _write(tmp_path / "p.json", full_model())
    run_cli(["model", "apply", "s", p, "--expected-revision", "0", "--json"])
    assert run_cli_json(["model", "diff", "s", p, "--json"])["status"] == "planned"
    assert store.read_meta("s").current_revision == 1
    p2 = _write(tmp_path / "p2.json", full_model(**_MOVED))
    run_cli(["model", "apply", "s", p2, "--expected-revision", "1", "--json"])
    assert run_cli_exit(["model", "apply", "s", p2, "--expected-revision", "1", "--json"])[1] != 0


def test_a_corrupt_model_reaches_the_operator_as_one_line_not_a_traceback():
    """The end-to-end half of #204, from the three verbs a user types, and the `--json` envelope's own code."""
    svc = SessionService()
    svc.create_session("A leave approval system.", slug="corrupt")
    svc.update_model("corrupt", full_model())
    (store.canonical_dir("corrupt") / "model.json").write_text("{", encoding="utf-8")
    for argv in (["status", "corrupt"], ["impact", "corrupt"], ["model", "show", "corrupt"]):
        err = _fails(argv)
        assert "Traceback" not in err and "pydantic" not in err, f"{argv} still surfaces a raw parse failure: {err!r}"
        assert "model.json" in err and "requivo session verify corrupt" in err and "revisions/" in err, argv
    assert run_cli_exit(["status", "corrupt", "--json"])[1] == 1


def test_status_and_model_show_agree_on_a_revision_zero_session():
    """#250: a claimed-but-undiscovered session names `discover` from both verbs; a never-created one names `session list`."""
    run_cli(["session", "init", "A tiny tool to track something.", "--slug", "rev0"])
    for argv in (["status", "rev0"], ["model", "show", "rev0"]):
        err = _fails(argv)
        assert "requivo discover" in err and "apply a proposal first" not in err, (argv, err)
    err = _fails(["model", "show", "no-such-slug-at-all"])
    assert "only the request was captured" not in err and "requivo session list" in err


def test_impact_on_an_unmatched_slot_exits_1_not_0(tmp_path):
    """A wrong probe used to be indistinguishable from an empty result (#250)."""
    _init("s", full_model(), tmp_path)
    _fails(["impact", "s", "not-a-real-slot"])


@pytest.mark.parametrize("key, item, announced", [
    ("exclusions", {"option": "Bulk import", "reason": "Out of scope for v1", "rests_on": ["workflow"]}, "exclusions to reconsider: 1"),
    ("thresholds", {"condition": "CAC exceeds the stated budget ceiling", "measure": "cost per paid signup",
                    "action": "stop the paid channel", "rests_on": ["workflow"]}, "thresholds to reconsider: 1"),
], ids=["exclusion", "threshold"])
def test_an_exclusion_only_invalidation_is_still_announced_on_the_apply_path(tmp_path, key, item, announced):
    """#599 and #604: a change that unseats only an exclusion, or only a threshold, is announced on the text path."""
    _init("x")
    first = {**full_model(workflow=slot(80, "explicit", "high", "manual scan")), key: [item]}   # rests on `workflow` alone
    run_cli(["model", "apply", "x", _write(tmp_path / "first.json", first)])
    out = run_cli(["model", "apply", "x", _write(tmp_path / "moved.json", full_model(workflow=slot(80, "explicit", "high", "an approval queue instead")))])
    assert announced in out, f"a change that unseats only {key} said nothing on the text path: {out!r}"


# ── artifact save / list / show ───────────────────────────────────────────────────


def test_artifact_list_json_has_a_top_level_that_is_not_data():
    """`artifact list --json` wraps the service's row under `artifacts` and names the slug asked for (#107, #87, invariant 14)."""
    run_cli(["session", "init", "Something.", "--slug", "aj"])
    row = {"revision": 1, "filename": "prd.md", "updated_at": "2026-01-01T00:00:00Z", "stale": False}
    forge_meta("aj", {"artifact_status": {"prd": row}})
    payload = run_cli_json(["artifact", "list", "aj", "--json"])
    assert "prd" not in payload and payload == {"slug": "aj", "artifacts": {"prd": row}}
    assert list(payload["artifacts"]["prd"]) == list(row)
    forge_meta("aj", {"slug": "forged"})
    assert run_cli_json(["artifact", "list", "aj", "--json"])["slug"] == "aj"
    run_cli(["session", "init", "Nothing saved yet.", "--slug", "noart"])
    assert run_cli_json(["artifact", "list", "noart", "--json"]) == {"slug": "noart", "artifacts": {}}


def test_artifact_save_reports_staleness_at_save_time(tmp_path):
    """Staleness is read at save time against `--revision`, and an omitted `--revision` is refused, not guessed (#6, #57)."""
    _init("s", full_model(), tmp_path)
    run_cli(["model", "apply", "s", _write(tmp_path / "p2.json", full_model(**_MOVED)), "--json"])   # revision 2
    doc = tmp_path / "prd.md"
    doc.write_text("# PRD\n", encoding="utf-8")
    save = ["artifact", "save", "s", "--type", "prd", "--file", str(doc)]
    r = run_cli_json([*save, "--revision", "1", "--json"])
    assert r["revision"] == 1 and r["stale"] is True
    assert run_cli_json(["artifact", "list", "s", "--json"])["artifacts"]["prd"]["stale"] is True
    fresh = run_cli_json([*save, "--revision", "2", "--json"])
    assert fresh["revision"] == 2 and fresh["stale"] is False
    out, code = run_cli_exit([*save, "--json"])
    envelope = json.loads(out)
    assert code == 1 and envelope["details"]["source_revision"] is None and "--revision" in envelope["message"]
    assert envelope["code"] == "unstated_source_revision"
    assert run_cli_json(["artifact", "list", "s", "--json"])["artifacts"]["prd"]["revision"] == 2


def _subparser(parser, name):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and name in action.choices:
            return action.choices[name]
    raise AssertionError(f"no `{name}` subparser under {parser.prog!r}")


def test_the_revision_flag_does_not_advertise_a_default_it_no_longer_has():
    """The help text is read while deciding whether to pass the flag (#6, #249)."""
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as ei:
        _build_parser().parse_args(["artifact", "save", "--help"])
    assert ei.value.code == 0
    help_text = buf.getvalue()
    assert "--revision" in help_text, "must fire: this is not the help text that owns the flag"
    chunk = help_text.rsplit("--revision", 1)[1].split("--json", 1)[0].lower()
    assert "required" in chunk, f"`--revision` does not say it is required: {chunk!r}"
    root = _build_parser()
    inherited = {opt for a in root._actions for opt in a.option_strings}
    own = [a for a in _subparser(_subparser(root, "artifact"), "save")._actions if not (set(a.option_strings) & inherited)]
    assert any("--revision" in a.option_strings for a in own), "must fire: `--revision` fell out of the set this sweep reads"
    assert len(own) >= 4, f"must fire: the walk found only {len(own)} option(s) on `artifact save`"
    offenders = [(a.option_strings or a.dest, form) for a in own for form in ("default:", "defaults to") if form in (a.help or "").lower()]
    assert not offenders, f"an option `artifact save` owns advertises a default: {offenders}"


# ── `deterministic/`: registration and documents on stdin (#141) ──────────────────


def test_new_verbs_are_bound_in_the_parser():
    for argv, fname in [(["doctor"], "_cmd_doctor"), (["session", "init", "r"], "_cmd_session_init"),
                        (["session", "migrate"], "_cmd_session_migrate"), (["model", "apply", "s", "p.json"], "_cmd_model_apply"),
                        (["model", "validate", "p.json"], "_cmd_model_validate"),
                        (["artifact", "save", "s", "--type", "prd", "--file", "f"], "_cmd_artifact_save")]:
        assert _build_parser().parse_args(argv).func.__name__ == fname


def test_a_proposal_can_be_applied_from_stdin(monkeypatch):
    """`-` on `session init`, `model validate/apply` and `artifact save --file` reads the document from stdin."""
    r = json.loads(run_cli_stdin(["session", "init", "-", "--slug", "s", "--json"], "We need a leave approval system.\n", monkeypatch))
    assert r["slug"] == "s" and "leave approval" in store.session_request("s")
    proposal = json.dumps(full_model())
    assert json.loads(run_cli_stdin(["model", "validate", "-", "--json"], proposal, monkeypatch))["status"] == "valid"
    applied = json.loads(run_cli_stdin(["model", "apply", "s", "-", "--expected-revision", "0", "--json"], proposal, monkeypatch))
    assert applied["revision"] == 1
    r = json.loads(run_cli_stdin(["artifact", "save", "s", "--type", "prd", "--file", "-", "--revision", "1", "--json"],
                                 "# PRD\nwritten straight to stdin\n", monkeypatch))
    assert r["revision"] == 1 and r["stale"] is False
    assert "straight to stdin" in run_cli(["artifact", "show", "s", "--type", "prd"])


def test_a_missing_document_path_is_an_error_not_content(monkeypatch):
    """A path that does not exist is an error, and stdin as a terminal is refused rather than waited on."""
    run_cli(["session", "init", "Something.", "--slug", "s", "--json"])
    assert run_cli_exit(["model", "apply", "s", "no-such-file.json", "--json"])[1] != 0

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", _Tty(""))
    assert run_cli_exit(["model", "apply", "s", "-", "--json"])[1] != 0


def test_the_three_no_llm_journey_verbs_still_live_in_cli_py():
    """#296: `status`, `demo` and `impact` stay in `cli.py`, and both prose sites that state the axis still say so."""
    for name in ("_cmd_status", "_cmd_demo", "_cmd_impact"):
        assert getattr(cli, name).__module__ == "requivo.cli", f"{name} moved out of cli.py; the documented axis moves with it (#296)"
    page = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    start = page.index("\nrequivo/\n")
    tree = page[start:page.index("\n```", start)]
    for verb in ("status", "demo", "impact"):
        assert verb in tree and verb in (deterministic.__doc__ or ""), f"{verb!r} is no longer named as a no-LLM verb kept in cli.py (#296)"


# ── `SessionService.resolve_slug` and the verbs over it (#402, #414) ──────────────

_WRITE_VERB_ARGV = [["answer", "some answers"], ["brief"], ["prd"], ["stories"], ["estimate"], ["criteria"], ["epic"], ["release"]]
_NT_SKIP = "POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: that resolve_slug converts a PermissionError into a clean SessionNotFoundError."


def test_resolve_slug_refuses_a_model_json_path_when_the_caller_opted_out(tmp_path):
    """The eight write verbs pass `accept_path=False`; a bare slug is still accepted."""
    ref = str(tmp_path / "loose" / "model.json")
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(ref, accept_path=False)
    assert exc.value.details["ref"] == ref
    assert "loose" not in str(exc.value).replace(ref, ""), "the refusal must not name a slug mined from the path"
    assert SessionService().resolve_slug("leave-approval", accept_path=False) == "leave-approval"


def test_the_path_refusal_cannot_forge_a_second_line_of_its_own_message():
    """The refusal echoes `ref` twice, escaped (#40)."""
    hostile = "evil/model.json\n\x1b[31mFAKE ERROR: session corrupted\x1b[0m"
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(hostile, accept_path=False)
    rendered = str(exc.value)
    assert "\n" not in rendered and "\x1b" not in rendered, f"a raw newline or escape survived into the refusal: {rendered!r}"
    assert "evil" in rendered and "model.json" in rendered   # must fire: the reference was not thrown away


def test_resolve_slug_no_longer_mines_a_nonexistent_model_json_path(tmp_path):
    """The root cause, independent of `accept_path` (#402)."""
    ref = str(tmp_path / "loose" / "model.json")
    assert SessionService().resolve_slug(ref) == ref   # named as given, not mined


def test_resolve_slug_refuses_a_directory_that_is_not_a_session(tmp_path):
    """#414: the directory branch used to mine ANY directory's own name."""
    d = tmp_path / "elsewhere" / "loose"
    d.mkdir(parents=True)
    (d / "unrelated.txt").write_text("nothing session-shaped in here", encoding="utf-8")
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(str(d))
    assert exc.value.details["ref"] == str(d)


@pytest.mark.parametrize("marker, by_file", [("model.json", True), ("session.json", False), ("model.json", False)],
                         ids=["saved-model-json", "session-directory", "legacy-directory"])
def test_resolve_slug_still_mines_a_real_saved_model_json(tmp_path, marker, by_file):
    """Must-fire controls: a real model.json path or a directory that really is a session still resolves by name."""
    d = tmp_path / "leave-approval"
    d.mkdir()
    (d / marker).write_text("{}", encoding="utf-8")
    assert SessionService().resolve_slug(str(d / marker) if by_file else str(d)) == "leave-approval"


@pytest.mark.parametrize("branch", ["model.json", "directory"])
def test_an_unreadable_model_json_path_refuses_cleanly_instead_of_crashing(tmp_path, request, branch):
    """A `PermissionError` on the model.json probe or the directory's marker probe is a clean refusal (#402, #414)."""
    if os.name == "nt":
        pytest.skip(_NT_SKIP)
    d = tmp_path / "noaccess"
    d.mkdir()
    request.addfinalizer(lambda: d.chmod(0o755))
    d.chmod(0o000)
    ref = str(d / "model.json") if branch == "model.json" else str(d)
    try:
        (Path(ref) if branch == "model.json" else d / "session.json").stat()
    except PermissionError:
        pass
    else:
        pytest.skip("chmod 000 did not deny the probe on this run (running as root?). UNTESTED HERE: the could-not-tell arm of resolve_slug.")
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(ref)
    assert exc.value.details["ref"] == ref


def test_a_directory_reference_under_a_blocked_ancestor_refuses_cleanly_too(tmp_path, request):
    """Found in review of #414: the entry gate, not only the marker probe, can raise."""
    if os.name == "nt":
        pytest.skip(_NT_SKIP)
    parent = tmp_path / "blocked-parent"
    target = parent / "session-slug"
    target.mkdir(parents=True)
    (target / "session.json").write_text("{}", encoding="utf-8")
    request.addfinalizer(lambda: parent.chmod(0o755))
    parent.chmod(0o000)
    ref = str(target)
    try:
        Path(ref).stat()
    except PermissionError:
        pass
    else:
        pytest.skip("chmod 000 on the parent did not deny the stat() probe (running as root?). UNTESTED HERE: the ancestor-blocked arm.")
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(ref)
    assert exc.value.details["ref"] == ref


@pytest.mark.parametrize("branch", ["file", "directory", "marker"])
def test_resolve_slug_does_not_read_an_inaccessible_path_as_absent_on_py314(tmp_path, monkeypatch, branch):
    """#636: all three path-shaped reference probes retain the could-not-tell result."""
    d = tmp_path / "session-slug"
    d.mkdir()
    denied = d if branch == "directory" else d / ("model.json" if branch == "file" else "session.json")
    simulate_py314_denied_path(monkeypatch, denied)
    ref = str(denied if branch == "file" else d)
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(ref)
    assert exc.value.details["ref"] == ref
    assert isinstance(exc.value.__cause__, PermissionError)


@pytest.mark.parametrize("tail", _WRITE_VERB_ARGV, ids=lambda a: a[0])
def test_a_nonexistent_model_json_path_does_not_silently_use_an_unrelated_real_session(tail, workspace):
    """#402 through every write verb: the path is named, and a same-named real session is not used instead."""
    store.create_session("loose", "an unrelated real session")
    verb, *rest = tail
    ref = str(workspace / "loose" / "model.json")   # does not exist; "loose" only coincides in name
    err = _fails([verb, ref, *rest])
    assert ref in err, f"`requivo {verb}` does not name the path it was given: {err!r}"
    assert "no session named loose" not in err.lower(), f"`requivo {verb}` reported on the unrelated real session: {err!r}"
    with pytest.raises(SystemExit):   # must-fire control: resolution by slug still succeeds
        app([verb, "loose", *rest], client=None)


def test_status_and_impact_still_open_a_model_json_path_directly(workspace):
    """The two verbs #402 leaves untouched read the file's own bytes and never go near `resolve_slug`."""
    loose = workspace / "elsewhere" / "model.json"
    loose.parent.mkdir(parents=True)
    loose.write_text(EngineOutput.model_validate(full_model()).model_dump_json(), encoding="utf-8")
    assert "UNDERSTANDING" in run_cli(["status", str(loose)])
    assert "DEPENDENCY MAP" in run_cli(["impact", str(loose)])


def test_a_directory_reference_does_not_silently_use_an_unrelated_real_session(workspace):
    """#414's worse half, and its must-fire control: the real session stays reachable by its own slug."""
    store.create_session("loose", "an unrelated real session")
    ref_dir = workspace / "elsewhere" / "loose"
    ref_dir.mkdir(parents=True)
    (ref_dir / "unrelated.txt").write_text("not a session", encoding="utf-8")
    err = _fails(["session", "show", str(ref_dir)])
    assert "no session named loose" not in err.lower() and str(ref_dir) in err
    out = run_cli(["session", "show", "loose"])
    assert "no session named" not in out.lower() and "loose" in out
