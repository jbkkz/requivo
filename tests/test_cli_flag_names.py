"""The CLI flag names, and the error channel one of them silently switched.

#83 -- `epic --json` wrote a file but also flipped `cli.app()`'s `want_json` flag, switching failures to a stdout envelope too; renamed to `--export-json` for both. #85 -- the context-card selector was `--context` on two verbs, `--cards` on a third; both now work. #72 added the parser-shape tests at the foot
of the file."""
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


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


class _RaisingClient:
    """create() always raises the same transport error, so all three export flags see one identical failure."""

    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise anthropic.APIConnectionError(message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))


class _CannedClient:
    """Answers with one canned JSON reply so `epic` can reach its writers; local, not `_fakes.py`'s shared client."""

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
    model = {sid: {"completeness": 0, "confidence": "empty", "impact": "low"}
             for sid in _schema_order() if sid in required}
    model["problem"] = {"completeness": 80, "confidence": "explicit", "impact": "high"}
    store.save_revision(slug, EngineOutput.model_validate(
        {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}))
    return slug


def _run_capturing(argv, client):
    """Run `app()`, returning (exit_code, stdout, stderr); the exit code is part of the observation, not an accident."""
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

EXPORT_FLAGS = ("--export-json", "--github", "--gitlab")


def test_epic_export_flags_report_the_same_failure_identically():
    """The three export flags must report one identical failure (#83): `--json` used to diverge, envelope vs. prose."""
    slug = _session_with_a_model("flagtest-epic-errors")

    results = {flag: _run_capturing(["epic", slug, flag], client=_RaisingClient()) for flag in EXPORT_FLAGS}

    # Positive half first: proves the failure happened, so the identity check below is not comparing empty strings.
    for flag, (code, out, err) in results.items():
        assert code == 1, f"{flag}: expected the clean-failure exit, got {code}"
        assert "Anthropic API unavailable" in err, f"{flag}: prose failure missing from stderr"
        assert out.strip() == "", f"{flag}: nothing should reach stdout, got {out[:120]!r}"

    # And now the identity itself.
    assert len(set(results.values())) == 1, (
        "the three export flags disagree: "
        + json.dumps({f: {"code": c, "stdout": o[:80], "stderr": e[:80]} for f, (c, o, e) in results.items()}))


def test_the_structured_envelope_is_still_reachable_on_a_verb_that_keeps_json():
    """Control for the test above: `epic` must not emit the envelope, but the harness must still be able to see one."""
    code, out, err = _run_capturing(["session", "show", "no-such-session", "--json"], client=None)

    assert code == 1
    envelope = json.loads(out)                    # it really is JSON, on stdout
    assert envelope["code"] and envelope["message"]
    assert err.strip() == ""                      # and the prose channel stayed quiet


def test_epic_no_longer_accepts_the_old_json_spelling():
    """`--json` is not a prefix of `--export-json`, so argparse rejects it (exit 2) rather than reinterpreting it."""
    slug = _session_with_a_model("flagtest-epic-old-flag")
    code, _out, err = _run_capturing(["epic", slug, "--json"], client=_RaisingClient())

    assert code == 2                              # argparse's usage error, not a run that happened
    assert "--json" in err


def test_epic_export_json_still_writes_the_neutral_export():
    """The rename moved the name, not the behaviour: `--export-json` writes the same file that `--json` used to."""
    slug = _session_with_a_model("flagtest-epic-writes")
    epic = {"title": "X", "issues": [{"id": "I-1", "title": "Build the request form"}]}
    with contextlib.redirect_stdout(io.StringIO()):
        app(["epic", slug, "--export-json"], client=_CannedClient(json.dumps(epic)))

    written = store.canonical_dir(slug).joinpath("artifacts", "epic.json")
    assert json.loads(written.read_text(encoding="utf-8"))


def test_every_other_verb_that_declares_json_still_binds_it_to_the_json_dest():
    """The generic `getattr(args, "json", False)` must bind every `--json` verb to `json`; `epic` offers none."""
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
    # The count is a floor, not exact, so adding a verb won't fail this test for the wrong reason.
    assert len(verbs_with_json) >= 13, verbs_with_json

