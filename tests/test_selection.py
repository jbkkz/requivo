"""Selector guards: an empty or unmatched token never widens to everything or empties to nothing (#13, #33, #35, #40)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from requivo.core import context as context_mod
from requivo.core.context import (
    _SELECTION_REFUSALS,
    available_cards,
    build_prompt,
    check_selection,
    load_context,
    resolve_cards,
)
from requivo.core.dependencies import _all_slot_ids, resolve_slots
from requivo.core.errors import (
    ContextUnreadableError,
    EmptySelectionError,
    EmptySelectorTokenError,
    NoContextCardsError,
    RequivoError,
    UnknownContextCardError,
    UnsafeSelectorTokenError,
)
from requivo.core.selectors import display_token, normalize_tokens
from requivo.deterministic.remedies import _RESTORABLE_CARD_CODES

A_CARD = "b2b-platform"          # a bundled card, committed to the repo
ANOTHER_CARD = "financial-reporting"
_SELECTORS = (resolve_cards, load_context, resolve_slots)


@pytest.fixture
def zero_cards(tmp_path, monkeypatch):
    """An install with no context cards at all (#33): the user card directory, to install one into."""
    bundled, user = tmp_path / "bundled-cards", tmp_path / "user-cards"
    bundled.mkdir()
    user.mkdir()
    monkeypatch.setattr(context_mod, "CONTEXT", bundled)
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user))
    assert available_cards() == [], "fixture is not empty: it still sees cards"
    return user


def _install_a_card(user_dir, stem="acme-crm", body="ACME CRM - the product context."):
    """The must-fire half of every `zero_cards` test: make the install healthy again, in place."""
    (user_dir / f"{stem}.md").write_text(body, encoding="utf-8")
    return stem


def _agrees(selection) -> None:
    """`check_selection` returning None means `load_context` succeeds; a problem means it raises that code."""
    problem = check_selection(selection)
    try:
        load_context(selection)
    except RequivoError as e:
        assert problem is not None and problem.code == e.code, f"load_context refused {selection!r} and check_selection did not"
    else:
        assert problem is None, f"check_selection refused {selection!r} and load_context did not"


# ── the positive control, and the ordinary path ─────────────────────────────────

def test_harness_can_see_both_vocabularies():
    """The silence half of every test below is only meaningful if the fixture can see something."""
    cards = available_cards()
    assert A_CARD in cards and ANOTHER_CARD in cards and all(not c.startswith("_") for c in cards), cards
    assert len(_all_slot_ids()) > 1, "fixture is blind: the slot schema resolved to fewer than 2 slots"
    ctx = load_context()
    assert f"## {A_CARD}" in ctx and "## _template" not in ctx   # a `_`-prefixed card is skipped


def test_load_context_refuses_when_only_underscore_files_are_present(tmp_path, monkeypatch):
    """A directory holding nothing but `_`-prefixed files has no cards (#33); one real card and it loads."""
    ctx_dir = tmp_path / "context"
    ctx_dir.mkdir()
    (ctx_dir / "_only_template.md").write_text("skip me", encoding="utf-8")
    monkeypatch.setattr(context_mod, "CONTEXT", ctx_dir)
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(tmp_path / "no-user-cards"))
    with pytest.raises(NoContextCardsError):
        load_context()
    (ctx_dir / "real-card.md").write_text("REAL CARD", encoding="utf-8")
    loaded = load_context()
    assert "## real-card" in loaded and "## _only_template" not in loaded


def test_user_context_cards_merge_and_override_bundled(tmp_path, monkeypatch):
    """A pip-installed user extends discovery by dropping cards in REQUIVO_CONTEXT_DIR; user wins on a stem clash."""
    bundled, user = tmp_path / "bundled", tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    (bundled / "b2b-platform.md").write_text("BUNDLED b2b", encoding="utf-8")
    (bundled / "shared.md").write_text("BUNDLED shared", encoding="utf-8")
    (user / "my-product.md").write_text("USER product", encoding="utf-8")
    (user / "shared.md").write_text("USER shared override", encoding="utf-8")
    monkeypatch.setattr(context_mod, "CONTEXT", bundled)
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user))
    assert available_cards() == ["b2b-platform", "my-product", "shared"]
    ctx = load_context()
    assert "BUNDLED b2b" in ctx and "USER product" in ctx and "USER shared override" in ctx and "BUNDLED shared" not in ctx


