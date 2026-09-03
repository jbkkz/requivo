"""Selector guards — an empty or unmatched token must never widen to everything or empty to nothing.

Issue #13. Three selectors — `resolve_cards`, `load_context` and `resolve_slots` — each turned an
absence into a confident answer, in opposite directions: an empty token matched every label, and a
selection that resolved to nothing became either *all of them* or *none of them*. Both render
exactly like a precise answer to a specific query, which is what makes them expensive.

**Every "must not widen" assertion here is paired with a "must fire" one in the same test.** A
negative assertion passes when nothing at all happens: if no context card could be read and the slot
schema were unreachable, every refusal below would pass for the wrong reason and report a coverage it
does not have. `test_harness_can_see_both_vocabularies` is the loud half — it fails rather than
skips when the fixture is blind.

The last section joined from the `test_engine.py` split (#72): the ordinary, non-adversarial half of
the same three functions — which cards a healthy install sees, what `only=` narrows to, and how a
user card overrides a bundled one. The guards above are only meaningful next to the behaviour they
guard, and until now that behaviour was asserted in another file.
"""

from __future__ import annotations

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
from requivo.deterministic.doctor import _RESTORABLE_CARD_CODES

A_CARD = "b2b-platform"          # a bundled card, committed to the repo
ANOTHER_CARD = "financial-reporting"


@pytest.fixture
def zero_cards(tmp_path, monkeypatch):
    """An install with **no context cards at all** — issue #33, and the scenario #12 was filed about:
    a wheel or container layer that ships `assets/` but loses `assets/context/`.

    Both roots exist and are readable; they are simply empty. That is what makes this distinct from
    `context_unreadable` (we could not look) — here we looked, and there is nothing. Every test that
    uses this fixture pairs its refusal with a card dropped into the same fixture, so a refusal can
    never pass because the harness is blind rather than because the install is empty.
    """
    bundled, user = tmp_path / "bundled-cards", tmp_path / "user-cards"
    bundled.mkdir()
    user.mkdir()
    monkeypatch.setattr(context_mod, "CONTEXT", bundled)
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user))
    assert available_cards() == [], "fixture is not empty: it still sees cards"
    return user


def _install_a_card(user_dir, stem="acme-crm", body="ACME CRM - the product context."):
    """The must-fire half of every `zero_cards` test: make the install healthy again, in place.

    `encoding` is explicit because `write_text` defaults to the console codepage, which is cp1252 on
    Windows — the body is ASCII here, but a helper that only works for ASCII is a trap for the next
    test that uses it.
    """
    (user_dir / f"{stem}.md").write_text(body, encoding="utf-8")
    return stem


# ── the positive control ─────────────────────────────────────────────────────────


def test_harness_can_see_both_vocabularies():
    """The silence half of every test below is only meaningful if the fixture can see something.

    If the bundled cards or the slot schema were unreachable, "an unknown card raises" would pass
    because *every* card is unknown, and "an empty token does not return all 15 slots" would pass
    because there are no slots to return. This fails loudly in that case rather than letting the
    other tests report a coverage they do not have."""
    cards = available_cards()
    assert A_CARD in cards and ANOTHER_CARD in cards, f"fixture is blind: cards={cards}"
    assert len(_all_slot_ids()) > 1, "fixture is blind: the slot schema resolved to fewer than 2 slots"
    assert load_context().strip(), "fixture is blind: load_context() with no selection read nothing"


# ── 1. an empty slot token resolved to every slot ────────────────────────────────


def test_empty_slot_token_is_refused_rather_than_matching_every_label():
    """`"" in label` is true for every label, so a stray empty token reported the entire model as
    changed with **zero** unmatched tokens — a total widening that reads as a precise answer."""
    # must fire: a real selection still works, and an unmatched token is still reported as unmatched
    assert resolve_slots(["workflow"]) == (["workflow"], [])
    assert resolve_slots(["permission"]) == (["permissions"], [])   # label substring still resolves
    assert resolve_slots(["workflow", "zzz"]) == (["workflow"], ["zzz"])

    # must not fire: an empty or whitespace token is a refusal, not a match against everything
    for token_list in ([""], ["  "], ["\t"], ["workflow", ""]):
        with pytest.raises(EmptySelectorTokenError):
            resolve_slots(token_list)


