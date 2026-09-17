"""The CLI's public flag names and parser shape (#83, #85, #72, #102, #248, #249, #284, #402), read off the built
parser and matched against `docs/compatibility.md` and `docs/cli.md`."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
from pathlib import Path

import anthropic
import httpx
import pytest

from requivo.cli import _build_parser, app
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput, _schema_order, schema_slot_ids

DOCS = Path(__file__).resolve().parents[1] / "docs"


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


class _RaisingClient:
    """`create()` always raises the same transport error, so every export flag sees one identical failure."""

    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise anthropic.APIConnectionError(message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))


class _CannedClient:
    """Answers with one canned JSON reply so `epic` can reach its writers."""

    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Response:
        stop_reason = "end_turn"

        def __init__(self, text):
            self.content = [_CannedClient._Block(text)]

    def __init__(self, *replies):
        self._replies = list(replies)
        self.messages = self

    def create(self, **kwargs):
        return _CannedClient._Response(self._replies.pop(0))


def _session_with_a_model(slug):
    store.create_session(slug, f"request for {slug}")
    _, required = schema_slot_ids()
    model = {sid: {"completeness": 0, "confidence": "empty", "impact": "low"} for sid in _schema_order() if sid in required}
    model["problem"] = {"completeness": 80, "confidence": "explicit", "impact": "high"}
    store.save_revision(slug, EngineOutput.model_validate(
        {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}))
    return slug


def _run_capturing(argv, client):
    """Run `app()`, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            app(argv, client=client)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def _walk_actions(parser):
    """Yield (verb path, action) for every argparse action reachable from the root parser."""
    stack = [(parser, ())]
    while stack:
        p, path = stack.pop()
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                stack.extend((sub, (*path, name)) for name, sub in action.choices.items())
            else:
                yield " ".join(path), action


def _subparsers(parser: argparse.ArgumentParser) -> list[tuple[str, argparse.ArgumentParser]]:
    return [(name, sub) for action in parser._actions if isinstance(action, argparse._SubParsersAction)
            for name, sub in action.choices.items()]

# ---- the epic export flags (#83) ----

EXPORT_FLAGS = ("--export-json", "--github", "--gitlab")


def test_epic_export_flags_report_the_same_failure_identically():
    """#83: `--json` used to diverge, envelope vs. prose; the three export flags report one identical failure."""
    slug = _session_with_a_model("flagtest-epic-errors")
    results = {flag: _run_capturing(["epic", slug, flag], client=_RaisingClient()) for flag in EXPORT_FLAGS}
    for flag, (code, out, err) in results.items():
        assert code == 1, f"{flag}: expected the clean-failure exit, got {code}"
        assert "Anthropic API unavailable" in err, f"{flag}: prose failure missing from stderr"
        assert out.strip() == "", f"{flag}: nothing should reach stdout, got {out[:120]!r}"
    assert len(set(results.values())) == 1, "the three export flags disagree: " + json.dumps(
        {f: {"code": c, "stdout": o[:80], "stderr": e[:80]} for f, (c, o, e) in results.items()})


def test_the_structured_envelope_is_still_reachable_on_a_verb_that_keeps_json():
    """Control: the harness can still see an envelope where a verb keeps `--json`."""
    code, out, err = _run_capturing(["session", "show", "no-such-session", "--json"], client=None)
    assert code == 1
    envelope = json.loads(out)
    assert envelope["code"] and envelope["message"]
    assert err.strip() == ""


def test_epic_no_longer_accepts_the_old_json_spelling():
    slug = _session_with_a_model("flagtest-epic-old-flag")
    code, _out, err = _run_capturing(["epic", slug, "--json"], client=_RaisingClient())
    assert code == 2 and "--json" in err  # argparse's usage error, not a run that happened


def test_epic_export_json_still_writes_the_neutral_export():
    slug = _session_with_a_model("flagtest-epic-writes")
    epic = {"title": "X", "issues": [{"id": "I-1", "title": "Build the request form"}]}
    with contextlib.redirect_stdout(io.StringIO()):
        app(["epic", slug, "--export-json"], client=_CannedClient(json.dumps(epic)))
    assert json.loads(store.canonical_dir(slug).joinpath("artifacts", "epic.json").read_text(encoding="utf-8"))