# ── 1. an empty slot token resolved to every slot ────────────────────────────────

def test_empty_slot_token_is_refused_rather_than_matching_every_label():
    """`"" in label` is true for every label: `requivo impact <slug> ""` reported the whole model as changed."""
    assert resolve_slots(["workflow"]) == (["workflow"], [])
    assert resolve_slots(["permission"]) == (["permissions"], [])   # a label substring still resolves
    assert resolve_slots(["workflow", "zzz"]) == (["workflow"], ["zzz"])
    assert resolve_slots(["workflow"])[0] != sorted(_all_slot_ids())   # a real query is narrow
    for token_list in ([""], ["  "], ["\t"], ["workflow", ""]):
        with pytest.raises(EmptySelectorTokenError):
            resolve_slots(token_list)
    with pytest.raises(EmptySelectorTokenError) as ei:
        resolve_slots("workflow,".split(","))   # a caller splitting a comma-separated value
    assert ei.value.details["position"] == 1    # names *which* token, not just that one was bad


# ── 2. a card selection that no longer resolves yielded zero product context ─────

def test_load_context_refuses_a_selection_that_matched_nothing():
    """A name that matched nothing produced `""`, which `build_prompt` spliced into `{{CONTEXT}}` every turn."""
    picked = load_context([A_CARD])
    assert f"## {A_CARD}" in picked and f"## {ANOTHER_CARD}" not in picked
    assert len(load_context()) > len(picked)    # the unrestricted load is still wider
    for selection in (["nonexistent-card"], [A_CARD, "nonexistent-card"]):   # a partial miss is still a miss
        with pytest.raises(UnknownContextCardError) as ei:
            load_context(selection)
        assert ei.value.details["unknown"] == ["nonexistent-card"]


def test_both_card_selectors_echo_an_unknown_name_as_the_caller_typed_it():
    """`resolve_cards` and `load_context` are one design; matching stays case-insensitive, the echo does not."""
    assert resolve_cards([f" {A_CARD.upper()} "]) == [A_CARD]
    assert f"## {A_CARD}" in load_context([A_CARD.upper()])
    for selector in (resolve_cards, load_context):
        with pytest.raises(UnknownContextCardError) as ei:
            selector(["  Some-Card  "])
        assert ei.value.details["unknown"] == ["Some-Card"], f"{selector.__name__} lower-cased the echo"


def test_resolve_cards_maps_stems_and_refuses_an_unknown_one():
    """One resolver in core, shared by every surface; no selection at all is `None`, every card."""
    assert resolve_cards([A_CARD, f" {ANOTHER_CARD}"]) == [A_CARD, ANOTHER_CARD]
    assert resolve_cards([]) is None
    with pytest.raises(UnknownContextCardError) as ei:
        resolve_cards([A_CARD, "nope"])
    assert ei.value.details["unknown"] == ["nope"]


# ── the shared rule ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("selector, good", [
    pytest.param(resolve_cards, [A_CARD], id="resolve_cards"),
    pytest.param(load_context, [A_CARD], id="load_context"),
    pytest.param(resolve_slots, ["workflow"], id="resolve_slots"),
])
def test_every_selector_refuses_an_empty_token(selector, good):
    """One rule, one helper, three sites; every refusal carries the token's position (#35)."""
    assert good[0] in repr(selector(good)), "must fire: the selector resolves a real token"
    for tokens in ([""], ["   "], [*good, ""]):
        with pytest.raises(EmptySelectorTokenError) as ei:
            selector(tokens)
        assert ei.value.details == {"selector": ei.value.details["selector"], "position": len(tokens) - 1}