def test_an_empty_slot_token_does_not_report_the_whole_model_as_changed():
    """The two reachable shapes. `requivo impact <slug> ""` is what an unset shell variable expands
    to and reaches `resolve_slots` as `[""]`; a caller splitting a comma-separated value reaches it as
    a trailing `""`. Before the guard, both resolved to every slot in the schema with an empty
    unmatched list — a blast radius covering the whole model, reported as if it were a precise
    answer."""
    every = _all_slot_ids()
    assert len(every) > 1                       # must fire: there is an "everything" to widen to
    assert resolve_slots(["workflow"])[0] != sorted(every)   # must fire: a real query is narrow

    with pytest.raises(EmptySelectorTokenError) as ei:
        resolve_slots([""])                     # argv: requivo impact <slug> ""
    assert ei.value.details["position"] == 0
    with pytest.raises(EmptySelectorTokenError) as ei:
        resolve_slots("workflow,".split(","))   # a caller splitting a comma-separated value
    assert ei.value.details["position"] == 1    # names *which* token, not just that one was bad


# ── 2. a card selection that no longer resolves yielded zero product context ─────


def test_load_context_refuses_a_selection_that_matched_nothing():
    """`load_context` filtered by stem and joined; a name that matched nothing silently produced the
    empty string, and `build_prompt` spliced that into `{{CONTEXT}}` on every later turn."""
    # must fire: a real selection loads that card and only that card
    picked = load_context([A_CARD])
    assert f"## {A_CARD}" in picked and picked.strip()
    assert f"## {ANOTHER_CARD}" not in picked
    assert len(load_context()) > len(picked)    # the unrestricted load is still wider

    # must not fire: a name that resolves to nothing is a refusal, not an empty context
    with pytest.raises(UnknownContextCardError) as ei:
        load_context(["nonexistent-card"])
    assert ei.value.details["unknown"] == ["nonexistent-card"]

    # a partial miss is still a miss — the cards that *did* resolve must not mask the one that did not
    with pytest.raises(UnknownContextCardError) as ei:
        load_context([A_CARD, "nonexistent-card"])
    assert ei.value.details["unknown"] == ["nonexistent-card"]


def test_load_context_refuses_an_empty_selection_and_an_empty_token():
    # must fire: no selection at all still means every card, which is the documented contract
    assert load_context(None).strip()
    # must not fire: a selection object that selects nothing is not the same as no selection.
    # Since #35 this is `EmptySelectionError` — a sibling code, not the empty-*token* one.
    with pytest.raises(EmptySelectionError) as ei:
        load_context([])
    assert ei.value.details == {"selector": "context card", "tokens": 0}
    assert isinstance(ei.value, RequivoError)   # rides the same envelope as every clean failure
    with pytest.raises(EmptySelectorTokenError):
        load_context([""])


def test_both_card_selectors_echo_an_unknown_name_as_the_caller_typed_it():
    """`resolve_cards` and `load_context` are one design and their errors are read side by side, so
    the name in `details["unknown"]` is the one the caller wrote — not the lower-cased key it was
    matched by. `load_context` built that list from the normalized tokens and reported `some-card`
    for a typed `Some-Card`."""
    # must fire: a real name still resolves through both, so this is about the echo and not the match
    assert resolve_cards([f" {A_CARD.upper()} "]) == [A_CARD]      # matching stays case-insensitive
    assert f"## {A_CARD}" in load_context([A_CARD.upper()])

    for selector in (resolve_cards, load_context):
        with pytest.raises(UnknownContextCardError) as ei:
            selector(["  Some-Card  "])
        assert ei.value.details["unknown"] == ["Some-Card"], f"{selector.__name__} lower-cased the echo"


def test_a_persisted_card_selection_is_visible_when_the_card_is_gone(tmp_path, monkeypatch):
    """The reported scenario. A session created on machine A with a card from the user context
    directory, opened on machine B where that card does not exist — or after it is renamed. The
    selection is read back out of `session.json` on every turn and `resolve_cards` (the guard) ran
    once, at creation, so nothing else was ever going to notice."""
    user_dir = tmp_path / "user-cards"
    user_dir.mkdir()
    # Explicitly UTF-8, because `load_context` reads it that way. Without it this fixture is written
    # with the *locale's* codec, so on Windows the em dash below goes to disk as cp1252 and the read
    # at line 188 raises `UnicodeDecodeError` — a red leg caused by the harness, about a product that
    # is behaving correctly. That failure mode is worth naming: it is the test rendering an
    # environment limit as a product verdict, and "add more tests for that platform" is the wrong
    # lever on it (#3).
    (user_dir / "acme-crm.md").write_text(
        "ACME CRM — the product context this session was built on.", encoding="utf-8")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user_dir))

    # machine A: the card is there, the selection loads it
    assert "ACME CRM" in load_context(["acme-crm"])

    # machine B: the same session.json, no such card. That must be visible, not an empty string.
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(tmp_path / "no-user-cards"))
    with pytest.raises(UnknownContextCardError):
        load_context(["acme-crm"])


