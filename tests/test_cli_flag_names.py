"""The CLI flag names, and the error channel one of them was silently switching.

Two issues ship here as one unit -- both are about what a flag is *called* and what that name
implies to the code reading it.

#83 -- `epic --json` meant "also write a second export file", not "emit JSON on stdout" as it does
on every other verb. The name was half the problem. The other half is that `cli.app()` reads
`want_json = getattr(args, "json", False)` *generically* and uses that same attribute to switch
failures from prose-on-stderr to a structured envelope-on-stdout. So `epic --json` also changed how
failures were reported, while its two actual siblings -- `--github` and `--gitlab`, same shape,
same effect -- did not. Renaming the flag to `--export-json` removes the `json` attribute from
`epic`'s namespace, that `getattr` falls through to `False`, and all three export flags report a
failure the same way. The rename is the visible half; this file asserts the half that mattered.

#85 -- the context-card selector was spelled `--context` on two verbs and `--cards` on a third.
Both spellings now work everywhere; the dest is unchanged on each verb, so no handler moved.

#72 added the parser-shape tests at the foot of the file: which verbs the parser binds, what it does
with a verb it does not know, and that every command the docs promise is really there. Same subject
one level up -- what the CLI *is called*, read off the built parser rather than off the source.
"""
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
    """A client whose create() raises a transport error, which the provider wraps as an
    EngineError -- a RequivoError, and therefore the exact exception `app()` routes through
    `want_json`. The failure has to be the *same* one on all three flags for the comparison to
    mean anything, so it is raised unconditionally."""

    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise anthropic.APIConnectionError(
            message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))


class _CannedClient:
    """A client that answers with one canned JSON reply -- enough for `epic` to reach its writers.
    Local on purpose: `tests/_fakes.py` carries the shared `FakeClient`, and this one is shaped for a
    single verb's failure path rather than for reuse. Until #72 this note read "the equivalent helper
    in test_engine.py is that module's fixture, and a test that reaches across modules for it breaks
    when the other module reorganises" -- that module was then split, which is the reorganisation the
    note was worried about, so the reason is restated against something that will not move."""

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
    """Run `app()` and return (exit_code, stdout, stderr). `app()` raises SystemExit on every
    clean failure, so the code is part of the observation, not an accident of the harness."""
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
    """The regression this issue is actually about. Before the rename, `--json` printed a JSON
    envelope on stdout and exited 1; `--github` and `--gitlab` printed prose on stderr and exited
    1. Same failure, two channels, chosen by a flag whose documented job was to write a file."""
    slug = _session_with_a_model("flagtest-epic-errors")

    results = {flag: _run_capturing(["epic", slug, flag], client=_RaisingClient())
               for flag in EXPORT_FLAGS}

    # Positive half first: the failure really happened and really said something. Without this the
    # identity assertion below would pass just as happily on three empty strings -- which is what a
    # harness that never reached the provider would produce.
    for flag, (code, out, err) in results.items():
        assert code == 1, f"{flag}: expected the clean-failure exit, got {code}"
        assert "Anthropic API unavailable" in err, f"{flag}: prose failure missing from stderr"
        assert out.strip() == "", f"{flag}: nothing should reach stdout, got {out[:120]!r}"

    # And now the identity itself.
    assert len(set(results.values())) == 1, (
        "the three export flags render the same provider failure differently: "
        + json.dumps({f: {"code": c, "stdout": o[:80], "stderr": e[:80]}
                      for f, (c, o, e) in results.items()}, indent=2))


def test_the_structured_envelope_is_still_reachable_on_a_verb_that_keeps_json():
    """The control for the assertion above. `epic` must NOT emit the envelope -- but the harness
    has to be able to see one when it is emitted, or `out.strip() == ""` is measuring nothing. A
    verb that keeps a real `--json` still routes a RequivoError through `e.to_dict()` on stdout."""
    code, out, err = _run_capturing(["session", "show", "no-such-session", "--json"], client=None)

    assert code == 1
    envelope = json.loads(out)                    # it really is JSON, on stdout
    assert envelope["code"] and envelope["message"]
    assert err.strip() == ""                      # and the prose channel stayed quiet


