"""`requivo --help` is the first screen, and it used to read as an implementation order (#244)."""
from __future__ import annotations

import argparse
import re

import pytest

from requivo.cli import _HELP_GROUP_PLUMBING, _HELP_GROUP_SCRIPTS, _HELP_GROUP_START, _build_parser
from requivo.providers.anthropic.generators import _OP_PROMPTS

# The verbs that make a paid call (#540).
API_VERBS = (set(_OP_PROMPTS) - {"analyze"}) | {"discover", "answer", "run", "docs"}

MARKER = "(API)"


def _subcommands() -> list:
    """(name, help) in registration order -- no longer the order `--help` renders them in (#546)."""
    for action in _build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return [(c.dest, c.help or "") for c in action._choices_actions]
    raise AssertionError("_build_parser() registers no subcommands")


def test_start_here_leads_the_rendered_help():
    """The acceptance bar from #546, checked against what `requivo --help` actually prints rather than against
    registration order: the first names a reader meets are `demo` and `run`, and `discover`."""
    text = _build_parser().format_help()
    start = text.index("Start here:")
    scripts = text.index("For scripts and integrations")
    plumbing = text.index("Plumbing:")
    assert start < scripts < plumbing, "the three groups render out of order"

    start_section = text[start:scripts]
    assert re.search(r"^\s*demo\s", start_section, re.MULTILINE)
    assert re.search(r"^\s*run\s", start_section, re.MULTILINE)
    # A substring check would also match "discovers" inside `run`'s own help text.
    assert not re.search(r"^\s*discover\s", start_section, re.MULTILINE), (
        "discover leaked onto the first screen")


def test_every_registered_verb_appears_in_exactly_one_help_group():
    """The completeness half of #546's acceptance criterion ("every existing verb appears exactly once")."""
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


@pytest.mark.parametrize("columns", ["60", "100", "200"])
def test_every_verb_help_is_byte_identical_regardless_of_the_root_formatter(monkeypatch, columns):
    """The other acceptance criterion: `requivo <verb> --help` unchanged for every top-level verb."""
    monkeypatch.setenv("COLUMNS", columns)
    grouped = _build_parser()
    plain = _build_parser(formatter_class=argparse.HelpFormatter)

    grouped_sub = next(a for a in grouped._actions if isinstance(a, argparse._SubParsersAction))
    plain_sub = next(a for a in plain._actions if isinstance(a, argparse._SubParsersAction))

    # must fire: the walk really found both trees, so the diff below is not vacuously empty.
    assert set(grouped_sub.choices) and set(grouped_sub.choices) == set(plain_sub.choices)

    changed = sorted(
        name for name, sp in grouped_sub.choices.items()
        if sp.format_help() != plain_sub.choices[name].format_help())
    assert changed == [], f"`requivo <verb> --help` changed for: {changed}"


def test_every_paid_verb_in_a_compact_group_still_shows_the_marker():
    """#546: "For scripts and integrations" and "Plumbing" render names only, comma-joined."""
    text = _build_parser().format_help()
    helps = dict(_subcommands())
    paid_outside_start = [name for name in (*_HELP_GROUP_SCRIPTS, *_HELP_GROUP_PLUMBING)
                           if MARKER in helps[name]]
    # must fire: the property below is vacuous if no paid verb lives outside "Start here".
    assert paid_outside_start
    for name in paid_outside_start:
        assert f"{name} {MARKER}" in text, f"{name} lost its {MARKER} marker in the rendered --help"


def test_the_plumbing_verbs_come_after_the_journey_verbs_in_registration_order():
    """Registration order, not rendered order (#546 separated the two)."""
    order = [name for name, _ in _subcommands()]
    plumbing = {"doctor", "schema", "context", "session", "model", "artifact"}
    journey = ["run", "discover", "answer", "status", "brief"]
    first_plumbing = min(order.index(name) for name in plumbing)
    assert all(order.index(v) < first_plumbing for v in journey), order


def test_every_paid_verb_is_marked_and_no_free_verb_is():
    """Both directions. The negative half is what makes the marker mean something (#546)."""
    helps = dict(_subcommands())
    marked = {name for name, text in helps.items() if MARKER in text}
    assert marked == API_VERBS, (
        f"marked but free: {sorted(marked - API_VERBS)}; "
        f"paid but unmarked: {sorted(API_VERBS - marked)}")


def test_every_verb_that_can_be_run_offline_says_so_or_says_nothing():
    """`status` and `impact` are the two a user is most likely to hesitate over."""
    helps = dict(_subcommands())
    for verb in ("status", "impact", "demo", "doctor", "session", "model", "artifact"):
        assert MARKER not in helps[verb], verb


def test_the_epilog_names_the_entry_points_and_explains_the_marker():
    """A marker nobody defines is a decoration (#546)."""
    epilog = _build_parser().epilog or ""
    assert "requivo demo" in epilog
    assert "requivo run" in epilog
    assert MARKER in epilog
    assert "ANTHROPIC_API_KEY" in epilog


def test_the_epilog_survives_argparse_reflowing_it():
    """argparse's default formatter re-wraps `epilog` into one paragraph."""
    text = _build_parser().format_help()
    assert "\n  requivo demo" in text
    assert "\n  requivo run " in text


def test_the_deterministic_package_still_registers_every_verb():
    """Moving `register_deterministic(sub)` down the function must not weaken its own guard."""
    names = {name for name, _ in _subcommands()}
    assert {"doctor", "schema", "context", "session", "model", "artifact"} <= names