def test_every_other_verb_that_declares_json_still_binds_it_to_the_json_dest():
    """`getattr(args, "json", False)` must bind every `--json` verb to `json`; `epic` offers none."""
    offenders, epic_json, verbs_with_json = [], [], []
    for verb, action in _walk_actions(_build_parser()):
        if "--json" in action.option_strings:
            verbs_with_json.append(verb)
            if action.dest != "json":
                offenders.append((verb, action.dest))
        if verb == "epic" and action.dest == "json":
            epic_json.append(action.option_strings)
    assert offenders == [], f"--json bound to a dest other than `json`: {offenders}"
    assert epic_json == [], f"epic still carries a `json` dest: {epic_json}"
    assert len(verbs_with_json) >= 13, verbs_with_json  # a floor, so adding a verb cannot fail this for the wrong reason

# ---- the card selector (#85): `--context` primary, `--cards` a permanent alias on the same action ----

CARD_SELECTOR_VERBS = (
    (("discover", "a request"), "context"), (("session", "init", "a request"), "context"), (("context",), "cards"),
)


@pytest.mark.parametrize(("argv_head", "dest"), CARD_SELECTOR_VERBS)
@pytest.mark.parametrize("spelling", ["--context", "--cards"])
def test_both_spellings_of_the_card_selector_reach_the_same_dest(argv_head, dest, spelling):
    args = _build_parser().parse_args([*argv_head, spelling, "b2b-platform"])
    assert getattr(args, dest) == "b2b-platform"


def test_the_card_selector_is_one_action_not_two():
    """A second argument would let the later flag win; the alias is a second option string on one action."""
    seen = {}
    for verb, action in _walk_actions(_build_parser()):
        if {"--context", "--cards"}.intersection(action.option_strings):
            seen.setdefault(verb, []).append(sorted(action.option_strings))
    for verb, actions in seen.items():
        assert actions == [["--cards", "--context"]], f"{verb}: expected two option strings, got {actions}"
    assert set(seen) == {"discover", "run", "session init", "context", "session rescope"}, seen


def test_the_context_verb_prints_the_same_cards_under_either_spelling():
    def run(spelling):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            app(["context", spelling, "b2b-platform"], client=None)
        return buf.getvalue()

    printed = run("--cards")
    assert printed.strip() and printed == run("--context")

# ---- the `--json` perimeter (#102): docs/compatibility.md names every `--json` verb, both ways ----

_PROMISE_SECTION = "## The `--json` outputs are public"


def _json_verbs(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    found = []
    for name, sub in _subparsers(parser):
        path = f"{prefix} {name}".strip()
        if any("--json" in (a.option_strings or []) for a in sub._actions):
            found.append(path)
        found += _json_verbs(sub, path)
    return found


def test_every_json_verb_is_inside_the_promise():
    """Both directions, or a leak like #84 hides."""
    verbs = sorted(_json_verbs(_build_parser()))
    assert len(verbs) >= 10 and "doctor" in verbs and "session list" in verbs, f"the parser walk looks blind: {verbs}"
    text = (DOCS / "compatibility.md").read_text(encoding="utf-8")
    start = text.index(_PROMISE_SECTION)
    section = text[start:text.index("\n## ", start + 1)]
    named = set(re.findall(r"`requivo ([a-z]+(?: [a-z]+)?)`", section))
    missing = sorted(v for v in verbs if v not in named)
    assert not missing, f"these verbs accept `--json` and are not named in the promise table: {missing}"
    stale = sorted(n for n in named if n not in verbs)
    assert not stale, f"the promise table names verbs that do not take `--json`: {stale}"

# ---- the parser's shape (#72) ----


def test_pc_parser_binds_every_subcommand():
    cases = {
        ("discover", "req"): "_cmd_discover", ("status", "m.json"): "_cmd_status", ("impact", "m.json"): "_cmd_impact",
        ("brief", "m.json"): "_cmd_brief", ("prd", "m.json"): "_cmd_prd", ("stories", "m.json"): "_cmd_stories",
        ("estimate", "m.json"): "_cmd_estimate", ("criteria", "m.json"): "_cmd_criteria",
        ("epic", "m.json"): "_cmd_epic", ("release", "m.json"): "_cmd_release",
    }
    for argv, fname in cases.items():
        assert _build_parser().parse_args(list(argv)).func.__name__ == fname
    assert _build_parser().parse_args(["epic", "m", "--github", "--gitlab"]).github
    assert _build_parser().parse_args(["release", "m", "v1.0"]).version == "v1.0"
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["bogus"])


