"""One absence, one sentence: every CLI route to "there is no such session" (#243).

The plugin README spends two paragraphs on the trap it calls *fails in no visible way* -- a valid
session made invisible by running from the wrong directory. This is the error where that trap
actually bites, and it existed in five wordings across three modules, of which exactly one named
the sessions root it had searched and none named the verb that lists what is there. A user who
typed `leave-aproval` was told `no model file or session found for 'leave-aproval'` and left with
nowhere to go next.

The sweep is the finding, which is why these live in a module of their own rather than beside each
verb's own tests. Each site was individually defensible; what was wrong was the set. A test per
verb would have gone green on all five wordings, and the sixth site added later would have arrived
with a sixth.

`no_session_message` in `core/persistence/store.py` is the one builder. These tests assert against the
*rendered* output of real verbs rather than against that function, because a shared builder nobody
calls is the failure this replaced -- `_no_session` already named the root and was reachable from
none of the verbs a user runs.

The shared harness is `tests/_cli_harness.py`.
"""
from __future__ import annotations

import json

import pytest

from requivo.cli import app
from requivo.core import persistence as store

# Every verb that can be handed a slug that is not there, one per distinct raising site. `session
# show` and `session verify` are in because they were the two that leaked the word "canonical" --
# engine vocabulary for "not the retired out/ layout", which means nothing to a user and names a
# distinction they cannot act on.
_VERBS = [
    ["status", "no-such-session"],
    ["answer", "no-such-session", "some answers"],
    ["impact", "no-such-session"],
    ["brief", "no-such-session"],
    ["prd", "no-such-session"],
    ["criteria", "no-such-session"],
    ["epic", "no-such-session"],
    ["release", "no-such-session"],
    ["stories", "no-such-session"],
    ["estimate", "no-such-session"],
    ["session", "show", "no-such-session"],
    ["session", "verify", "no-such-session"],
    ["session", "export", "no-such-session"],
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


def _fails(argv, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        app(argv, client=None)   # client=None -> an accidental API call would blow up
    assert exc.value.code == 1
    return capsys.readouterr().err


@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: "-".join(a[:2]))
def test_every_cli_route_to_a_missing_session_names_the_root_and_the_listing_command(
        argv, workspace, capsys):
    """The two facts a user needs and none of the five wordings carried: *where I looked* and *how
    to see what is actually there*. Naming the root is what makes the wrong-directory trap visible
    at the moment it bites; naming `requivo session list` is what turns a dead end into a step."""
    err = _fails(argv, capsys)
    assert str(store.session_root()) in err, f"{argv} does not name the sessions root it searched"
    assert "requivo session list" in err, f"{argv} does not name the listing command"
    assert "--workspace" in err, f"{argv} does not say what changes where Requivo looks"


@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: "-".join(a[:2]))
def test_no_cli_route_to_a_missing_session_says_canonical(argv, workspace, capsys):
    """`canonical` distinguishes the current layout from the retired `out/` one. It is a fact about
    the store's history, the user cannot act on it, and it appeared only in the three sites nobody
    reaches from the main verbs -- so the jargon and the missing help travelled together."""
    assert "canonical" not in _fails(argv, capsys).lower()


def test_the_structured_envelope_still_carries_the_published_code_and_slug(workspace, capsys):
    """Message text is not the contract; `code` and `details` are, and `docs/compatibility.md`
    promises them. This change rewrites every one of those sentences, so the half consumers read
    has to be pinned in the same commit -- otherwise the next rewording is free to move it."""
    with pytest.raises(SystemExit):
        app(["status", "no-such-session", "--json"], client=None)
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "session_not_found"
    assert payload["details"]["ref"] == "no-such-session"


def test_a_reference_carrying_a_control_character_cannot_write_its_own_line(workspace, capsys):
    """A refusal echoes the thing refused, and the thing refused here is raw argv. A newline in it
    ends the line, and everything after it is a sentence Requivo appears to be saying -- the shape
    #40 fixed for a stored card name, arriving on the field this message is built from.

    **Which guard fires here is not the one the message calls, and saying so is the point.** On
    every current CLI route `validate_slug` refuses a control character before `no_session_message`
    is reached, so a test that asserted the escaping and named `display_token` would be green
    whether or not the builder escaped anything at all. What is pinned is therefore the outcome --
    no forged line at column 0, from whichever refusal answered -- and the builder's own escaping is
    pinned separately below, against the builder."""
    err = _fails(["status", "ok\nAll clear, nothing to see."], capsys)
    assert "\nAll clear" not in err
    assert "All clear" in err       # must fire: it is escaped, not quietly dropped


def test_the_shared_builder_escapes_a_reference_it_could_be_handed_directly(workspace):
    """`display_token` inside `no_session_message` is a second line of defence and is documented as
    one. It cannot fire from the CLI, where `validate_slug` gets there first -- but the builder is
    public in `core.persistence`, and invariant 14's rule is that a surface being careful is not the
    guarantee: an external consumer calls this layer. So the escaping is asserted where it can
    actually be exercised, rather than through a verb that would prove nothing about it."""
    forged = "ok\nAll clear, nothing to see."
    line = store.no_session_message(forged)
    assert "\nAll clear" not in line
    assert "All clear" in line                      # must fire: escaped, not dropped
    assert store.no_session_message("leave-approval").startswith("no session named leave-approval")