CARD_SELECTOR_VERBS = (
    (("discover", "a request"), "context"), (("session", "init", "a request"), "context"), (("context",), "cards"),
)


@pytest.mark.parametrize(("argv_head", "dest"), CARD_SELECTOR_VERBS)
@pytest.mark.parametrize("spelling", ["--context", "--cards"])
def test_both_spellings_of_the_card_selector_reach_the_same_dest(argv_head, dest, spelling):
    """`--context` is primary, `--cards` a permanent alias on one action, so they can't drift; the dest never moved."""
    args = _build_parser().parse_args([*argv_head, spelling, "b2b-platform"])
    assert getattr(args, dest) == "b2b-platform"


def test_the_card_selector_is_one_action_not_two():
    """The alias must be a second option string on one action, not a second argument that lets the later flag win."""
    seen = {}
    for verb, action in _walk_actions(_build_parser()):
        if {"--context", "--cards"}.intersection(action.option_strings):
            seen.setdefault(verb, []).append(sorted(action.option_strings))

    for verb, actions in seen.items():
        assert actions == [["--cards", "--context"]], f"{verb}: expected two option strings, got {actions}"
    assert set(seen) == {"discover", "run", "session init", "context", "session rescope"}, seen


def test_the_context_verb_prints_the_same_cards_under_either_spelling():
    """End-to-end on the card-selecting verb needing no provider, so the alias is proved against the real handler."""
    def run(spelling):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            app(["context", spelling, "b2b-platform"], client=None)
        return buf.getvalue()

    printed = run("--cards")
    assert printed.strip()                      # it really printed a card, not an empty selection
    assert printed == run("--context")

# ── the `--json` perimeter (#102) ────────────────────────────────────────────
# docs/compatibility.md must name every `--json` verb both ways (the whole set now), or #84 repeats unnoticed.

_PROMISE_SECTION = "## The `--json` outputs are public"


def _json_verbs(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """Every verb accepting `--json`, read off the built parser -- what's promised is what the parser accepts."""
    found = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                path = f"{prefix} {name}".strip()
                if any("--json" in (a.option_strings or []) for a in sub._actions):
                    found.append(path)
                found += _json_verbs(sub, path)
    return found


def test_every_json_verb_is_inside_the_promise():
    """Every `--json` verb must be named in the promise table and vice versa, or a leak like #84 hides."""
    verbs = sorted(_json_verbs(_build_parser()))
    # must fire: an empty walk would make every assertion below vacuously true -- `assert not []` earns nothing.
    assert len(verbs) >= 10, f"the parser walk looks blind: {verbs}"
    assert "doctor" in verbs and "session list" in verbs

    page = Path(__file__).resolve().parents[1] / "docs" / "compatibility.md"
    text = page.read_text(encoding="utf-8")
    start = text.index(_PROMISE_SECTION)
    section = text[start:text.index("\n## ", start + 1)]

    named = set(re.findall(r"`requivo ([a-z]+(?: [a-z]+)?)`", section))

    missing = sorted(v for v in verbs if v not in named)
    assert not missing, f"these verbs accept `--json` and are not named in the promise table: {missing}"

    # The other direction: a name in the table the parser does not produce promises an output that does not exist.
    stale = sorted(n for n in named if n not in verbs)
    assert not stale, f"the promise table names verbs that do not take `--json`: {stale}"

# ── the parser's shape: which verbs exist at all ─────────────────────────────


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


def test_pc_unknown_command_errors():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["bogus"])


def test_documented_cli_commands_exist():
    # Every documented top-level command must be a real subcommand -- guards doc/CLI drift.
    import argparse

    parser = _build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    documented = {"discover", "answer", "status", "impact", "brief", "prd", "stories", "estimate", "criteria", "epic",
                  "release", "web", "api", "demo", "doctor", "schema", "context", "session", "model", "artifact"}
    missing = documented - set(sub.choices)
    assert not missing, f"documented CLI commands missing from the parser: {sorted(missing)}"