def test_documented_cli_commands_exist():
    documented = {"discover", "answer", "status", "impact", "brief", "prd", "stories", "estimate", "criteria", "epic",
                  "release", "web", "api", "demo", "doctor", "schema", "context", "session", "model", "artifact"}
    missing = documented - {name for name, _ in _subparsers(_build_parser())}
    assert not missing, f"documented CLI commands missing from the parser: {sorted(missing)}"

# ---- one name for a session reference (#248), and which verbs take a path (#402) ----

SESSION_TAKING_JOURNEY_VERBS = ("answer", "status", "impact", "brief", "prd", "stories", "estimate", "criteria", "epic", "release")
PATH_ACCEPTING_JOURNEY_VERBS = ("status", "impact")  # the other eight resolve a slug (#402)


def _positionals(parser):
    found = {}
    for verb, action in _walk_actions(parser):
        if not action.option_strings:
            found.setdefault(verb, []).append(action.dest)
    return found


def _session_helps():
    return {verb: action.help for verb, action in _walk_actions(_build_parser())
            if not action.option_strings and action.dest == "session"}


def test_every_session_reference_positional_is_spelled_session():
    """#248: a property of the whole parser, so a later copycat verb fails too."""
    positionals = _positionals(_build_parser())
    assert len(positionals) >= 20, f"the parser walk looks blind: {positionals}"
    for verb in SESSION_TAKING_JOURNEY_VERBS:
        assert positionals.get(verb, [])[:1] == ["session"], f"`{verb}`: first positional is {positionals.get(verb)}"
    offenders = {v: d for v, d in positionals.items() if "model" in d}
    assert offenders == {}, f"a positional still calls a session reference `model`: {offenders}"


def test_the_missing_argument_error_names_a_session_not_a_model():
    """`metavar` could drift from `dest` unseen; `brief`, since #541 made `status`'s positional optional."""
    code, out, err = _run_capturing(["brief"], client=None)
    assert code == 2, f"expected argparse's usage error, got {code}: {err!r}"
    assert "required: session" in err and "required: model" not in err, err


def test_only_status_and_impact_document_the_saved_model_json_path():
    """#402: the two path-accepting verbs say so; the eight write verbs document a bare slug only."""
    helps = _session_helps()
    assert len(helps) >= 15, f"the parser walk looks blind: {sorted(helps)}"
    for verb in PATH_ACCEPTING_JOURNEY_VERBS:
        assert "model.json" in (helps.get(verb) or ""), f"`{verb}` stopped documenting model.json: {helps.get(verb)!r}"
    write_verbs = [v for v in SESSION_TAKING_JOURNEY_VERBS if v not in PATH_ACCEPTING_JOURNEY_VERBS]
    assert len(write_verbs) == 8, f"expected eight write verbs, found {write_verbs}"
    for verb in write_verbs:
        assert (helps.get(verb) or "") == "a session slug", f"`requivo {verb}` documents {helps.get(verb)!r}, but cannot open a path"


def test_docs_cli_md_names_the_same_two_path_accepting_verbs_as_the_parser():
    text = (DOCS / "cli.md").read_text(encoding="utf-8")
    marker = "also accept a path to a saved"
    line = next((ln for ln in text.splitlines() if marker in ln), None)
    assert line is not None, "docs/cli.md no longer says which verbs accept a model.json path"
    documented = tuple(re.findall(r"`([a-z]+)`", line.split(marker)[0]))
    assert documented == PATH_ACCEPTING_JOURNEY_VERBS, f"got {documented}; parser says {PATH_ACCEPTING_JOURNEY_VERBS}"

# ---- a global flag is global wherever it is written (#249) ----