def test_build_prompt_never_silently_substitutes_an_empty_context():
    """The consequence that costs money: every provider call assembles its system prompt through
    `build_prompt`, so a stale selection reasoned with no product context at all — losing the
    `information_value = uncertainty x impact` driver — and was billed for it."""
    # must fire: a live selection reaches {{CONTEXT}}
    prompt = build_prompt("engine.md", [A_CARD])
    assert f"## {A_CARD}" in prompt and "{{CONTEXT}}" not in prompt
    # must not fire: a dead selection is a refusal, never a prompt with an empty context
    with pytest.raises(UnknownContextCardError):
        build_prompt("engine.md", ["nonexistent-card"])


# ── 3. resolve_cards had the same door ───────────────────────────────────────────


def test_resolve_cards_refuses_an_empty_token_instead_of_returning_all_cards():
    """`if not key: continue` dropped empty tokens, and a selection of *only* empty tokens then hit
    `return picked or None` — None, which every reader spells "every card". The widening the
    function's own docstring says it closes, reached through the empty-token entrance."""
    # must fire: the documented contract is unchanged
    assert resolve_cards([A_CARD, f" {ANOTHER_CARD}"]) == [A_CARD, ANOTHER_CARD]
    assert resolve_cards([]) is None                    # no selection at all == every card
    with pytest.raises(UnknownContextCardError) as ei:
        resolve_cards([A_CARD, "nope"])
    assert ei.value.details["unknown"] == ["nope"]

    # must not fire: an empty token no longer buys every card
    for token_list in ([""], [" "], [A_CARD, ""]):
        with pytest.raises(EmptySelectorTokenError):
            resolve_cards(token_list)


# ── the shared rule ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("selector, good", [
    pytest.param(resolve_cards, [A_CARD], id="resolve_cards"),
    pytest.param(load_context, [A_CARD], id="load_context"),
    pytest.param(resolve_slots, ["workflow"], id="resolve_slots"),
])
def test_every_selector_refuses_an_empty_token(selector, good):
    """One rule, one helper, three sites — so a fourth selector inherits it rather than re-deriving
    it. Stated behaviourally: whatever the selector is, an empty token is a refusal."""
    assert good[0] in repr(selector(good)), "must fire: the selector resolves a real token"
    with pytest.raises(EmptySelectorTokenError):
        selector([""])
    with pytest.raises(EmptySelectorTokenError):
        selector(["   "])


def test_the_refusal_is_a_structured_error_every_surface_can_render():
    """It rides the same envelope as every other clean failure: a stable code for `--json` and
    Claude Code, a 400 in the Web, one line in the CLI — never a traceback."""
    with pytest.raises(EmptySelectorTokenError) as ei:
        normalize_tokens(["ok", " "], what="context card")
    envelope = ei.value.to_dict()
    assert envelope["code"] == "empty_selector_token"
    assert envelope["details"] == {"selector": "context card", "position": 1}
    assert "context card" in envelope["message"]
    assert isinstance(ei.value, RequivoError)


def test_a_selector_handed_a_generator_does_not_silently_resolve_to_nothing():
    """The same absence, one layer down. `normalize_tokens` iterates the tokens it is given; a
    selector that then zips them against its own second pass would pair nothing if the caller handed
    in a generator, and return "no slots, no complaint" — this bug's own shape, introduced by fixing
    it. Both selectors that take a positional token list materialise first."""
    # must fire: a list still resolves, so the assertion below is about the generator and not the token
    assert resolve_slots(["workflow"]) == (["workflow"], [])
    assert resolve_slots(t for t in ["workflow"]) == (["workflow"], [])
    assert resolve_cards(t for t in [A_CARD]) == [A_CARD]
    # must not fire: the refusal still reaches a generator
    with pytest.raises(EmptySelectorTokenError):
        resolve_slots(t for t in ["workflow", ""])


