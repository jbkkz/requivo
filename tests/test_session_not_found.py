"""One absence, one sentence: every CLI route to "there is no such session" (#243)."""
from __future__ import annotations

import json

import pytest

from requivo.cli import app
from requivo.core import persistence as store

# Every verb that can be handed a slug that is not there, one per distinct raising site.
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


def _fails(argv, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        app(argv, client=None)   # client=None -> an accidental API call would blow up
    assert exc.value.code == 1
    return capsys.readouterr().err


@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: "-".join(a[:2]))
def test_every_cli_route_to_a_missing_session_names_the_root_and_the_listing_command(
        argv, workspace, capsys):
    """The two facts a user needs and none of the five wordings carried."""
    err = _fails(argv, capsys)
    assert str(store.session_root()) in err, f"{argv} does not name the sessions root it searched"
    assert "requivo session list" in err, f"{argv} does not name the listing command"
    assert "--workspace" in err, f"{argv} does not say what changes where Requivo looks"


@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: "-".join(a[:2]))
def test_no_cli_route_to_a_missing_session_says_canonical(argv, workspace, capsys):
    """`canonical` distinguishes the current layout from the retired `out/` one."""
    assert "canonical" not in _fails(argv, capsys).lower()


def test_the_structured_envelope_still_carries_the_published_code_and_slug(workspace, capsys):
    """Message text is not the contract; `code` and `details` are, and `docs/compatibility.md` promises them."""
    with pytest.raises(SystemExit):
        app(["status", "no-such-session", "--json"], client=None)
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "session_not_found"
    assert payload["details"]["ref"] == "no-such-session"


def test_a_reference_carrying_a_control_character_cannot_write_its_own_line(workspace, capsys):
    """A refusal echoes the thing refused, and the thing refused here is raw argv (#40)."""
    err = _fails(["status", "ok\nAll clear, nothing to see."], capsys)
    assert "\nAll clear" not in err
    assert "All clear" in err       # must fire: it is escaped, not quietly dropped


def test_the_shared_builder_escapes_a_reference_it_could_be_handed_directly(workspace):
    """`display_token` inside `no_session_message` is a second line of defence and is documented as one."""
    forged = "ok\nAll clear, nothing to see."
    line = store.no_session_message(forged)
    assert "\nAll clear" not in line
    assert "All clear" in line                      # must fire: escaped, not dropped
    assert store.no_session_message("leave-approval").startswith("no session named leave-approval")