def _verbs_missing_workspace(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    missing = []
    if prefix and not any("--workspace" in a.option_strings for a in parser._actions):
        missing.append(prefix)
    for name, sp in _subparsers(parser):
        missing.extend(_verbs_missing_workspace(sp, f"{prefix} {name}".strip()))
    return missing


def test_every_verb_accepts_workspace_after_its_own_name():
    missing = _verbs_missing_workspace(_build_parser())
    assert not missing, f"these verbs do not bind `--workspace`: {sorted(set(missing))}"


@pytest.mark.parametrize("argv", [
    ["--workspace", "/w", "status", "m.json"], ["status", "m.json", "--workspace", "/w"],
    ["--workspace", "/w", "session", "list"], ["session", "list", "--workspace", "/w"], ["--workspace", "/w", "web"],
])
def test_workspace_parses_identically_before_and_after_the_command(argv):
    """Every subparser copy carries `default=argparse.SUPPRESS`, so no copy clobbers the global value."""
    assert _build_parser().parse_args(argv).workspace == "/w"


def test_an_unknown_flag_after_the_command_is_still_refused():
    """MUST-FIRE: a fix that stopped argparse minding unknown arguments would pass every assertion above."""
    for argv in (["status", "m.json", "--worksapce", "/w"], ["session", "list", "--nonsuch"]):
        with pytest.raises(SystemExit) as e:
            _build_parser().parse_args(argv)
        assert e.value.code == 2


def test_the_workspace_help_no_longer_tells_the_reader_to_place_it_first():
    action = next(a for a in _build_parser()._actions if "--workspace" in a.option_strings)
    assert "before the command" not in (action.help or ""), "the global --workspace help still says to place it first"


def test_workspace_after_the_command_reaches_the_session_store(tmp_path):
    root = tmp_path / "elsewhere"
    root.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        app(["session", "init", "A leave approval system", "--slug", "ws-after", "--workspace", str(root)], client=None)
    assert (root / ".requivo" / "sessions" / "ws-after" / "session.json").is_file()

# ---- every real flag is mentioned in docs/cli.md (#284) ----


def _real_flags(parser: argparse.ArgumentParser, prefix: str = "", global_flags: frozenset[str] | None = None):
    """Every action with a real option string; a re-declared root flag (#249) is exempt below root, keyed on root only."""
    if global_flags is None:
        global_flags = frozenset(opt for a in parser._actions for opt in a.option_strings)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _real_flags(sub, f"{prefix} {name}".strip(), global_flags)
        elif set(action.option_strings) - {"-h", "--help"}:
            if prefix and (set(action.option_strings) & global_flags):
                continue
            yield (prefix, action)


def test_the_inherited_flag_exemption_skips_only_a_re_declared_global_flag():
    """MUST-FIRE, two levels deep: an ancestor-keyed exemption let an intermediate group hide a leaf flag."""
    p = argparse.ArgumentParser(prog="x")
    p.add_argument("--workspace")
    sub = p.add_subparsers()
    sp = sub.add_parser("go")
    sp.add_argument("--workspace", default=argparse.SUPPRESS)
    sp.add_argument("--only-here")
    grp = sub.add_parser("grp")
    grp.add_argument("--dry-run")
    leaf = grp.add_subparsers().add_parser("leaf")
    leaf.add_argument("--dry-run")
    leaf.add_argument("--workspace", default=argparse.SUPPRESS)
    yielded = {(verb, tuple(a.option_strings)) for verb, a in _real_flags(p)}
    assert ("go", ("--only-here",)) in yielded and ("go", ("--workspace",)) not in yielded
    assert ("", ("--workspace",)) in yielded, "the global flag is still demanded, once, at root"
    assert ("grp", ("--dry-run",)) in yielded and ("grp leaf", ("--dry-run",)) in yielded
    assert ("grp leaf", ("--workspace",)) not in yielded


def test_every_workspace_copy_carries_the_same_help_text():
    """#249: `web`'s copy predated the rest and drifted alone."""
    helps = {(a.help or "") for _verb, a in _real_flags(_build_parser(), global_flags=frozenset()) if "--workspace" in a.option_strings}
    assert len(helps) == 1, f"the --workspace copies describe the flag {len(helps)} different ways: {helps}"
    assert "before or after the command" in helps.pop()


def test_the_only_flags_the_root_parser_binds_are_the_two_global_ones():
    """What bounds the exemption: a third root flag fails here."""
    bound = {opt for a in _build_parser()._actions for opt in a.option_strings} - {"-h", "--help"}
    assert bound == {"--version", "--workspace"}


def test_every_real_flag_is_documented_in_the_cli_reference():
    """#284: the long form on the same physical line as `requivo <verb>` in docs/cli.md."""
    lines = (DOCS / "cli.md").read_text(encoding="utf-8").splitlines()
    checked, missing = 0, []
    for verb, action in _real_flags(_build_parser()):
        candidates = [opt for opt in action.option_strings if opt.startswith("--")] or list(action.option_strings)
        checked += 1
        marker = f"requivo {verb}".strip() if verb else "requivo"
        if not any(marker in line and any(opt in line for opt in candidates) for line in lines):
            missing.append((verb or "(top level)", action.option_strings))
    assert checked >= 30, f"the parser walk looks blind: only {checked} flag(s) found"
    assert not missing, f"flags real but undocumented in docs/cli.md under their own `requivo <verb>`: {missing}"
