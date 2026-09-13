"""`requivo --help` is the first screen, and it read as an implementation order (#244).

argparse renders subcommands in registration order, and `_build_parser` registered the deterministic
package first -- so the six plumbing entries (doctor, schema, context, session, model, artifact) led,
and the two verbs a new user needs, `demo` and `discover`, sat seventh and eighth. Nothing
distinguished the nine verbs that spend the user's own money from the ten that cannot: `brief` and
`status` looked alike, and only `demo` and `doctor` said anything about a key at all.

The marker is checked in **both directions**, and the negative half is the one that matters. A test
that only asserted "every API verb is marked" is satisfied by marking all nineteen, which is the
same as marking none.

The expected set is **derived from the provider's own operation table**, not written out here. A
list in a test is a second copy of a decision, and the copy is what goes stale -- a generator added
to `_OP_PROMPTS` would arrive unmarked and this file would still be green. `_OP_PROMPTS` minus
`analyze` is the seven generators; `analyze` itself backs two verbs, `discover` and `answer`, which
is the only part stated by hand because it is a fact about the CLI rather than about the provider.
"""
from __future__ import annotations

import argparse

from requivo.cli import _build_parser
from requivo.providers.anthropic.generators import _OP_PROMPTS

# The verbs that make a paid call. `analyze` is the provider operation behind `discover`, `answer`
# and `run` (#540: a fresh request or an existing session's slug both reach it), so it expands to
# three; every other operation is one verb of the same name.
API_VERBS = (set(_OP_PROMPTS) - {"analyze"}) | {"discover", "answer", "run"}

MARKER = "(API)"


def _subcommands() -> list:
    """(name, help) in registration order, which is the order argparse prints."""
    for action in _build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return [(c.dest, c.help or "") for c in action._choices_actions]
    raise AssertionError("_build_parser() registers no subcommands")


def test_the_two_entry_points_lead_the_help():
    """The acceptance bar from the issue, kept as a bar rather than as a full ordering: `demo` and
    `discover` inside the first three entries. Pinning all nineteen positions would go red on any
    later reshuffle and teach the next person to edit the assertion."""
    first_three = [name for name, _ in _subcommands()[:3]]
    assert "demo" in first_three, first_three
    assert "discover" in first_three, first_three


def test_the_plumbing_verbs_come_after_the_journey_verbs():
    """The other half of the same claim, and the one the old order actually failed: a user reading
    top to bottom should meet the product before the diagnostics."""
    order = [name for name, _ in _subcommands()]
    plumbing = {"doctor", "schema", "context", "session", "model", "artifact"}
    journey = ["run", "discover", "answer", "status", "brief"]
    first_plumbing = min(order.index(name) for name in plumbing)
    assert all(order.index(v) < first_plumbing for v in journey), order


def test_every_paid_verb_is_marked_and_no_free_verb_is():
    """Both directions. The negative half is what makes the marker mean something -- marking all
    nineteen would satisfy the positive half and tell a reader nothing."""
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
    `(API)` means, and it is where the first command a visitor should run is named."""
    epilog = _build_parser().epilog or ""
    assert "requivo demo" in epilog
    assert "requivo discover" in epilog
    assert MARKER in epilog
    assert "ANTHROPIC_API_KEY" in epilog


def test_the_epilog_survives_argparse_reflowing_it():
    """argparse's default formatter re-wraps `epilog` into one paragraph, which would run the two
    example commands into the prose around them. The formatter must be one that leaves it alone --
    asserted through the rendered help rather than by naming the class, so a different formatter
    with the same behaviour still passes."""
    text = _build_parser().format_help()
    assert "\n  requivo demo" in text
    assert "\n  requivo discover " in text


def test_the_deterministic_package_still_registers_every_verb():
    """Moving `register_deterministic(sub)` down the function must not weaken its own guard: it
    composes four halves and an ImportError is the intended failure if one stops registering."""
    names = {name for name, _ in _subcommands()}
    assert {"doctor", "schema", "context", "session", "model", "artifact"} <= names
