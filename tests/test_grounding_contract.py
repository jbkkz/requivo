"""Every surface a human reads names the product context the session was grounded on (#492).

`information_value = uncertainty x impact` is the whole driver, and the context cards are what the
impact half is read against. So a session can be grounded on `financial-reporting` while reasoning
about a CI matrix, produce a model, reach *ready* -- and nothing on screen names that its question
selection was scored against a product the request has nothing to do with. The preflight tells the
three states `ok` / `empty` / `unreadable` apart; *present, readable, and about the wrong product*
is a fourth, and it renders identically to `ok`.

**The decision this file pins is that there is no fourth status, and there is not going to be one.**
Every other `context.status` value is decidable from the filesystem. Relevance is not: automating it
needs either a model call inside the free deterministic preflight -- putting a paid, fallible
judgment in front of the one path whose whole value is that it is decidable -- or a keyword
heuristic, which is the combination that is right often enough to be trusted and wrong silently.
`CLAUDE.md`'s own bar for automatic relevance routing is a third *measured* instance of a card
diluting its neighbour; two are on record (the `financial-reporting` / `doc-reapproval` measurement,
and #489's external validation report). That is the revisit trigger, written down so it stops being
re-derived.

What is left is that the human is the detector -- and a detector is only as good as the fact they
are handed. So this is a property rather than a list of expected strings: **a surface that renders a
session's state names what that state was reasoned against, and says something different when the
grounding was never narrowed.** A surface may word it however suits its reader; it may not be silent,
and it may not render the two cases identically.

The shape is `tests/test_readiness_contract.py`'s, deliberately: one fact, several surfaces, and a
guard that lives above all of them rather than inside any one -- because what went wrong in #165 was
two surfaces disagreeing off the same `model.json`, and what goes wrong here is one surface simply
not saying it.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _cli_harness import _full_model, _run
from fastapi.testclient import TestClient

from requivo.render.terminal import render_grounding
from requivo.web.app import create_app
from requivo.web.viewmodels.status import grounding_view

CARD = "event-ops"
SLUG = "grounded"


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _proposal(tmp_path) -> Path:
    """A complete model on disk, applied by `_seed` -- `model apply` takes a path, not inline JSON."""
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(_full_model()), encoding="utf-8")
    return path


def _seed(cards: list[str] | None, proposal: Path) -> None:
    """A session at revision 1, grounded either on one named card or on nothing in particular."""
    argv = ["session", "init", "A leave approval request", "--slug", SLUG]
    if cards is not None:
        argv += ["--context", ",".join(cards)]
    _run(argv)
    _run(["model", "apply", SLUG, str(proposal)])


def _terminal_status(cards: list[str] | None, proposal: Path) -> str:
    _seed(cards, proposal)
    return _run(["status", SLUG])


def _terminal_session_show(cards: list[str] | None, proposal: Path) -> str:
    _seed(cards, proposal)
    return _run(["session", "show", SLUG])


def _web_primary_screen(cards: list[str] | None, proposal: Path) -> str:
    """The rendered page, cut at *Traceability details*.

    The cut is the assertion, not a convenience. The history line under traceability has named the
    cards since long before this issue, and it is one click away -- which is exactly the state #492
    was filed about: the fact existed and the reader was not handed it. A test reading the whole page
    would have been green on the defect."""
    _seed(cards, proposal)
    client = TestClient(create_app(), base_url="http://127.0.0.1:8765",
                        raise_server_exceptions=False)
    response = client.get(f"/sessions/{SLUG}")
    assert response.status_code == 200, f"the session page answered {response.status_code}"
    body = response.text
    marker = body.find("Traceability")
    return body[:marker] if marker != -1 else body


def _terminal_renderer(cards: list[str] | None, _proposal: Path) -> str:
    """`render_grounding` on its own, with no session behind it -- the unit the two CLI verbs share,
    so a regression in it is attributed here rather than to whichever verb happened to be read."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_grounding(cards)
    return buf.getvalue()


def _wipe(workspace: Path) -> None:
    """Remove the seeded session so the next `session init` in the same test can claim the slug
    again. `create_session`'s claim is atomic and by identity (invariant 11), so re-seeding the same
    slug with *different* cards is a different identity and would raise rather than overwrite --
    which is the behaviour under test elsewhere, not something to work around silently here."""
    import shutil
    root = workspace / ".requivo"
    if root.exists():
        shutil.rmtree(root)


SURFACES = [
    ("terminal status", _terminal_status),
    ("terminal session show", _terminal_session_show),
    ("web primary screen", _web_primary_screen),
    ("terminal renderer", _terminal_renderer),
]


@pytest.mark.parametrize("surface,render", SURFACES, ids=[s for s, _ in SURFACES])
def test_every_surface_names_the_cards_a_session_was_grounded_on(surface, render, _proposal):
    assert CARD in render([CARD], _proposal), (
        f"{surface} renders a session's state without naming what that state was reasoned "
        f"against -- the reader is the only detector of a wrong grounding, and this is the fact "
        f"they judge")


@pytest.mark.parametrize("surface,render", SURFACES, ids=[s for s, _ in SURFACES])
def test_no_surface_renders_a_narrowed_and_an_unnarrowed_grounding_the_same_way(
        surface, render, _proposal, tmp_path):
    """The must-fire half. Without it, a surface that printed a fixed caption and no value would
    pass the row above for the `[CARD]` case only by accident of the caption containing the word,
    and a surface that dropped the distinction entirely would pass both."""
    narrowed = render([CARD], _proposal)
    _wipe(tmp_path)
    unnarrowed = render(None, _proposal)
    assert narrowed != unnarrowed, (
        f"{surface} says the same thing whether the session was scoped to one product area or "
        f"left on every card in the install")


def test_the_view_model_states_which_of_the_two_it_is_rather_than_leaving_it_to_the_template():
    """`narrowed` is a fact about the session; the template's job is to word it. A template deciding
    it from `cards|length` would be re-deriving, which is the one thing `viewmodels/` may not do --
    and it would be wrong the day an install holds exactly as many cards as a session named."""
    assert grounding_view({"context_cards": [CARD]}) == {"narrowed": True, "cards": [CARD]}
    unnarrowed = grounding_view({"context_cards": None})
    assert unnarrowed["narrowed"] is False
    assert CARD in unnarrowed["cards"], (
        "an unnarrowed session is grounded on every card in the install, re-resolved per turn -- "
        "not on none")


def test_no_surface_claims_a_relevance_verdict_it_cannot_reach(_proposal, tmp_path):
    """The other half of the decision, and the one a future change is most likely to break: naming
    is not judging. A surface that started calling a grounding `mismatched`, `wrong` or `irrelevant`
    would be asserting something no deterministic path can decide -- see this module's docstring for
    why the two available ways of deciding it are both refused."""
    for cards in ([CARD], None):
        for _, render in SURFACES:
            _wipe(tmp_path)
            rendered = render(cards, _proposal).lower()
            for word in ("mismatched", "irrelevant", "wrong product", "not relevant"):
                assert word not in rendered, (
                    f"a surface renders {word!r}: relevance is a judgment, and no surface here has "
                    f"a way to reach one")
