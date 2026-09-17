"""The empty-install guard reaches session *creation*, not only the turns after it (#41)."""

from __future__ import annotations

import pytest

from requivo.core import context as context_mod
from requivo.core.context import available_cards, check_selection, load_context, resolve_cards
from requivo.core.errors import (
    EmptySelectorTokenError,
    NoContextCardsError,
    RequivoError,
    UnknownContextCardError,
    UnsafeSelectorTokenError,
)

A_NAME = "acme-crm"          # a card that exists only once the fixture installs it
NOT_A_CARD = "no-such-card"  # a name that is wrong even on a healthy install


@pytest.fixture
def zero_cards(tmp_path, monkeypatch):
    """An install with no context cards at all: both roots exist, both are readable, both are empty."""
    bundled, user = tmp_path / "bundled-cards", tmp_path / "user-cards"
    bundled.mkdir()
    user.mkdir()
    monkeypatch.setattr(context_mod, "CONTEXT", bundled)
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user))
    assert available_cards() == [], "fixture is not empty: it still sees cards"
    return user


def _install_a_card(user_dir, stem=A_NAME, body="ACME CRM - the product context."):
    """Make the install healthy again, in place — the must-fire half of every test here."""
    (user_dir / f"{stem}.md").write_text(body, encoding="utf-8")
    return stem


def test_resolve_cards_on_a_zero_card_install_names_the_install_not_the_card(zero_cards):
    """The reported defect. With nothing installed, every name is "unknown"."""
    with pytest.raises(NoContextCardsError) as ei:
        resolve_cards([A_NAME])
    assert ei.value.to_dict()["code"] == "no_context_cards"
    assert len(ei.value.details["roots"]) == 2, "the refusal names both roots it looked in"

    # must fire: put one card in the same roots and an unknown name is an unknown name again.
    stem = _install_a_card(zero_cards)
    assert resolve_cards([stem]) == [stem]
    assert resolve_cards([stem.upper()]) == [stem], "matching stays case-insensitive"
    with pytest.raises(UnknownContextCardError) as unknown:
        resolve_cards([NOT_A_CARD])
    assert unknown.value.details["unknown"] == [NOT_A_CARD]


@pytest.mark.parametrize("selector", [
    pytest.param(lambda names: resolve_cards(names), id="resolve_cards"),
    pytest.param(lambda names: load_context(names), id="load_context"),
    pytest.param(lambda names: _raise(check_selection(names)), id="check_selection"),
])
def test_every_card_selector_reports_the_same_code_for_the_same_install(zero_cards, selector):
    """The structural half, and the reason this is worth a test rather than a line."""
    with pytest.raises(NoContextCardsError):
        selector([A_NAME])

    # must fire, both halves: with a card present the same three accept a real name and refuse an unknown one with the *narrow* code.
    stem = _install_a_card(zero_cards)
    selector([stem])
    with pytest.raises(UnknownContextCardError):
        selector([NOT_A_CARD])


def _raise(problem: RequivoError | None) -> None:
    """`check_selection` reports rather than raises, deliberately."""
    if problem is not None:
        raise problem


def test_the_install_is_diagnosed_ahead_of_a_malformed_token_too(zero_cards):
    """Which guard wins is a decision, asserted rather than left implicit (#33)."""
    for malformed in ([""], ["  "], ["ok-card\nAll clear."]):
        with pytest.raises(NoContextCardsError):
            resolve_cards(malformed)

    # must fire: with a card installed, the token-shape refusals are exactly what comes back.
    _install_a_card(zero_cards)
    with pytest.raises(EmptySelectorTokenError):
        resolve_cards([""])
    with pytest.raises(UnsafeSelectorTokenError):
        resolve_cards(["ok-card\nAll clear."])


def test_creating_a_session_on_a_zero_card_install_refuses_at_creation(zero_cards, tmp_path,
                                                                      monkeypatch):
    """Invariant 14: the service layer is the integrity boundary, not the interfaces."""
    from requivo.services.sessions import SessionService

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path / "workspace"))
    with pytest.raises(NoContextCardsError):
        SessionService().create_session("A leave approval system.", context_cards=[A_NAME])

    # must fire: the same call on the same fixture succeeds once a card is there.
    stem = _install_a_card(zero_cards)
    meta = SessionService().create_session("A leave approval system.", context_cards=[stem])
    assert meta.context_cards == [stem]


def test_no_selection_at_all_is_still_no_selection(zero_cards):
    """The deliberate non-change, pinned so it reads as a decision rather than an oversight."""
    assert resolve_cards([]) is None
    with pytest.raises(NoContextCardsError):
        load_context(None)          # …and the same install is refused where the cards are needed

    # must fire: `None` here means "every card", so once a card exists that is what gets loaded
    stem = _install_a_card(zero_cards)
    assert resolve_cards([]) is None
    assert f"## {stem}" in load_context(None)