def test_a_selector_handed_a_generator_does_not_silently_resolve_to_nothing():
    """`normalize_tokens` iterates what it is given, so a generator resolves and is refused like a list."""
    assert resolve_slots(t for t in ["workflow"]) == (["workflow"], [])
    assert resolve_cards(t for t in [A_CARD]) == [A_CARD]
    with pytest.raises(EmptySelectorTokenError):
        resolve_slots(t for t in ["workflow", ""])


def test_normalize_tokens_passes_real_tokens_through_stripped_and_lowercased():
    assert normalize_tokens([" Workflow ", "PERMISSIONS"], what="slot") == ["workflow", "permissions"]
    assert normalize_tokens([], what="slot") == []      # an empty list is not an empty token


# ── asking the same question without paying for it (#12) ─────────────────────────

def test_check_selection_answers_exactly_what_load_context_would_do():
    """It reports, it does not raise: an unknown card, an empty selection and an empty token are each named."""
    for selection in (None, [A_CARD], [A_CARD, ANOTHER_CARD], [A_CARD.upper()]):
        assert check_selection(selection) is None
    unknown = check_selection([A_CARD, "no-such-card"])
    assert isinstance(unknown, UnknownContextCardError) and unknown.details["unknown"] == ["no-such-card"]
    assert unknown.to_dict()["code"] == "unknown_context_card"
    assert isinstance(check_selection([]), EmptySelectionError)
    assert isinstance(check_selection([" "]), EmptySelectorTokenError)   # a token, not a selection


@pytest.mark.parametrize("selection", [None, [A_CARD], [A_CARD, ANOTHER_CARD], ["no-such-card"], []])
def test_check_selection_agrees_with_load_context_on_every_selection(selection):
    """The drift guard itself (#12)."""
    _agrees(selection)


# ── 4. an install with no cards at all (#33): the wide instance the two narrow fixes left open ───

def test_load_context_refuses_an_install_with_no_cards_at_all(zero_cards):
    """`no_context_cards` names both roots and the install, never a card; one card and the same call succeeds."""
    for selection in (None, ["acme-crm"], []):   # the install is diagnosed ahead of the selection
        with pytest.raises(NoContextCardsError) as ei:
            load_context(selection)
    assert len(ei.value.details["roots"]) == 2, "both card roots are reported, not just the bundled one"
    assert ei.value.to_dict()["code"] == "no_context_cards" and "install" in ei.value.message.lower()
    stem = _install_a_card(zero_cards)
    loaded = load_context(None)
    assert f"## {stem}" in loaded and "ACME CRM" in loaded
    with pytest.raises(UnknownContextCardError) as ei:
        load_context(["no-such-card"])   # a genuinely unknown name is an unknown name again
    assert ei.value.details["unknown"] == ["no-such-card"]


def test_build_prompt_never_sends_an_empty_context_to_a_paid_call(zero_cards):
    """The consequence that costs money: neither an empty install nor a dead selection reaches `{{CONTEXT}}`."""
    with pytest.raises(NoContextCardsError):
        build_prompt("engine.md", None)
    stem = _install_a_card(zero_cards)
    prompt = build_prompt("engine.md", None)
    assert f"## {stem}" in prompt and "{{CONTEXT}}" not in prompt and "{{SCHEMA}}" not in prompt
    with pytest.raises(UnknownContextCardError):
        build_prompt("engine.md", ["nonexistent-card"])


def test_check_selection_agrees_with_load_context_on_a_zero_card_install(zero_cards):
    """The drift guard against the fixture that used to be missing (#33), and clean again once there is a card."""
    for selection in (None, ["acme-crm"], ["no-such-card"], []):
        _agrees(selection)
    stem = _install_a_card(zero_cards)
    assert check_selection(None) is None and check_selection([stem]) is None