def test_epic_no_longer_accepts_the_old_json_spelling():
    """The break, asserted rather than assumed. `--json` is not a prefix of `--export-json`, so
    argparse rejects it outright (exit 2) instead of quietly meaning something new."""
    slug = _session_with_a_model("flagtest-epic-old-flag")
    code, _out, err = _run_capturing(["epic", slug, "--json"], client=_RaisingClient())

    assert code == 2                              # argparse's usage error, not a run that happened
    assert "--json" in err


def test_epic_export_json_still_writes_the_neutral_export():
    """The rename moved the name, not the behaviour: `--export-json` writes the same file that
    `--json` used to."""
    slug = _session_with_a_model("flagtest-epic-writes")
    epic = {"title": "X", "issues": [{"id": "I-1", "title": "Build the request form"}]}
    with contextlib.redirect_stdout(io.StringIO()):
        app(["epic", slug, "--export-json"], client=_CannedClient(json.dumps(epic)))

    written = store.canonical_dir(slug).joinpath("artifacts", "epic.json")
    assert json.loads(written.read_text(encoding="utf-8"))


def test_every_other_verb_that_declares_json_still_binds_it_to_the_json_dest():
    """The generic `getattr(args, "json", False)` in `app()` is the thing the rename must not
    disturb. Walk the parser: every verb that offers a `--json` option string must still land it on
    the `json` dest, and `epic` must offer none. A rename that moved a dest by accident -- or a
    later verb that spells its flag `--export-json` and expects an envelope -- fails here."""
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
    # The count is the "thirteen other verbs" from the issue, asserted as a floor rather than an
    # exact number so adding a verb does not fail this test for the wrong reason.
    assert len(verbs_with_json) >= 13, verbs_with_json


CARD_SELECTOR_VERBS = (
    (("discover", "a request"), "context"),
    (("session", "init", "a request"), "context"),
    (("context",), "cards"),
)


@pytest.mark.parametrize(("argv_head", "dest"), CARD_SELECTOR_VERBS)
@pytest.mark.parametrize("spelling", ["--context", "--cards"])
def test_both_spellings_of_the_card_selector_reach_the_same_dest(argv_head, dest, spelling):
    """`--context` is the documented primary; `--cards` is a permanent alias. Both are option
    strings on one argparse action, so they cannot drift apart -- and the dest is unchanged on
    each verb, so no handler had to move."""
    args = _build_parser().parse_args([*argv_head, spelling, "b2b-platform"])
    assert getattr(args, dest) == "b2b-platform"


def test_the_card_selector_is_one_action_not_two():
    """The alias must be a second option string on the *same* action, never a second argument.
    Two arguments would give the last one on the command line the win and silently drop the other,
    which is the failure mode this test exists to make impossible."""
    seen = {}
    for verb, action in _walk_actions(_build_parser()):
        if {"--context", "--cards"}.intersection(action.option_strings):
            seen.setdefault(verb, []).append(sorted(action.option_strings))

    for verb, actions in seen.items():
        assert actions == [["--cards", "--context"]], (
            f"{verb}: --context/--cards must be two option strings on one action, got {actions}")
    assert set(seen) == {"discover", "session init", "context", "session rescope"}, seen


def test_the_context_verb_prints_the_same_cards_under_either_spelling():
    """End-to-end on the one card-selecting verb that needs no provider, so the alias is proved
    against the real handler and not only against the parser."""
    def run(spelling):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            app(["context", spelling, "b2b-platform"], client=None)
        return buf.getvalue()

    printed = run("--cards")
    assert printed.strip()                      # it really printed a card, not an empty selection
    assert printed == run("--context")