def test_normalize_tokens_passes_real_tokens_through_stripped_and_lowercased():
    """The must-fire half of the helper: it is a normalizer, not only a refusal."""
    assert normalize_tokens([" Workflow ", "PERMISSIONS"], what="slot") == ["workflow", "permissions"]
    assert normalize_tokens([], what="slot") == []      # an empty list is not an empty token


# ── asking the same question without paying for it (#12) ─────────────────────────


def test_check_selection_answers_exactly_what_load_context_would_do():
    """`load_context` refuses a selection that no longer resolves — correct, and discovered at the
    next provider call, which costs money and happens minutes into a session. `check_selection` is
    that same guard asked as a question: it reports rather than raises, so `doctor` and
    `session verify` can answer it for free and in advance.

    The two must never drift, which is why `check_selection` runs the guard rather than
    reimplementing the rule. Both halves are asserted here: the selections that pass, and the ones
    that do not."""
    # must not fire — these are the selections that still load
    assert check_selection(None) is None            # None is the "every card" sentinel, not a selection
    assert check_selection([A_CARD]) is None
    assert check_selection([A_CARD, ANOTHER_CARD]) is None
    assert check_selection([A_CARD.upper()]) is None        # matched case-insensitively, like the loader

    # must fire — and it reports, it does not raise
    unknown = check_selection([A_CARD, "no-such-card"])
    assert isinstance(unknown, UnknownContextCardError)
    assert unknown.details["unknown"] == ["no-such-card"]
    assert unknown.to_dict()["code"] == "unknown_context_card"

    empty = check_selection([])
    assert isinstance(empty, EmptySelectionError), (
        "a persisted selection of nothing is refused by load_context; the checker must say so too")
    assert isinstance(check_selection([" "]), EmptySelectorTokenError)   # a token, not a selection


@pytest.mark.parametrize("selection", [None, [A_CARD], [A_CARD, ANOTHER_CARD], ["no-such-card"], []])
def test_check_selection_agrees_with_load_context_on_every_selection(selection):
    """The drift guard itself: for each selection, `check_selection` returning None must mean
    `load_context` succeeds, and returning a problem must mean it raises that same code. A checker
    that answers a slightly different question than the thing it checks is the defect this issue is
    about, one level up."""
    problem = check_selection(selection)
    try:
        load_context(selection)
    except RequivoError as e:
        assert problem is not None, f"load_context refused {selection!r} and check_selection did not"
        assert problem.code == e.code
    else:
        assert problem is None, f"check_selection refused {selection!r} and load_context did not"


# ── 4. an install with no cards at all (#33) ─────────────────────────────────────
#
# The wide instance the two narrow fixes left open. #24 closed the door where a *selection* resolves
# to nothing (`only=[]`, an unknown name); #26 taught `doctor` to tell `ok` from `empty` from
# `unreadable`. Neither is on the path `only=None` takes, and `only=None` is what a session with no
# card selection sends on every turn — so a wheel that lost `assets/context/` reasoned with an empty
# `{{CONTEXT}}`, on a call that costs money, and nothing said so.


def test_load_context_refuses_an_install_with_no_cards_at_all(zero_cards):
    """`load_context(None)` comprehended over an empty `_card_paths()` and returned `""`.

    The pre-existing test for this line was `assert load_context(None).strip()`, which is true on
    every machine that has cards installed — it pinned nothing about an install that has none.
    """
    # must not fire: an empty install is a refusal, not an empty context
    with pytest.raises(NoContextCardsError) as ei:
        load_context(None)
    assert ei.value.details["roots"], "the refusal must name where it looked"
    assert len(ei.value.details["roots"]) == 2, "both card roots are reported, not just the bundled one"

    # must fire: one card dropped into the same fixture and the same call succeeds — so the refusal
    # above is about the empty install, not about a harness that cannot read anything at all
    stem = _install_a_card(zero_cards)
    loaded = load_context(None)
    assert f"## {stem}" in loaded and "ACME CRM" in loaded


