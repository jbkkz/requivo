"""The empty-install guard reaches session *creation*, not only the turns after it (#41).

`f48708e` gave the module a third state for the card vocabulary — `NoContextCardsError`, *we looked
at every root and there is nothing* — and wired it into the two consumers of `_card_paths()` that
call it directly: `load_context` and `check_selection`. `resolve_cards` reaches the same table
through `available_cards()`, so a sweep for `_card_paths()` never saw it and it kept answering
`UnknownContextCardError` for a name that was typed correctly.

The consequence is not cosmetic. `resolve_cards` is what the CLI, the deterministic verbs, the Web
route and `SessionService.create_session` all call, so on a card-less install creation answered
*your request was bad* (400) and the very next call answered *this install is incomplete* (500) —
one condition, two verdicts, and the wrong one arrives first.

Every refusal below is paired with its must-fire control in the same fixture: a card dropped into
the same empty roots, where an unknown name must go back to being an unknown name and a real name
must still resolve. Without that pairing a `NoContextCardsError` assertion passes just as happily
against a harness that cannot see anything at all.

These tests live in their own file rather than in `tests/test_selection.py` because that file was
being edited concurrently; the `zero_cards` fixture is therefore a local copy of the one there.
"""

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
    """An install with no context cards at all: both roots exist, both are readable, both are empty.

    That is what separates this from `context_unreadable` — here we looked, and there is nothing.
    """
    bundled, user = tmp_path / "bundled-cards", tmp_path / "user-cards"
    bundled.mkdir()
    user.mkdir()
    monkeypatch.setattr(context_mod, "CONTEXT", bundled)
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user))
    assert available_cards() == [], "fixture is not empty: it still sees cards"
    return user


def _install_a_card(user_dir, stem=A_NAME, body="ACME CRM - the product context."):
    """Make the install healthy again, in place — the must-fire half of every test here.

    `encoding` is explicit because `write_text` defaults to the console codepage, which is cp1252 on
    Windows; the body is ASCII today and a helper that only works for ASCII is a trap tomorrow.
    """
    (user_dir / f"{stem}.md").write_text(body, encoding="utf-8")
    return stem


def test_resolve_cards_on_a_zero_card_install_names_the_install_not_the_card(zero_cards):
    """The reported defect. With nothing installed, every name is "unknown" — technically true, and
    it sends the reader to check a name they typed correctly while the real fault is that there is
    nothing to match against. `_require_any_card`'s own docstring says so in those words; it simply
    never ran on this path."""
    with pytest.raises(NoContextCardsError) as ei:
        resolve_cards([A_NAME])
    assert ei.value.to_dict()["code"] == "no_context_cards"
    assert len(ei.value.details["roots"]) == 2, "the refusal names both roots it looked in"

    # must fire: put one card in the same roots and an unknown name is an unknown name again, so the
    # refusal above is about the empty install and not about a fixture that can see nothing at all.
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
    """The structural half, and the reason this is worth a test rather than a line.

    Three functions resolve a caller-supplied card name against the installed vocabulary and are read
    side by side, so a condition earning `no_context_cards` from two and `unknown_context_card` from
    the third is one surface being lenient where its siblings are strict."""
    with pytest.raises(NoContextCardsError):
        selector([A_NAME])

    # must fire, both halves: with a card present the same three accept a real name and refuse an
    # unknown one with the *narrow* code. An implementation that raised `NoContextCardsError`
    # unconditionally would pass the assertion above and fail here.
    stem = _install_a_card(zero_cards)
    selector([stem])
    with pytest.raises(UnknownContextCardError):
        selector([NOT_A_CARD])


def _raise(problem: RequivoError | None) -> None:
    """`check_selection` reports rather than raises, deliberately — a health check that raised would
    take a whole listing down with it. Re-raising here lets it be asserted beside the two selectors
    that do raise, without changing what it does."""
    if problem is not None:
        raise problem


def test_the_install_is_diagnosed_ahead_of_a_malformed_token_too(zero_cards):
    """Which guard wins is a decision, asserted rather than left implicit. A stray comma earns
    `empty_selector_token` and a control character earns `unsafe_selector_token`, both from
    `normalize_tokens` -- but on a card-less install the install is reported first, the same
    precedence `load_context` has held ahead of `_selection_keys` since #33. Telling somebody
    about a stray comma with no cards to select from is the same misdirection as their spelling."""
    for malformed in ([""], ["  "], ["ok-card\nAll clear."]):
        with pytest.raises(NoContextCardsError):
            resolve_cards(malformed)

    # must fire: with a card installed, the token-shape refusals are exactly what comes back — so the
    # assertions above pin a precedence rather than a guard that swallowed its two neighbours.
    _install_a_card(zero_cards)
    with pytest.raises(EmptySelectorTokenError):
        resolve_cards([""])
    with pytest.raises(UnsafeSelectorTokenError):
        resolve_cards(["ok-card\nAll clear."])


def test_creating_a_session_on_a_zero_card_install_refuses_at_creation(zero_cards, tmp_path,
                                                                      monkeypatch):
    """Invariant 14: the service layer is the integrity boundary, not the interfaces.
    `create_session` resolves the selection itself rather than trusting the caller. The session
    it used to create was one that could never be analysed: the first provider turn reads the
    same roots and refuses. Refusing here says the actionable thing at the first moment a new
    install is touched."""
    from requivo.services.sessions import SessionService

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path / "workspace"))
    with pytest.raises(NoContextCardsError):
        SessionService().create_session("A leave approval system.", context_cards=[A_NAME])

    # must fire: the same call on the same fixture succeeds once a card is there, so the refusal is
    # about the empty install and not about the workspace, the request or the service wiring.
    stem = _install_a_card(zero_cards)
    meta = SessionService().create_session("A leave approval system.", context_cards=[stem])
    assert meta.context_cards == [stem]


def test_no_selection_at_all_is_still_no_selection(zero_cards):
    """The deliberate non-change, pinned so it reads as a decision rather than an oversight. An
    empty list is not an empty token: `resolve_cards` answers `None` for it, the every-card
    sentinel, and the guard is not applied there -- not leniency but uniformity, since
    `SessionService`, the CLI and the deterministic verbs all skip `resolve_cards` when no cards
    were named. The install is still caught by `load_context`, at the point the cards are read."""
    assert resolve_cards([]) is None
    with pytest.raises(NoContextCardsError):
        load_context(None)          # …and the same install is refused where the cards are needed

    # must fire: `None` here means "every card", so once a card exists that is what gets loaded
    stem = _install_a_card(zero_cards)
    assert resolve_cards([]) is None
    assert f"## {stem}" in load_context(None)