# ── the `--json` perimeter (#102) ────────────────────────────────────────────
#
# `docs/compatibility.md` bounded this surface by naming six of fourteen outputs and justifying the
# six as "what the Claude Code plugin drives". That was wrong in both directions by three entries
# each, nothing tested it, and eight outputs sat in neither column — one of which #84 had already
# made a breaking change to. The perimeter is now the whole set, which is what makes this guard
# trivial: a subset would need the test to encode the boundary, and the boundary is what drifts.

_PROMISE_SECTION = "## The `--json` outputs are public"


def _json_verbs(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """Every verb path that accepts `--json`, read off the built parser.

    From the parser and not from a grep of the source: a grep validates the reader's regex, and the
    thing being promised is what the command actually accepts.
    """
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
    """The page promises every `--json` output. This is what stops that being a sentence.

    Both directions are checked. A new verb with `--json` and no row is the case #84 walked into —
    a public output nobody promised, broken before anyone noticed there was no promise to break. A
    row for a verb that no longer takes `--json` is the mirror: a promise about something that
    cannot be observed, which reads as coverage this does not have.
    """
    verbs = sorted(_json_verbs(_build_parser()))
    # must fire: the walk really found the surface. An empty list would make every assertion below
    # vacuously true — `assert not []` is an all-clear nobody earned.
    assert len(verbs) >= 10, f"the parser walk looks blind: {verbs}"
    assert "doctor" in verbs and "session list" in verbs

    page = Path(__file__).resolve().parents[1] / "docs" / "compatibility.md"
    text = page.read_text(encoding="utf-8")
    start = text.index(_PROMISE_SECTION)
    section = text[start:text.index("\n## ", start + 1)]

    named = set(re.findall(r"`requivo ([a-z]+(?: [a-z]+)?)`", section))

    missing = sorted(v for v in verbs if v not in named)
    assert not missing, (
        "these verbs accept `--json` and are not named in the promise table, so their output is "
        f"public by accident rather than by decision: {missing}")

    # The other direction, and it is not symmetric hand-waving: a name in the table that the parser
    # does not produce is a promise about an output that does not exist.
    stale = sorted(n for n in named if n not in verbs)
    assert not stale, f"the promise table names verbs that do not take `--json`: {stale}"


# ── the parser's shape: which verbs exist at all ─────────────────────────────


def test_pc_parser_binds_every_subcommand():
    cases = {
        ("discover", "req"): "_cmd_discover",
        ("status", "m.json"): "_cmd_status",
        ("impact", "m.json"): "_cmd_impact",
        ("brief", "m.json"): "_cmd_brief",
        ("prd", "m.json"): "_cmd_prd",
        ("stories", "m.json"): "_cmd_stories",
        ("estimate", "m.json"): "_cmd_estimate",
        ("criteria", "m.json"): "_cmd_criteria",
        ("epic", "m.json"): "_cmd_epic",
        ("release", "m.json"): "_cmd_release",
    }
    for argv, fname in cases.items():
        assert _build_parser().parse_args(list(argv)).func.__name__ == fname
    assert _build_parser().parse_args(["epic", "m", "--github", "--gitlab"]).github
    assert _build_parser().parse_args(["release", "m", "v1.0"]).version == "v1.0"


def test_pc_unknown_command_errors():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["bogus"])


def test_documented_cli_commands_exist():
    # Guard doc/CLI drift: every top-level command the README and docs/cli.md promise must be a real
    # subcommand, so a rename or removal can't leave the docs pointing at a command that doesn't exist.
    import argparse

    parser = _build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    documented = {"discover", "answer", "status", "impact", "brief", "prd", "stories", "estimate",
                  "criteria", "epic", "release", "web", "api", "demo", "doctor", "schema", "context",
                  "session", "model", "artifact"}
    missing = documented - set(sub.choices)
    assert not missing, f"documented CLI commands missing from the parser: {sorted(missing)}"


# ── one name for a session reference, across both authoring eras (#248) ──────
#
# `cli.py`'s journey verbs declared the positional as `model` while all ten verbs under
# `deterministic/` declared the identical concept as `session`. So `requivo status` with no argument
# answered "the following arguments are required: model" about a thing the user supplies as a
# session slug -- engine vocabulary, and doubly confusing beside the `model` verb group, where
# `requivo model <model>` was the rendered usage. A dest is internal and a positional is passed by
# position, so unifying the two eras changed no invocation.