def test_the_prompt_assembly_path_never_decodes_an_asset_with_the_locale_encoding():
    """Parsed rather than grepped, since this module's own comments name `read_text()`."""
    tree = ast.parse(Path(context_mod.__file__).read_text(encoding="utf-8"))
    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "read_text"]
    assert reads, "must fire: the scan found no read_text calls at all, so it proves nothing"
    bare = [n.lineno for n in reads if not any(k.arg == "encoding" for k in n.keywords)]
    assert not bare, f"{context_mod.__file__} decodes an asset with the locale's encoding at line(s) {bare}"


# ── 5. one code carried two facts (#35) ──────────────────────────────────────────

def test_an_empty_token_and_an_empty_selection_are_two_codes():
    """`empty_selector_token` used to carry two `details` shapes; both halves still ride the structured envelope."""
    with pytest.raises(EmptySelectorTokenError) as token:
        normalize_tokens(["ok", " "], what="context card")
    with pytest.raises(EmptySelectionError) as selection:
        load_context([])
    for err, code, details in ((token.value, "empty_selector_token", {"selector": "context card", "position": 1}),
                               (selection.value, "empty_selection", {"selector": "context card", "tokens": 0})):
        assert isinstance(err, RequivoError)
        assert err.to_dict()["code"] == code and err.to_dict()["details"] == details
        assert "context" in err.to_dict()["message"]    # the message names what was being selected
    assert not isinstance(selection.value, EmptySelectorTokenError) and not isinstance(token.value, EmptySelectionError)


# ── a selector token that forges the receipt reporting it (#40) ──────────────────

def test_a_control_character_in_a_selector_token_is_refused_not_echoed():
    """#40: a selector token is caller text; a control character is a refusal, named in escaped form."""
    assert normalize_tokens([A_CARD], what="context card") == [A_CARD]
    for token in ("ok-card\nAll clear.", "a\rb", "a\tb", "a\x1b[2Kb", "a\x00b", "a\x9bb"):
        for call in (lambda t: normalize_tokens([t], what="context card"), *(lambda t, s=s: s([t]) for s in _SELECTORS)):
            with pytest.raises(UnsafeSelectorTokenError):
                call(token)
    with pytest.raises(UnsafeSelectorTokenError) as ei:
        resolve_cards(["ok-card\nAll clear."])
    assert "\n" not in ei.value.message and "ok-card" in ei.value.message
    assert ei.value.code == "unsafe_selector_token"
    assert ei.value.details == {"selector": "context card", "position": 0}


def test_check_selection_reports_a_hostile_persisted_card_rather_than_raising():
    """`check_selection` is what `doctor`/`session verify` ask (#40)."""
    assert check_selection([A_CARD]) is None
    problem = check_selection(["ok-card\nAll clear."])
    assert problem is not None and problem.code == "unsafe_selector_token" and "\n" not in problem.message


def test_the_two_card_code_tables_agree():
    """`_RESTORABLE_CARD_CODES` decides which remedy `doctor`/`session verify` print."""
    returnable = {e.code for e in _SELECTION_REFUSALS}
    assert returnable, "must fire: the refusal tuple is not empty"
    assert _RESTORABLE_CARD_CODES <= returnable, (
        f"a remedy is routed on a code check_selection can never return: {sorted(_RESTORABLE_CARD_CODES - returnable)}")
    assert returnable - _RESTORABLE_CARD_CODES == {"empty_selection", "empty_selector_token", "unsafe_selector_token"}
    assert ContextUnreadableError not in _SELECTION_REFUSALS, (
        "context_unreadable is now returned rather than propagated; _RESTORABLE_CARD_CODES needs revisiting")


def test_display_token_is_the_render_side_companion_where_no_selector_runs():
    """`session show` prints a stored card name without selecting anything with it; non-ASCII is not unsafe."""
    assert display_token(A_CARD) == A_CARD and display_token("Ünïcode-cárd") == "Ünïcode-cárd"
    shown = display_token("ok-card\nAll clear.")
    assert "\n" not in shown and "ok-card" in shown