def test_the_zero_card_refusal_is_not_the_unreadable_one(zero_cards):
    """Two conditions, two codes, two remedies: `no_context_cards` means we looked and there are
    none (restore the install), `context_unreadable` means we could not look (fix the permissions).
    Collapsing them would send a reader to fix permissions on a directory that is perfectly
    readable and simply empty."""
    with pytest.raises(NoContextCardsError) as ei:
        load_context(None)
    assert ei.value.to_dict()["code"] == "no_context_cards"
    assert isinstance(ei.value, RequivoError)   # rides the same envelope as every clean failure
    # must fire: the message points at the install, not at a selection the caller made
    assert "install" in ei.value.message.lower()


def test_build_prompt_never_sends_an_empty_context_to_a_paid_call(zero_cards):
    """The consequence that costs money, and the reason this is a blocker rather than a tidiness
    fix. Every provider entry point assembles its system prompt through `build_prompt`; with no
    cards installed it substituted the empty string into `{{CONTEXT}}` with no check, so the engine
    reasoned with no product context at all — `information_value = uncertainty x impact`, the
    central design idea, silently off — and the call was billed anyway."""
    # must not fire: no paid call is assembled from an empty context
    with pytest.raises(NoContextCardsError):
        build_prompt("engine.md", None)

    # must fire: the same prompt assembles once the install is whole, and nothing is left unsubstituted
    stem = _install_a_card(zero_cards)
    prompt = build_prompt("engine.md", None)
    assert f"## {stem}" in prompt
    assert "{{CONTEXT}}" not in prompt and "{{SCHEMA}}" not in prompt


def test_a_selection_on_a_zero_card_install_names_the_install_not_the_card(zero_cards):
    """With no cards at all, every named card is 'unknown' — technically true and the wrong remedy.
    The reader is told to check the name they typed when the actual fault is that the install has
    no cards to match against. The wider condition is diagnosed first.

    The precedence is asserted rather than left implicit: `_require_any_card` runs *before*
    `_selection_keys`, so on an empty install even `load_context([])` — which would otherwise be
    `empty_selection` — reports the install. Which of the two guards wins is a decision, and a
    decision only a passing parametrized case covers is one nobody can find later.
    """
    with pytest.raises(NoContextCardsError):
        load_context(["acme-crm"])
    with pytest.raises(NoContextCardsError):
        load_context([])            # the install is diagnosed ahead of the empty selection

    # must fire: once there are cards, a genuinely unknown name is an unknown name again
    _install_a_card(zero_cards)
    with pytest.raises(UnknownContextCardError) as ei:
        load_context(["no-such-card"])
    assert ei.value.details["unknown"] == ["no-such-card"]


@pytest.mark.parametrize("selection", [None, ["acme-crm"], ["no-such-card"], []])
def test_check_selection_agrees_with_load_context_on_a_zero_card_install(zero_cards, selection):
    """The drift guard, run against the fixture that used to be missing.

    `check_selection` documents itself as returning 'the exact `RequivoError` `load_context` would
    raise'. It used to short-circuit `None` to `None` on the grounds that the every-card sentinel
    'cannot fail' — true until #33, false after it. A checker that says a session is fine while the
    call it predicts refuses is this issue's own defect class one level up, and it is what `doctor`
    and `session verify` report from.
    """
    problem = check_selection(selection)
    try:
        load_context(selection)
    except RequivoError as e:
        assert problem is not None, f"load_context refused {selection!r} and check_selection did not"
        assert problem.code == e.code
    else:
        assert problem is None, f"check_selection refused {selection!r} and load_context did not"


def test_check_selection_still_passes_a_healthy_install(zero_cards):
    """The must-fire half of the guard above: it must not report a problem once there is a card."""
    stem = _install_a_card(zero_cards)
    assert check_selection(None) is None
    assert check_selection([stem]) is None


def test_the_prompt_assembly_path_never_decodes_an_asset_with_the_locale_encoding():
    """`Path.read_text()` with no `encoding` decodes with the *locale's* encoding, not the file's.

    Every bundled card carries an em dash or an arrow, so on a cp1252 Windows console they decoded to
    mojibake and were sent to the provider that way — silently, since mojibake is still a string.
    Reproduced portably as a crash rather than a corruption: under `LC_ALL=C` (US-ASCII),
    `load_context(["b2b-platform"])` raised `UnicodeDecodeError: 'ascii' codec can't decode byte
    0xe2`.

    Asserted against the source rather than by forcing an encoding at runtime, because the mechanism
    is a *missing argument* and because the runtime reproduction is not available on every platform:
    `LC_ALL` does not move `getpreferredencoding` on Windows, so a functional test would pass there
    by not exercising anything — coverage claimed and not held, on the one leg that has the bug.
    """
    import ast
    from pathlib import Path

    # Parsed rather than grepped: this module's own comments discuss `read_text()` by name, and a
    # regex counted those as calls — a scan that fails on prose about the bug rather than on the bug.
    tree = ast.parse(Path(context_mod.__file__).read_text(encoding="utf-8"))
    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "read_text"]
    assert reads, "must fire: the scan found no read_text calls at all, so it proves nothing"
    bare = [n.lineno for n in reads if not any(k.arg == "encoding" for k in n.keywords)]
    assert not bare, (
        f"{context_mod.__file__} decodes an asset with the locale's encoding at line(s) {bare}")