SESSION_TAKING_JOURNEY_VERBS = (
    "answer", "status", "impact", "brief", "prd", "stories", "estimate", "criteria", "epic",
    "release",
)


def _positionals(parser):
    """verb path -> its positional dests in declaration order, read off the built parser.

    Off the parser and not off a grep of the source, for `test_every_json_verb_is_inside_the_promise`'s
    reason one field along: a grep validates the reader's regex, and what is being asserted is what
    the command actually binds.
    """
    found = {}
    for verb, action in _walk_actions(parser):
        if action.option_strings:
            continue
        found.setdefault(verb, []).append(action.dest)
    return found


def test_every_session_reference_positional_is_spelled_session():
    """The rename, asserted as a property of the whole parser rather than of the ten verbs that
    moved -- so a verb added later that copies the era this closed fails here, under its own name."""
    positionals = _positionals(_build_parser())

    # must fire: the walk really found the surface. Without this both assertions below pass on an
    # empty dict, which is exactly what a walk aimed at the wrong attribute would produce.
    assert len(positionals) >= 20, f"the parser walk looks blind: {positionals}"

    for verb in SESSION_TAKING_JOURNEY_VERBS:
        assert positionals.get(verb, [])[:1] == ["session"], (
            f"`requivo {verb}` takes a session reference; its first positional is "
            f"{positionals.get(verb)}")

    offenders = {v: d for v, d in positionals.items() if "model" in d}
    assert offenders == {}, f"a positional still calls a session reference `model`: {offenders}"


def test_the_missing_argument_error_names_a_session_not_a_model():
    """The user-visible half, and the sentence the issue was filed on. The parser test above would
    stay green if `dest` moved and `metavar` did not, and the metavar is what a person reads."""
    code, out, err = _run_capturing(["status"], client=None)

    assert code == 2, f"expected argparse's usage error, got {code}: {err!r}"
    assert "required: session" in err, err
    assert "required: model" not in err, err


# `status` and `impact` genuinely open a path they are handed -- `cli.py`'s `_resolve_ref` reads the
# file's own bytes directly, no session lookup involved. The other eight resolve a *slug* and
# read/write the store's own copy (`ArtifactService.save` refuses anything that is not
# `has_meta(slug)`), so a model.json path was never a meaningful input for them (#402).
PATH_ACCEPTING_JOURNEY_VERBS = ("status", "impact")


def test_only_status_and_impact_document_the_saved_model_json_path():
    """The rename moved the name, not the accepted value set -- for the two verbs that actually have
    one. `resolve_slug`'s wrong-cause failure (#402) was closed by making the other eight refuse a
    path outright rather than mine a slug out of it, so their help must not go on claiming a path
    works: a help string is a promise the parser has to keep, not a decoration."""
    helps = {verb: action.help for verb, action in _walk_actions(_build_parser())
             if not action.option_strings and action.dest == "session"}

    assert len(helps) >= 15, f"the parser walk looks blind: {sorted(helps)}"
    for verb in PATH_ACCEPTING_JOURNEY_VERBS:
        assert "model.json" in (helps.get(verb) or ""), (
            f"`requivo {verb}` stopped documenting the saved model.json path: {helps.get(verb)!r}")


def test_the_eight_write_verbs_document_a_bare_slug_only():
    """The other half of the same guard, and the one that would have caught #402 outright: a help
    string that still says "or a path to a saved model.json" for a verb that cannot accept one is
    exactly the drift this issue was filed over."""
    helps = {verb: action.help for verb, action in _walk_actions(_build_parser())
             if not action.option_strings and action.dest == "session"}

    write_verbs = [v for v in SESSION_TAKING_JOURNEY_VERBS if v not in PATH_ACCEPTING_JOURNEY_VERBS]
    assert len(write_verbs) == 8, f"expected eight write verbs, found {write_verbs}"
    for verb in write_verbs:
        help_ = helps.get(verb) or ""
        assert help_ == "a session slug", (
            f"`requivo {verb}` documents {help_!r} -- it cannot open a model.json path, so its help "
            "must not offer one")