# ── one name for a session reference, across both authoring eras (#248) ──────
# `cli.py`'s journey verbs called the positional `model`; `deterministic/`'s ten called it `session`, so a missing
# argument said "required: model" for a session slug -- unified with no invocation change.

SESSION_TAKING_JOURNEY_VERBS = (
    "answer", "status", "impact", "brief", "prd", "stories", "estimate", "criteria", "epic", "release",
)


def _positionals(parser):
    """verb path -> its positional dests in order, read off the built parser (same reason as `_json_verbs`)."""
    found = {}
    for verb, action in _walk_actions(parser):
        if action.option_strings:
            continue
        found.setdefault(verb, []).append(action.dest)
    return found


def test_every_session_reference_positional_is_spelled_session():
    """The rename is a property of the whole parser, not just the verbs that moved -- a later copycat verb fails too."""
    positionals = _positionals(_build_parser())

    # must fire: an empty walk would make both assertions below vacuously pass.
    assert len(positionals) >= 20, f"the parser walk looks blind: {positionals}"

    for verb in SESSION_TAKING_JOURNEY_VERBS:
        assert positionals.get(verb, [])[:1] == ["session"], f"`{verb}`: first positional is {positionals.get(verb)}"

    offenders = {v: d for v, d in positionals.items() if "model" in d}
    assert offenders == {}, f"a positional still calls a session reference `model`: {offenders}"


def test_the_missing_argument_error_names_a_session_not_a_model():
    """`metavar` could drift from `dest` unseen; uses `brief`, since #541 made `status`'s positional optional."""
    code, out, err = _run_capturing(["brief"], client=None)

    assert code == 2, f"expected argparse's usage error, got {code}: {err!r}"
    assert "required: session" in err, err
    assert "required: model" not in err, err

# `status`/`impact` open a path directly; the other eight resolve a *slug*, so a path was never meaningful (#402).
PATH_ACCEPTING_JOURNEY_VERBS = ("status", "impact")


def test_only_status_and_impact_document_the_saved_model_json_path():
    """The rename moved the name, not the values: #402 made the other eight refuse a path, so help can't claim one."""
    helps = {verb: action.help for verb, action in _walk_actions(_build_parser())
             if not action.option_strings and action.dest == "session"}

    assert len(helps) >= 15, f"the parser walk looks blind: {sorted(helps)}"
    for verb in PATH_ACCEPTING_JOURNEY_VERBS:
        assert "model.json" in (helps.get(verb) or ""), f"`{verb}` stopped documenting model.json: {helps.get(verb)!r}"


def test_the_eight_write_verbs_document_a_bare_slug_only():
    """The other half: a help string saying "or a saved model.json" for a verb that cannot accept one is drift #402."""
    helps = {verb: action.help for verb, action in _walk_actions(_build_parser())
             if not action.option_strings and action.dest == "session"}

    write_verbs = [v for v in SESSION_TAKING_JOURNEY_VERBS if v not in PATH_ACCEPTING_JOURNEY_VERBS]
    assert len(write_verbs) == 8, f"expected eight write verbs, found {write_verbs}"
    for verb in write_verbs:
        help_ = helps.get(verb) or ""
        assert help_ == "a session slug", f"`requivo {verb}` documents {help_!r}, but cannot open a model.json path"


def test_docs_cli_md_names_the_same_two_path_accepting_verbs_as_the_parser():
    """The other half of #402: agreement with the page opened, read from `docs/cli.md` rather than hand-copied."""
    page = Path(__file__).resolve().parents[1] / "docs" / "cli.md"
    text = page.read_text(encoding="utf-8")

    marker = "also accept a path to a saved"
    line = next((ln for ln in text.splitlines() if marker in ln), None)
    assert line is not None, "docs/cli.md no longer says which verbs accept a model.json path"

    documented = tuple(re.findall(r"`([a-z]+)`", line.split(marker)[0]))
    assert documented == PATH_ACCEPTING_JOURNEY_VERBS, f"got {documented}; parser says {PATH_ACCEPTING_JOURNEY_VERBS}"