# ── 5. one code carried two facts (#35) ──────────────────────────────────────────


def test_an_empty_token_and_an_empty_selection_are_two_codes():
    """`empty_selector_token` used to carry two different facts with two different `details` shapes:
    `{selector, position}` from `normalize_tokens`, and `{selector, tokens: 0}` from
    `_selection_keys` seventy lines later in the same pull request.

    `docs/compatibility.md` tells consumers to assert on the code, which is the right advice and is
    what makes this a contract defect: a consumer matching `empty_selector_token` and reading
    `details["position"]` got a `KeyError` from a payload that correctly carried the code it matched.
    """
    # an empty token *inside* a selection — the position is the actionable fact
    with pytest.raises(EmptySelectorTokenError) as token:
        normalize_tokens(["ok", " "], what="context card")
    assert token.value.details == {"selector": "context card", "position": 1}

    # a selection that is *itself* empty — a different fact, and now a different code
    with pytest.raises(EmptySelectionError) as selection:
        load_context([])
    assert selection.value.details == {"selector": "context card", "tokens": 0}

    # the two must not be confusable in either direction
    assert token.value.code != selection.value.code
    assert not isinstance(selection.value, EmptySelectorTokenError), (
        "an empty selection is not an empty token; a subclass would re-conflate what this splits")
    assert not isinstance(token.value, EmptySelectionError)


def test_every_empty_selector_token_payload_carries_a_position():
    """The consumer scenario from the issue, asserted as the invariant it wants: *every* payload
    carrying `empty_selector_token` has a `position` to read. That is what makes the documented
    advice — assert on the code — safe to follow."""
    reached = 0
    for selector in (resolve_cards, load_context, resolve_slots):
        for tokens in ([""], ["  "], ["workflow", ""]):
            with pytest.raises(EmptySelectorTokenError) as ei:
                selector(tokens)
            assert "position" in ei.value.details, f"{selector.__name__} omitted position"
            assert "tokens" not in ei.value.details, f"{selector.__name__} carries the other shape"
            reached += 1
    assert reached == 9, "must fire: every selector/token pair actually raised"

    # The discriminating half. Every case above reaches `normalize_tokens`, which has always set a
    # position — so without this the test would pass unchanged against the defect. The site that
    # broke the invariant is the one that never reaches `normalize_tokens`' raise at all: an empty
    # *list* normalizes to `[]` without complaint, and `_selection_keys` then raised the token code
    # with a `position`-less payload.
    with pytest.raises(RequivoError) as ei:
        load_context([])
    assert ei.value.code != "empty_selector_token", (
        "an empty selection still claims the token code, and its payload has no position: "
        f"{ei.value.details}")


def test_both_refusals_still_ride_the_structured_envelope():
    """Splitting the code must not cost either half its envelope — a stable code for `--json` and
    Claude Code, a clean status in the Web, one line in the CLI, never a traceback."""
    with pytest.raises(EmptySelectionError) as ei:
        load_context([])
    envelope = ei.value.to_dict()
    assert envelope["code"] == "empty_selection"
    assert envelope["details"] == {"selector": "context card", "tokens": 0}
    assert "context-card" in envelope["message"]    # the message names what was being selected
    assert isinstance(ei.value, RequivoError)


# ── a selector token that forges the receipt reporting it (#40) ──────────────────