def test_docs_cli_md_names_the_same_two_path_accepting_verbs_as_the_parser():
    """The other half of #402's fourth acceptance criterion: not only must the help be internally
    consistent (the two tests above), the page a reader actually opens has to say the same thing.
    `docs/cli.md`'s opening paragraph already named the narrower truth before the parser did --
    read here rather than hand-copied, so a verb moved between the two sets on either side shows up
    as a real disagreement instead of two hand-maintained lists that happen to agree today."""
    page = Path(__file__).resolve().parents[1] / "docs" / "cli.md"
    text = page.read_text(encoding="utf-8")

    marker = "also accept a path to a saved"
    line = next((ln for ln in text.splitlines() if marker in ln), None)
    assert line is not None, "docs/cli.md no longer says which verbs accept a model.json path"

    documented = tuple(re.findall(r"`([a-z]+)`", line.split(marker)[0]))
    assert documented == PATH_ACCEPTING_JOURNEY_VERBS, (
        f"docs/cli.md names {documented} as path-accepting; the parser says "
        f"{PATH_ACCEPTING_JOURNEY_VERBS}")


# ── a global flag is global wherever it is written (#249) ────────────────────
#
# `--workspace` was declared on the root parser alone, so `requivo status <slug> --workspace DIR`
# died with argparse's bare `unrecognized arguments: --workspace DIR` at exit 2 -- a message that
# names a flag this CLI absolutely does know as unknown, and sends the reader looking for a typo.
# The constraint ("Place before the command") lived only in `--help` text, which the user who
# triggered it is the one user not reading. `web` already carried the working pattern: the same
# option re-declared on the subparser with `default=argparse.SUPPRESS`, so an absent subcommand
# copy leaves the global value alone. It is now on every subparser, at every depth.


