"""`requivo --help` is the first screen, and it used to read as an implementation order (#244).

argparse renders subcommands in registration order, and `_build_parser` registered the deterministic
package first -- so the six plumbing entries (doctor, schema, context, session, model, artifact) led,
and the two verbs a new user needs, `demo` and `discover`, sat seventh and eighth (#244). #546 went
further: registering the journey verbs first was not enough, because it put `discover` on the first
screen at all -- the verb the three-journey-verb decision
(docs/decisions/0018-three-journey-verbs.md) moved to "for scripts and integrations", one tier below
`run`. `_JourneyHelpFormatter` (cli.py) now renders three named groups instead of a flat,
registration-order list; registration order itself is untouched (`_subcommands()` below still reads
it) and stays the axis CLAUDE.md's tree entry for `cli.py` documents.

The marker is checked in **both directions**, and the negative half is the one that matters. A test
that only asserted "every API verb is marked" is satisfied by marking all of them, which is the same
as marking none.

The expected set is **derived from the provider's own operation table**, not written out here. A
list in a test is a second copy of a decision, and the copy is what goes stale -- a generator added
to `_OP_PROMPTS` would arrive unmarked and this file would still be green. `_OP_PROMPTS` minus
`analyze` is the seven generators; `analyze` itself backs two verbs, `discover` and `answer`, which
is the only part stated by hand because it is a fact about the CLI rather than about the provider.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from requivo.cli import _HELP_GROUP_PLUMBING, _HELP_GROUP_SCRIPTS, _HELP_GROUP_START, _build_parser
from requivo.providers.anthropic.generators import _OP_PROMPTS

# The verbs that make a paid call. `analyze` is the provider operation behind `discover`, `answer`
# and `run` (#540: a fresh request or an existing session's slug both reach it), so it expands to
# three; every other operation is one verb of the same name. `docs` (#544) is a fourth hand-added
# entry for the same reason: it is a loop over the seven generators, not an operation of its own.
API_VERBS = (set(_OP_PROMPTS) - {"analyze"}) | {"discover", "answer", "run", "docs"}

MARKER = "(API)"


def _subcommands() -> list:
    """(name, help) in registration order -- no longer the order `--help` renders them in (#546),
    but still the order `CLAUDE.md`'s tree entry for `cli.py` documents and the order the plumbing
    axis is defined against."""
    for action in _build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return [(c.dest, c.help or "") for c in action._choices_actions]
    raise AssertionError("_build_parser() registers no subcommands")


def test_start_here_leads_the_rendered_help():
    """The acceptance bar from #546, checked against what `requivo --help` actually prints rather
    than against registration order: the first names a reader meets are `demo` and `run`, and
    `discover` -- the old entry point -- is not among them."""
    text = _build_parser().format_help()
    start = text.index("Start here:")
    scripts = text.index("For scripts and integrations")
    plumbing = text.index("Plumbing:")
    assert start < scripts < plumbing, "the three groups render out of order"

    start_section = text[start:scripts]
    assert re.search(r"^\s*demo\s", start_section, re.MULTILINE)
    assert re.search(r"^\s*run\s", start_section, re.MULTILINE)
    # A substring check would also match "discovers" inside `run`'s own help text -- the row start
    # is what a reader actually sees as a verb name.
    assert not re.search(r"^\s*discover\s", start_section, re.MULTILINE), (
        "discover leaked onto the first screen")


def test_every_registered_verb_appears_in_exactly_one_help_group():
    """The completeness half of #546's acceptance criterion ("every existing verb appears exactly
    once"), checked directly against the parser rather than only against the runtime guard
    `_JourneyHelpFormatter._format_grouped_commands` raises on the same condition -- a test failure
    here is a normal `pytest` run going red; the formatter's own check only fires when someone asks
    for `--help`."""
    registered = {name for name, _ in _subcommands()}
    groups = [_HELP_GROUP_START, _HELP_GROUP_SCRIPTS, _HELP_GROUP_PLUMBING]

    seen: dict[str, list[str]] = {}
    for label, group in zip(("start", "scripts", "plumbing"), groups):
        for name in group:
            seen.setdefault(name, []).append(label)

    duplicated = {name: labels for name, labels in seen.items() if len(labels) > 1}
    assert duplicated == {}, f"a verb is in more than one --help group: {duplicated}"
    assert set(seen) == registered, (
        f"missing from every group: {sorted(registered - set(seen))}; "
        f"grouped but not registered: {sorted(set(seen) - registered)}")


def test_every_verb_help_is_byte_identical_to_before_546():
    """The other acceptance criterion: `requivo <verb> --help` unchanged for every top-level verb.
    The fixture is `format_help()` captured from each verb's own subparser on this same branch,
    before `_build_parser`'s `formatter_class` moved from `RawDescriptionHelpFormatter` to
    `_JourneyHelpFormatter` -- a snapshot rather than a structural check because the mechanism
    (`add_parser` does not inherit `formatter_class` from its parent) makes an exact match free to
    keep passing, and a byte diff is the sharpest signal if that mechanism ever stops holding."""
    fixture = Path(__file__).parent / "fixtures" / "cli_help" / "pre_546_verb_help.json"
    before = json.loads(fixture.read_text(encoding="utf-8"))

    root = _build_parser()
    sub_action = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    after = {name: sp.format_help() for name, sp in sub_action.choices.items()}

    assert set(before) == set(after), (
        f"verb set changed: missing={sorted(set(before) - set(after))} "
        f"added={sorted(set(after) - set(before))}")
    changed = sorted(name for name in before if before[name] != after[name])
    assert changed == [], f"`requivo <verb> --help` changed for: {changed}"


def test_the_plumbing_verbs_come_after_the_journey_verbs_in_registration_order():
    """Registration order, not rendered order (#546 separated the two) -- the axis the old help
    actually failed: source should still read demo/run/discover/answer/status/brief before
    doctor/schema/context/session/model/artifact, even though `--help` groups them differently now."""
    order = [name for name, _ in _subcommands()]
    plumbing = {"doctor", "schema", "context", "session", "model", "artifact"}
    journey = ["run", "discover", "answer", "status", "brief"]
    first_plumbing = min(order.index(name) for name in plumbing)
    assert all(order.index(v) < first_plumbing for v in journey), order


def test_every_paid_verb_is_marked_and_no_free_verb_is():
    """Both directions. The negative half is what makes the marker mean something -- marking every
    verb would satisfy the positive half and tell a reader nothing. Read off each verb's own `help`
    text (`_subcommands()`), which `--help` still shows in full for the "Start here" group and which
    stays authored correctly for the other two even though their rendered row is now a bare name
    (#546) -- the data is still there for a reader who runs `requivo --help` before it grows a
    fourth tier, or one who reads `docs/cli.md`."""
    helps = dict(_subcommands())
    marked = {name for name, text in helps.items() if MARKER in text}
    assert marked == API_VERBS, (
        f"marked but free: {sorted(marked - API_VERBS)}; "
        f"paid but unmarked: {sorted(API_VERBS - marked)}")


def test_every_verb_that_can_be_run_offline_says_so_or_says_nothing():
    """`status` and `impact` are the two a user is most likely to hesitate over -- they take a slug
    like `brief` does and cost nothing. Absence of the marker is the claim; this pins that no free
    verb accidentally acquires the word."""
    helps = dict(_subcommands())
    for verb in ("status", "impact", "demo", "doctor", "session", "model", "artifact"):
        assert MARKER not in helps[verb], verb


def test_the_epilog_names_the_entry_points_and_explains_the_marker():
    """A marker nobody defines is a decoration. The epilog is the only place `--help` can say what
    `(API)` means, and it is where the first command a visitor should run is named -- `run`, not
    `discover`, since #546/#547 (docs/decisions/0018-three-journey-verbs.md)."""
    epilog = _build_parser().epilog or ""
    assert "requivo demo" in epilog
    assert "requivo run" in epilog
    assert MARKER in epilog
    assert "ANTHROPIC_API_KEY" in epilog


def test_the_epilog_survives_argparse_reflowing_it():
    """argparse's default formatter re-wraps `epilog` into one paragraph, which would run the two
    example commands into the prose around them. The formatter must be one that leaves it alone --
    asserted through the rendered help rather than by naming the class, so a different formatter
    with the same behaviour still passes."""
    text = _build_parser().format_help()
    assert "\n  requivo demo" in text
    assert "\n  requivo run " in text


def test_the_deterministic_package_still_registers_every_verb():
    """Moving `register_deterministic(sub)` down the function must not weaken its own guard: it
    composes four halves and an ImportError is the intended failure if one stops registering."""
    names = {name for name, _ in _subcommands()}
    assert {"doctor", "schema", "context", "session", "model", "artifact"} <= names