def test_a_control_character_in_a_selector_token_is_refused_not_echoed():
    """#40. A selector token is caller text, and `.strip()` — all that ever touched one — removes
    surrounding whitespace and *not* interior newlines. Echoed into a diagnostic line, such a token
    writes its own lines into the receipt.

    The guard is here, in the one function all three selectors go through, rather than at the print
    sites: a render-site fix is per-site and per-site is how the second one gets forgotten. This is
    the same shape as `validate_slug` and `validate_filename` — refuse, and name the refused value
    in the escaped form so the refusal cannot forge the line reporting it.
    """
    # must fire: a well-formed token is untouched, on every selector
    assert normalize_tokens([A_CARD], what="context card") == [A_CARD]
    assert resolve_cards([A_CARD]) == [A_CARD]
    assert resolve_slots(["workflow"]) == (["workflow"], [])

    # must not fire: a control character is a refusal, not something echoed back
    for token in ("ok-card\nAll clear.", "a\rb", "a\tb", "a\x1b[2Kb", "a\x00b", "a\x9bb"):
        for call in (lambda t: normalize_tokens([t], what="context card"),
                     lambda t: resolve_cards([t]),
                     lambda t: load_context([t]),
                     lambda t: resolve_slots([t])):
            with pytest.raises(UnsafeSelectorTokenError):
                call(token)

    # The offending value is named, and named in escaped form — the message must not be able to
    # forge the line that carries it.
    with pytest.raises(UnsafeSelectorTokenError) as ei:
        resolve_cards(["ok-card\nAll clear."])
    assert "\n" not in ei.value.message, "the refusal itself carries a line break"
    assert "ok-card" in ei.value.message, "the refusal does not say which token"
    assert ei.value.code == "unsafe_selector_token"
    assert ei.value.details == {"selector": "context card", "position": 0}


def test_check_selection_reports_a_hostile_persisted_card_rather_than_raising():
    """`check_selection` is what `doctor` and `session verify` ask, and its contract is *reported,
    never raised* — a health check that raises takes the whole listing down with it (invariant 15).
    A stored selection is exactly where a hostile card name arrives, so the new refusal has to join
    the reported set rather than escape it.

    A hostile name can only ever arrive *persisted*: `create_session` resolves the selection against
    the installed cards (invariant 14), so the doors are `session import` and a hand-edited
    `session.json`, and the first code to look at one is therefore a health check. Escaping from here
    would mean `doctor` answering nothing at all about a workspace holding one tampered session,
    rather than degrading that one row. This is why `UnsafeSelectorTokenError` is a member of
    `context._SELECTION_REFUSALS` (#40)."""
    assert check_selection([A_CARD]) is None            # must fire: a good selection is still clean

    problem = check_selection(["ok-card\nAll clear."])
    assert problem is not None and problem.code == "unsafe_selector_token"
    assert "\n" not in problem.message


def test_the_two_card_code_tables_agree():
    """`deterministic.doctor._RESTORABLE_CARD_CODES` decides which remedy `doctor` and `session verify`
    print, by matching `problem["code"]`. It can only ever see codes `check_selection` **returns**,
    which is exactly `_SELECTION_REFUSALS` — so a member outside that set is a branch that cannot
    run, and a returned code outside both tables silently gets the repair hint.

    Pinned structurally rather than by making a card directory unreadable: that needs a chmod, which
    does nothing when the test runs as root and behaves differently on Windows, so the assertion
    would be vacuous on exactly the legs nobody re-reads. This asks the same question of the two
    tables directly and answers it identically everywhere.
    """
    returnable = {e.code for e in _SELECTION_REFUSALS}
    assert returnable, "must fire: the refusal tuple is not empty"
    assert _RESTORABLE_CARD_CODES <= returnable, (
        "a remedy is routed on a code check_selection can never return: "
        f"{sorted(_RESTORABLE_CARD_CODES - returnable)}")
    # And the reverse, as documentation of the split rather than as a second guard: everything else
    # `check_selection` can return is a malformed *selection*, which restoring a file cannot fix.
    assert returnable - _RESTORABLE_CARD_CODES == {
        "empty_selection", "empty_selector_token", "unsafe_selector_token"}
    assert ContextUnreadableError not in _SELECTION_REFUSALS, (
        "context_unreadable is now returned rather than propagated; _RESTORABLE_CARD_CODES and "
        "_card_health's third state both need revisiting")


def test_display_token_is_the_render_side_companion_where_no_selector_runs():
    """`session show` prints a stored card name without selecting anything with it, so the guard
    above never runs on that path. `display_token` is what such a site uses: a value that is already
    one safe line is returned byte-for-byte (so ordinary output is unchanged), and only a value that
    could break the line is quoted and escaped."""
    assert display_token(A_CARD) == A_CARD              # must fire: no quotes on a clean name
    assert display_token("Ünïcode-cárd") == "Ünïcode-cárd"   # non-ASCII is not "unsafe"

    shown = display_token("ok-card\nAll clear.")
    assert "\n" not in shown and "ok-card" in shown