def _verbs_missing_workspace(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """Every verb path whose parser does not bind `--workspace`, read off the built parser rather
    than off a list -- a list is what leaves the next subcommand out."""
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
    assert not missing, (
        "these verbs do not bind `--workspace`, so writing it after the command still dies with "
        f"argparse's `unrecognized arguments`: {sorted(set(missing))}")


def test_workspace_parses_identically_before_and_after_the_command():
    before = _build_parser().parse_args(["--workspace", "/w", "status", "m.json"])
    after = _build_parser().parse_args(["status", "m.json", "--workspace", "/w"])
    assert before.workspace == after.workspace == "/w"
    assert before.session == after.session == "m.json"
    # Two levels down, where the flag has to be on the *leaf* parser to be reachable at all.
    assert _build_parser().parse_args(["session", "list", "--workspace", "/w"]).workspace == "/w"


def test_an_absent_subcommand_workspace_does_not_clobber_the_global_one():
    """The reason every copy carries `default=argparse.SUPPRESS`, and the half a naive fix breaks:
    argparse copies the subparser's namespace over the root's, so a copy defaulting to None would
    silently erase `requivo --workspace DIR <command>` for every verb at once."""
    assert _build_parser().parse_args(["--workspace", "/w", "status", "m.json"]).workspace == "/w"
    assert _build_parser().parse_args(["--workspace", "/w", "session", "list"]).workspace == "/w"
    assert _build_parser().parse_args(["--workspace", "/w", "web"]).workspace == "/w"


def test_an_unknown_flag_after_the_command_is_still_refused():
    """The must-fire control. A fix that simply stopped argparse minding unknown arguments would
    pass every assertion above and lose the refusal that makes a typo visible."""
    for argv in (["status", "m.json", "--worksapce", "/w"], ["session", "list", "--nonsuch"]):
        with pytest.raises(SystemExit) as e:
            _build_parser().parse_args(argv)
        assert e.value.code == 2


def test_the_workspace_help_no_longer_tells_the_reader_to_place_it_first():
    """The constraint is gone, so the sentence stating it has to go with it -- prose that outlives
    the rule it describes is the thing this issue was actually about."""
    root = _build_parser()
    action = next(a for a in root._actions if "--workspace" in a.option_strings)
    assert "before the command" not in (action.help or ""), (
        "the global --workspace help still tells the reader to place it before the command")
    # Read off the action, never off `format_help()`: argparse rewraps help text to the terminal
    # width, so the sentence under test can be split across two lines and a substring search over
    # the rendered page passes while the constraint is still being stated. Verified: it does.


def test_workspace_after_the_command_reaches_the_session_store(tmp_path):
    """End to end, not only through the parser: `app()` reads the flag position-independently
    (`getattr(args, "workspace", None)`), so the session has to land under the directory named
    after the verb."""
    root = tmp_path / "elsewhere"
    root.mkdir()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        app(["session", "init", "A leave approval system", "--slug", "ws-after",
             "--workspace", str(root)], client=None)
    assert (root / ".requivo" / "sessions" / "ws-after" / "session.json").is_file()


# ── every real flag is mentioned in docs/cli.md (#284, the inverse of #72's direction) ───────
#
# #72 (above) guards that a command docs/cli.md promises really exists in the parser. This is the
# other direction: a flag the parser actually binds and docs/cli.md never mentions is a flag a user
# hunting for it will not find -- the exact gap #284 was filed over (`session init --slug` and
# `--provider` existed and were absent from the reference).


def _real_flags(parser: argparse.ArgumentParser, prefix: str = "",
                global_flags: frozenset[str] | None = None):
    """Yield (verb path, action) for every argparse action that carries a real option string --
    `-h`/`--help` excluded, since every verb has it and documenting it would be noise.

    `global_flags` is the option strings the **root** parser binds, computed once on the outermost
    call and passed down unchanged. Below the root, an action re-declaring one of them is skipped
    rather than yielded (#249): a global flag re-declared on every subparser so that its position
    stops mattering is still one flag, documented once on its own top-level row -- which this same
    walk checks when the recursion is at the root. Demanding a per-verb row for it would ask
    docs/cli.md for thirty-three lines that all say the same sentence, which is not what
    "documented" means to a reader.

    **Root, not any ancestor, and the difference is the whole safety of the exemption.** The first
    version of this accumulated `inherited | own` at every level, so a flag bound on an intermediate
    group parser (`session`, `model`, `artifact`) would have silently exempted an *unrelated* flag
    of the same name on that group's own leaves -- the "ship silently undocumented" class this guard
    exists to catch, reintroduced by the fix for a different one. Found in review of #249; today no
    group parser binds an option of its own, so nothing was actually hidden, which is exactly what
    makes it the kind of gap nobody notices later. Pinned by
    `test_the_inherited_flag_exemption_skips_only_a_re_declared_global_flag`, whose fixture is two
    levels deep for that reason.

    Deliberately keyed on *bound at the root*, not on `help=argparse.SUPPRESS`: hiding a flag from
    `--help` would then be enough to escape this guard, and a hidden flag is the one most in need of
    being written down.
    """
    if global_flags is None:
        global_flags = frozenset(opt for a in parser._actions for opt in a.option_strings)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _real_flags(sub, f"{prefix} {name}".strip(), global_flags)
        elif set(action.option_strings) - {"-h", "--help"}:
            # `prefix` is empty only at the root, where the flag is the one being documented rather
            # than a copy of it -- so the root's own `--workspace` is still demanded.
            if prefix and (set(action.option_strings) & global_flags):
                continue
            yield (prefix, action)


def test_the_inherited_flag_exemption_skips_only_a_re_declared_global_flag():
    """The must-fire control on `_real_flags`' one exemption. Skipping a re-declared global flag is
    only safe while it cannot reach a flag a verb genuinely owns -- and a walk that quietly stopped
    yielding would make the guard below pass over an undocumented CLI.

    **Two levels deep on purpose.** A one-level fixture cannot tell "bound at the root" from "bound
    at any ancestor", and the first version of `_real_flags` accumulated the latter: a flag on the
    `grp` parser would have exempted an unrelated same-named flag on `grp leaf`. The `grp --dry-run`
    / `grp leaf --dry-run` pair below is that case, and it is the assertion the one-level fixture
    could not make. `requivo`'s own `session`, `model` and `artifact` groups are exactly this shape.
    """
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
    assert ("grp leaf", ("--dry-run",)) in yielded, (
        "a leaf flag was hidden by a same-named flag on its parent -- the exemption is keyed on any "
        "ancestor rather than on the root, which is the gap that lets a real flag ship undocumented")
    assert ("grp leaf", ("--workspace",)) not in yielded


def test_every_workspace_copy_carries_the_same_help_text():
    """`--workspace` is one flag with thirty-odd declarations, and `cli.py` says so in as many words
    ("One string, bound to every copy of the flag, so the global one and the per-verb ones cannot
    describe two different things"). `web`'s copy predates the walk and is skipped by it -- the walk
    only *adds* where the option is absent, because adding twice is an argparse conflict -- so it
    was the one copy free to drift, and it had (#249, found in review). Asserted rather than argued,
    because the next hand-written copy will be free in exactly the same way."""
    helps = {(a.help or "") for _verb, a in _real_flags(_build_parser(), global_flags=frozenset())
             if "--workspace" in a.option_strings}
    assert len(helps) == 1, f"the --workspace copies describe the flag {len(helps)} different ways: {helps}"
    assert "before or after the command" in helps.pop()


def test_the_only_flags_the_root_parser_binds_are_the_two_global_ones():
    """What bounds the exemption above: it can only ever skip something the root parser declares.
    If a third flag ever moves onto the root, this fails and the exemption gets re-argued rather
    than silently widening to cover it."""
    root = _build_parser()
    bound = {opt for a in root._actions for opt in a.option_strings} - {"-h", "--help"}
    assert bound == {"--version", "--workspace"}


def test_every_real_flag_is_documented_in_the_cli_reference():
    """Read off the built parser, not off a hand-maintained list -- a hand-maintained list is what
    let `--provider` ship silently undocumented in the first place. Checked against the *long*
    option string only (`--output`, never `-o`): a short flag is one or two characters and is
    already, coincidentally, a substring of ordinary prose almost everywhere, so checking it would
    make the assertion pass whether or not anyone had actually written the flag down.

    Checked *on the same line as the flag's own verb*, not merely present anywhere on the page. A
    page-wide substring search cannot tell "documented under this command" from "documented under
    the wrong one" -- a flag moved to a neighbouring table row would still read as present. Every
    row in docs/cli.md's reference tables is one physical line (a markdown table forbids anything
    else), so a real entry always carries the verb's own `requivo <verb>` invocation and the flag
    on the same line; this is what lets the two be checked together instead of merely both existing
    somewhere in the file. Found in review of #284's own first version of this test."""
    page = Path(__file__).resolve().parents[1] / "docs" / "cli.md"
    lines = page.read_text(encoding="utf-8").splitlines()

    checked, missing = 0, []
    for verb, action in _real_flags(_build_parser()):
        long_forms = [opt for opt in action.option_strings if opt.startswith("--")]
        candidates = long_forms or list(action.option_strings)
        checked += 1
        marker = f"requivo {verb}".strip() if verb else "requivo"
        documented = any(marker in line and any(opt in line for opt in candidates)
                          for line in lines)
        if not documented:
            missing.append((verb or "(top level)", action.option_strings))

    # must fire: an empty walk would make the assertion below vacuously true.
    assert checked >= 30, f"the parser walk looks blind: only {checked} flag(s) found"
    assert not missing, (
        "these flags are real (the parser binds them) and no line of docs/cli.md names both the "
        "flag and its own `requivo <verb>` invocation -- so the flag is either undocumented, or "
        f"documented under a different command than the one that binds it: {missing}"
    )