# ── a global flag is global wherever it is written (#249) ────────────────────
# `--workspace` was declared only on the root parser, so writing it after the verb died with `unrecognized arguments`
# (#249) -- fixed by re-declaring it on every subparser with `default=argparse.SUPPRESS`.


def _verbs_missing_workspace(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """Every verb whose parser lacks `--workspace`, read off the built parser -- a list leaves a subcommand out."""
    missing, subs = [], []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subs.extend((name, sp) for name, sp in action.choices.items())
    if prefix and not any("--workspace" in a.option_strings for a in parser._actions):
        missing.append(prefix)
    for name, sp in subs:
        missing.extend(_verbs_missing_workspace(sp, f"{prefix} {name}".strip()))
    return missing


def test_every_verb_accepts_workspace_after_its_own_name():
    missing = _verbs_missing_workspace(_build_parser())
    assert not missing, f"these verbs do not bind `--workspace`: {sorted(set(missing))}"


def test_workspace_parses_identically_before_and_after_the_command():
    before = _build_parser().parse_args(["--workspace", "/w", "status", "m.json"])
    after = _build_parser().parse_args(["status", "m.json", "--workspace", "/w"])
    assert before.workspace == after.workspace == "/w"
    assert before.session == after.session == "m.json"
    # Two levels down, where the flag has to be on the *leaf* parser to be reachable at all.
    assert _build_parser().parse_args(["session", "list", "--workspace", "/w"]).workspace == "/w"


def test_an_absent_subcommand_workspace_does_not_clobber_the_global_one():
    """Every copy carries `default=argparse.SUPPRESS`, or a copy defaulting to None erases `--workspace` globally."""
    assert _build_parser().parse_args(["--workspace", "/w", "status", "m.json"]).workspace == "/w"
    assert _build_parser().parse_args(["--workspace", "/w", "session", "list"]).workspace == "/w"
    assert _build_parser().parse_args(["--workspace", "/w", "web"]).workspace == "/w"


def test_an_unknown_flag_after_the_command_is_still_refused():
    """The must-fire control: a fix that stopped argparse minding unknown arguments would pass every assertion above."""
    for argv in (["status", "m.json", "--worksapce", "/w"], ["session", "list", "--nonsuch"]):
        with pytest.raises(SystemExit) as e:
            _build_parser().parse_args(argv)
        assert e.value.code == 2


def test_the_workspace_help_no_longer_tells_the_reader_to_place_it_first():
    """The constraint is gone, so the sentence stating it goes too -- prose that outlives the rule it describes."""
    root = _build_parser()
    action = next(a for a in root._actions if "--workspace" in a.option_strings)
    assert "before the command" not in (action.help or ""), "the global --workspace help still says to place it first"
    # Read off the action, never `format_help()` which rewraps to terminal width -- a page search could pass regardless.


def test_workspace_after_the_command_reaches_the_session_store(tmp_path):
    """End to end: `app()` reads the flag position-independently, so the session lands under the named directory."""
    root = tmp_path / "elsewhere"
    root.mkdir()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        app(["session", "init", "A leave approval system", "--slug", "ws-after", "--workspace", str(root)], client=None)
    assert (root / ".requivo" / "sessions" / "ws-after" / "session.json").is_file()

# ── every real flag is mentioned in docs/cli.md (#284, the inverse of #72's direction) ───────
# The inverse of #72: a flag the parser binds that docs/cli.md never mentions is the gap #284 was filed over.


def _real_flags(parser: argparse.ArgumentParser, prefix: str = "", global_flags: frozenset[str] | None = None):
    """Yield (verb path, action) for every argparse action with a real option string (`-h`/`--help` excluded). `global_flags`, computed once from the root parser, exempts a re-declared subparser copy (#249) -- keyed on *root* only, since an ancestor-keyed version let an intermediate group exempt an unrelated
    leaf flag (pinned by `test_the_inherited_flag_exemption_skips_only_a_re_declared_global_flag`). Not `help=argparse.SUPPRESS`: a hidden flag needs documenting most."""
    if global_flags is None:
        global_flags = frozenset(opt for a in parser._actions for opt in a.option_strings)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _real_flags(sub, f"{prefix} {name}".strip(), global_flags)
        elif set(action.option_strings) - {"-h", "--help"}:
            # `prefix` is empty only at root, where the flag is being documented, not a re-declared copy.
            if prefix and (set(action.option_strings) & global_flags):
                continue
            yield (prefix, action)


def test_the_inherited_flag_exemption_skips_only_a_re_declared_global_flag():
    """Must-fire control on `_real_flags`' exemption. Two levels deep: a one-level fixture can't tell root-bound from ancestor-bound, the earlier bug; `session`/`model`/`artifact` groups are this shape."""
    p = argparse.ArgumentParser(prog="x")
    p.add_argument("--workspace")
    sub = p.add_subparsers()
    sp = sub.add_parser("go")
    sp.add_argument("--workspace", default=argparse.SUPPRESS)   # the position-independence copy
    sp.add_argument("--only-here")                              # genuinely this verb's own

    grp = sub.add_parser("grp")
    grp.add_argument("--dry-run")                               # an intermediate group's own flag
    leaf = grp.add_subparsers().add_parser("leaf")
    leaf.add_argument("--dry-run")                              # unrelated, and the leaf's own
    leaf.add_argument("--workspace", default=argparse.SUPPRESS)

    yielded = {(verb, tuple(a.option_strings)) for verb, a in _real_flags(p)}
    assert ("go", ("--only-here",)) in yielded, "a verb's own flag must still be demanded"
    assert ("go", ("--workspace",)) not in yielded, "the re-declared global copy is not a new flag"
    assert ("", ("--workspace",)) in yielded, "and the global flag is still demanded, once, at root"
    assert ("grp", ("--dry-run",)) in yielded, "an intermediate group's own flag is still demanded"
    assert ("grp leaf", ("--dry-run",)) in yielded, "a same-named parent flag must not hide a leaf flag"
    assert ("grp leaf", ("--workspace",)) not in yielded


def test_every_workspace_copy_carries_the_same_help_text():
    """`--workspace` has 30-odd declarations; `web`'s predates the rest, so it alone drifted (#249)."""
    helps = {(a.help or "") for _verb, a in _real_flags(_build_parser(), global_flags=frozenset())
             if "--workspace" in a.option_strings}
    assert len(helps) == 1, f"the --workspace copies describe the flag {len(helps)} different ways: {helps}"
    assert "before or after the command" in helps.pop()


def test_the_only_flags_the_root_parser_binds_are_the_two_global_ones():
    """What bounds the exemption: it can only skip what the root parser declares; a third root flag fails this test."""
    root = _build_parser()
    bound = {opt for a in root._actions for opt in a.option_strings} - {"-h", "--help"}
    assert bound == {"--version", "--workspace"}


def test_every_real_flag_is_documented_in_the_cli_reference():
    """Read off the built parser, not a hand-maintained list (#284). Checks the *long* form on the *same physical line* as `requivo <verb>`, since a page search can't tell "here" from "wrong command"."""
    page = Path(__file__).resolve().parents[1] / "docs" / "cli.md"
    lines = page.read_text(encoding="utf-8").splitlines()

    checked, missing = 0, []
    for verb, action in _real_flags(_build_parser()):
        long_forms = [opt for opt in action.option_strings if opt.startswith("--")]
        candidates = long_forms or list(action.option_strings)
        checked += 1
        marker = f"requivo {verb}".strip() if verb else "requivo"
        documented = any(marker in line and any(opt in line for opt in candidates) for line in lines)
        if not documented:
            missing.append((verb or "(top level)", action.option_strings))

    # must fire: an empty walk would make the assertion below vacuously true.
    assert checked >= 30, f"the parser walk looks blind: only {checked} flag(s) found"
    assert not missing, f"flags real but undocumented in docs/cli.md under their own `requivo <verb>`: {missing}"