# ── the ordinary path: which cards load, and which one wins ──────────────────


def test_load_context_includes_real_cards_and_skips_underscore():
    ctx = load_context()
    assert "## b2b-platform" in ctx    # committed context card is included
    assert "## _template" not in ctx   # underscore-prefixed card is skipped


def test_load_context_refuses_when_only_underscore_files_are_present(tmp_path, monkeypatch):
    """A directory holding nothing but `_`-prefixed files has **no cards**, and since #33 that is a
    refusal rather than an empty string.

    This test used to assert `load_context() == ""`, which is the defect #33 reports written down as
    the intended contract: an empty `{{CONTEXT}}` reached `build_prompt` and was sent to a paid call
    with the product context silently missing. What it is still here to pin is the other half — that
    an underscore-prefixed file does not count as a card — so the refusal below is the *right*
    refusal and not an accident of an empty directory.
    """
    from requivo.core import context as llm
    from requivo.core.errors import NoContextCardsError

    ctx_dir = tmp_path / "context"
    ctx_dir.mkdir()
    (ctx_dir / "_only_template.md").write_text("skip me", encoding="utf-8")
    monkeypatch.setattr(llm, "CONTEXT", ctx_dir)
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(tmp_path / "no-user-cards"))

    with pytest.raises(NoContextCardsError):
        load_context()

    # must fire: the same directory with one *real* card loads, so the refusal above is about the
    # underscore rule and not about the directory being unreadable or the anchor being wrong
    (ctx_dir / "real-card.md").write_text("REAL CARD", encoding="utf-8")
    loaded = load_context()
    assert "## real-card" in loaded and "## _only_template" not in loaded


def test_user_context_cards_merge_and_override_bundled(tmp_path, monkeypatch):
    # A pip-installed user extends discovery by dropping cards in REQUIVO_CONTEXT_DIR: new stems are added,
    # and a stem matching a bundled card overrides it (tweak a built-in without editing the package).
    from requivo.core import context as llm

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "b2b-platform.md").write_text("BUNDLED b2b")
    (bundled / "shared.md").write_text("BUNDLED shared")
    monkeypatch.setattr(llm, "CONTEXT", bundled)

    user = tmp_path / "user"
    user.mkdir()
    (user / "my-product.md").write_text("USER product")
    (user / "shared.md").write_text("USER shared override")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user))

    assert llm.available_cards() == ["b2b-platform", "my-product", "shared"]  # merged, sorted
    ctx = load_context()
    assert "BUNDLED b2b" in ctx            # bundled-only card kept
    assert "USER product" in ctx           # user-only card added
    assert "USER shared override" in ctx   # user card wins on stem clash
    assert "BUNDLED shared" not in ctx      # ...replacing the bundled version


def test_available_cards_lists_real_non_underscore_cards():
    from requivo.core.context import available_cards

    cards = available_cards()
    assert "b2b-platform" in cards and "financial-reporting" in cards
    assert all(not c.startswith("_") for c in cards)


def test_load_context_only_filters_to_selected_cards():
    from requivo.core.context import load_context

    ctx = load_context(only=["b2b-platform"])
    assert "## b2b-platform" in ctx
    assert "## financial-reporting" not in ctx  # a non-selected card is excluded
    assert load_context() != ctx                # default still loads everything


def test_resolve_cards_maps_stems_and_refuses_an_unknown_one():
    # One resolver in Core, shared by every surface. An unknown card is an error rather than a warning
    # you can walk past: dropping it leaves an empty selection, and an empty selection means *all*
    # cards — so a typo would widen the context instead of narrowing it.
    from requivo.core.context import resolve_cards
    from requivo.core.errors import UnknownContextCardError

    assert resolve_cards(["b2b-platform", " financial-reporting"]) == ["b2b-platform", "financial-reporting"]
    assert resolve_cards([]) is None                      # no selection == every card, explicitly
    with pytest.raises(UnknownContextCardError) as ei:
        resolve_cards(["b2b-platform", "nope"])
    assert ei.value.details["unknown"] == ["nope"]
