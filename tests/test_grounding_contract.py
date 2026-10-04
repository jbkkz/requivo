"""The product context a session is grounded on: an empty install refuses at creation (#41), the grounding
judgment rides the claim seam (#593), and every surface names the cards (#492)."""
from __future__ import annotations

import builtins
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _fakes import _ENGINE_REPLY, _ROUTING_REPLY, FakeClient, StubProvider, card, full_model, run_cli
from fastapi.testclient import TestClient

from conftest import FakeProvider
from requivo.core import context as context_mod
from requivo.core.context import (
    CardSummary,
    available_cards,
    card_summaries,
    check_selection,
    load_context,
    resolve_cards,
    write_generated_card,
)
from requivo.core.contracts import ContextJudgment, GeneratedCard
from requivo.core.errors import (
    EmptySelectorTokenError,
    NoContextCardsError,
    ProviderOutputError,
    RequivoError,
    SessionUnreadableError,
    UnknownContextCardError,
    UnsafeSelectorTokenError,
)
from requivo.paths import card_draft_root
from requivo.providers.anthropic.generators import judge_context
from requivo.render.terminal import render_grounding
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.app import create_app
from requivo.web.viewmodels.status import grounding_view

pytestmark = pytest.mark.usefixtures("workspace")

A_NAME = "acme-crm"          # a card that exists only once the fixture installs it
NOT_A_CARD = "no-such-card"  # a name that is wrong even on a healthy install
CARD = "event-ops"
SLUG = "grounded"


# ── an install with no cards at all (#41) ──────────────────────────────────────


@pytest.fixture
def zero_cards(tmp_path, monkeypatch):
    """Both card roots exist, both are readable, both are empty."""
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


def _raise(problem: RequivoError | None) -> None:
    """`check_selection` reports rather than raises, deliberately."""
    if problem is not None:
        raise problem


def test_resolve_cards_on_a_zero_card_install_names_the_install_not_the_card(zero_cards):
    """With nothing installed, every name is "unknown"; the refusal names both roots it looked in."""
    with pytest.raises(NoContextCardsError) as ei:
        resolve_cards([A_NAME])
    assert ei.value.to_dict()["code"] == "no_context_cards"
    assert len(ei.value.details["roots"]) == 2

    stem = _install_a_card(zero_cards)  # must fire: one card in the same roots, and unknown is unknown again
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
    with pytest.raises(NoContextCardsError):
        selector([A_NAME])
    stem = _install_a_card(zero_cards)  # must fire, both halves, with the *narrow* code
    selector([stem])
    with pytest.raises(UnknownContextCardError):
        selector([NOT_A_CARD])


def test_the_install_is_diagnosed_ahead_of_a_malformed_token_too(zero_cards):
    """Which guard wins is a decision, asserted rather than left implicit (#33)."""
    for malformed in ([""], ["  "], ["ok-card\nAll clear."]):
        with pytest.raises(NoContextCardsError):
            resolve_cards(malformed)
    _install_a_card(zero_cards)
    with pytest.raises(EmptySelectorTokenError):
        resolve_cards([""])
    with pytest.raises(UnsafeSelectorTokenError):
        resolve_cards(["ok-card\nAll clear."])


def test_creating_a_session_on_a_zero_card_install_refuses_at_creation(zero_cards):
    """Invariant 14: the service layer is the integrity boundary, not the interfaces."""
    with pytest.raises(NoContextCardsError):
        SessionService().create_session("A leave approval system.", context_cards=[A_NAME])
    stem = _install_a_card(zero_cards)
    assert SessionService().create_session("A leave approval system.", context_cards=[stem]).context_cards == [stem]


def test_no_selection_at_all_is_still_no_selection(zero_cards):
    """The deliberate non-change: `None` means "every card", refused where the cards are needed."""
    assert resolve_cards([]) is None
    with pytest.raises(NoContextCardsError):
        load_context(None)
    stem = _install_a_card(zero_cards)
    assert resolve_cards([]) is None
    assert f"## {stem}" in load_context(None)


# ── the grounding judgment (#593, `decision: the-engine-writes-the-missing-card`) ─


def test_a_judgment_naming_a_card_the_install_does_not_have_is_refused():
    """An invented card name would reach `resolve_cards` as a selection and refuse the discovery it grounds (#593)."""
    cards = [CardSummary(stem="b2b-platform", domain="enterprise management")]
    invented = json.dumps({"decision": "installed", "reason": "r", "cards": ["dentistry-es"]})
    client = FakeClient(invented, invented, invented)
    with pytest.raises(ProviderOutputError):  # a `RequivoError` sibling of `EngineError`, not a subclass
        judge_context(client, "a request", cards)
    assert len(client.calls) == 3, "the correction did not ride the retry loop"

    good = json.dumps({"decision": "installed", "reason": "r", "cards": ["b2b-platform"]})
    assert judge_context(FakeClient(good), "a request", cards).cards == ["b2b-platform"]


def test_the_judgment_prompt_carries_neither_the_schema_nor_the_cards():
    """Its whole economy is asking about ~9k of context for the price of a few hundred tokens (#593)."""
    from requivo.core.context import SHARED_PROMPT_HEAD

    client = FakeClient(json.dumps({"decision": "none", "reason": "r"}))
    judge_context(client, "a leave approval system", [CardSummary(stem="b2b-platform", domain="d")])
    system = client.calls[0]["system"]
    text = system if isinstance(system, str) else "".join(b["text"] for b in system)
    assert not text.startswith(SHARED_PROMPT_HEAD[:40])
    assert "# Model schema" not in text, "the judgment call is paying for the schema"
    assert "a leave approval system" in text and "b2b-platform" in text


class _Judge(StubProvider):
    """A `ReasoningProvider` that also answers grounding questions."""

    name = "judging-stub"

    def __init__(self, judgment=None):
        super().__init__()
        self.judgment = judgment or ContextJudgment(decision="none", reason="ordinary software")
        self.asked: list[list] = []

    def judge_context(self, request, *, cards):
        self.asked.append(cards)
        return self.judgment


_NARROWS = ContextJudgment(decision="installed", reason="finance", cards=["financial-reporting"])


def test_an_explicit_card_selection_is_not_second_guessed():
    """A `--context` is a human decision; re-examining it would agree at cost or disagree with no remedy."""
    judge = _Judge()
    grounding = DiscoveryService(judge).judge_grounding("a request", cards=["b2b-platform"])
    assert judge.asked == [], "the judgment was billed over a selection the user had already made"
    assert grounding.judgment is None and "--context" in grounding.why_not


def test_a_provider_that_cannot_judge_reports_not_asked_rather_than_no_card_needed():
    """`ContextJudge` is a protocol a provider may simply not implement (#492)."""
    grounding = DiscoveryService(FakeProvider()).judge_grounding("a request", cards=None)
    assert grounding.judgment is None, "a provider that cannot judge produced a verdict anyway"
    assert grounding.why_not, "not asked, and it did not say why"


def test_the_judgment_reaches_the_provider_with_one_line_per_installed_card():
    """The summaries are read in `core`, so the provider cannot answer about a different set of cards."""
    judge = _Judge()
    DiscoveryService(judge).judge_grounding("a request", cards=None)
    assert len(judge.asked) == 1
    assert [c.stem for c in judge.asked[0]] == sorted(available_cards())


def test_an_install_with_no_cards_is_not_judged_as_needing_none(monkeypatch):
    """`load_context` refuses this install outright a moment later."""
    from requivo.services import discovery as disco_mod

    monkeypatch.setattr(disco_mod, "card_summaries", list)
    judge = _Judge()
    grounding = DiscoveryService(judge).judge_grounding("a request", cards=None)
    assert judge.asked == [], "an install with nothing to judge against was still billed"
    assert grounding.judgment is None and "no context cards" in grounding.why_not


def test_a_narrowing_verdict_reclaims_under_the_narrowed_identity():
    """The selection is half a session's identity (invariant 11), so acting on `installed` cannot be an edit."""
    meta, grounding, cards, _routing = DiscoveryService(_Judge(_NARROWS)).claim_and_ground(
        "a billing request", cards=None, slug=None)
    assert cards == ["financial-reporting"] and meta.context_cards == ["financial-reporting"]
    assert grounding.judgment.decision.value == "installed"
    assert len(SessionService().list_sessions()) == 1, "the widened claim was left behind"


def test_a_session_this_call_did_not_create_is_never_deleted_by_a_verdict():
    """`create_session_report`'s boolean is the whole authorisation for the delete."""
    svc = SessionService()
    first = svc.create_session("a billing request")
    meta, _grounding, cards, _routing = DiscoveryService(_Judge(_NARROWS)).claim_and_ground(
        "a billing request", cards=None, slug=None)
    assert meta.slug == first.slug, "an idempotent re-entry landed somewhere else"
    assert cards is None, "a session this call did not create was narrowed anyway"
    assert svc.exists(first.slug), "a session this call did not create was deleted"


def test_a_session_that_moved_off_revision_zero_during_the_judgment_is_left_alone():
    """`created` was true a call ago, and a call ago is long enough for a model to have landed."""
    svc = SessionService()

    class _WritesMidJudgment(_Judge):
        def judge_context(self, request, *, cards):
            svc.update_model(svc.list_sessions()[0].slug, full_model())  # a concurrent writer
            return super().judge_context(request, cards=cards)

    meta, _grounding, cards, _routing = DiscoveryService(_WritesMidJudgment(_NARROWS)).claim_and_ground(
        "a billing request", cards=None, slug=None)
    assert svc.exists(meta.slug), "a session with a model in it was deleted on a verdict"
    assert cards is None, "the narrowing went ahead over a session that had moved on"
    assert svc.list_sessions()[0].current_revision == 1, "the concurrent write was lost"


# ── the missing card (#598, `decision: the-engine-writes-the-missing-card`) ────


@pytest.fixture
def user_cards(tmp_path, monkeypatch):
    """The user card root a written card lands in, never the developer's own."""
    user = tmp_path / "user-cards"
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user))
    return user


def _uncovered(**overrides) -> ContextJudgment:
    return ContextJudgment(decision="uncovered", reason="dental billing", card=GeneratedCard(**card(**overrides)))


def test_an_uncovered_verdict_writes_its_card_and_reclaims_under_it_alone(user_cards):
    """Unsaved, the card stays in the workspace and grounds its session alone (invariant 11)."""
    meta, grounding, cards, _routing = DiscoveryService(_Judge(_uncovered())).claim_and_ground(
        "a dental billing request", cards=None, slug=None)
    assert cards == ["dental-billing"] and meta.context_cards == ["dental-billing"]
    assert grounding.written == card_draft_root() / "dental-billing.md" and not grounding.saved
    assert grounding.written.read_text(encoding="utf-8") == grounding.judgment.card.markdown()
    assert not user_cards.exists(), "a card left the workspace without consent"
    assert len(SessionService().list_sessions()) == 1, "the every-card claim was left behind"
    # A re-run writing the same card lands on the same session: one card, one identity.
    again, _g, _c, _r = DiscoveryService(_Judge(_uncovered())).claim_and_ground(
        "a dental billing request", cards=None, slug=None)
    assert again.slug == meta.slug and len(SessionService().list_sessions()) == 1, "a re-run forked the session"


def test_an_unsaved_card_grounds_its_session_alone(user_cards):
    """Every consumer resolves it through the one lookup; the every-card default and a judgment never see it."""
    DiscoveryService(_Judge(_uncovered())).claim_and_ground("a dental billing request", cards=None, slug=None)
    assert "## dental-billing" in load_context(["dental-billing"]) and check_selection(["dental-billing"]) is None
    assert resolve_cards(["dental-billing"]) == ["dental-billing"], "rescope and session init cannot name it"
    assert "## dental-billing" not in load_context(None), "an unsaved card diluted the every-card default"
    assert "dental-billing" not in available_cards() and all(c.stem != "dental-billing" for c in card_summaries())


def test_a_written_card_is_kept_only_on_consent(user_cards):
    """`save_card` writes to the user root; keeping an unsaved one later moves it without moving its sessions."""
    _meta, grounding, cards, _r = DiscoveryService(_Judge(_uncovered())).claim_and_ground(
        "a dental billing request", cards=None, slug=None, save_card=True)
    assert grounding.saved and grounding.written == user_cards / "dental-billing.md" and cards == ["dental-billing"]
    assert CardSummary("dental-billing", "dental practice billing in Spain") in card_summaries(), "kept, not reusable"
    disco = DiscoveryService(_Judge(_uncovered(stem="dental-claims")))
    meta, unsaved, _c, _r = disco.claim_and_ground("a dental claims request", cards=None, slug=None)
    kept = disco.keep_card(unsaved.written.stem)
    assert kept == user_cards / "dental-claims.md"
    assert kept.read_text(encoding="utf-8") == unsaved.written.read_text(encoding="utf-8"), "kept a different card"
    assert SessionService().meta(meta.slug).context_cards == ["dental-claims"], "keeping moved the session"
    assert "## dental-claims" in load_context(["dental-claims"])
    with pytest.raises(ValueError, match="already installed"):
        disco.keep_card("dental-claims")
    with pytest.raises(ValueError, match="no unsaved card"):
        disco.keep_card("dental-nothing")


def test_keeping_a_card_never_pulls_it_from_under_a_reader(user_cards):
    """A session that looked the card up before it was kept still reads it after (#765): the draft stays."""
    disco = DiscoveryService(_Judge(_uncovered()))
    disco.claim_and_ground("a dental billing request", cards=None, slug=None)
    looked_up = context_mod._cards_for_selection()  # a concurrent turn, between its lookup and its read
    disco.keep_card("dental-billing")
    assert "dental practice billing in Spain" in looked_up["dental-billing"].read_text(encoding="utf-8")
    assert load_context(["dental-billing"]).count("## dental-billing") == 1, "the kept card loads twice"


def test_an_unreadable_unsaved_card_root_does_not_flag_an_unscoped_session():
    """Invariant 8: `load_context(None)` never reads `.requivo/cards`, so neither does its diagnostic (#765)."""
    card_draft_root().parent.mkdir(parents=True, exist_ok=True)
    card_draft_root().write_text("not a directory", encoding="utf-8")
    assert load_context(None) and check_selection(None) is None


def test_a_written_card_never_shadows_an_installed_one(user_cards, monkeypatch):
    """A taken name moves to the next free suffix; nothing installed or already written is replaced."""
    meta, grounding, cards, _routing = DiscoveryService(_Judge(_uncovered(stem=CARD))).claim_and_ground(
        "a dental billing request", cards=None, slug=None)
    assert cards == [f"{CARD}-2"] and grounding.written.name == f"{CARD}-2.md"
    assert f"## {CARD}-2" in load_context(cards) and "on-site event operations" not in load_context(cards)
    # A different card already written under the name, even one landing after the check, is left whole.
    drafts = card_draft_root()
    drafts.joinpath("dental-billing.md").write_text("someone else's card", encoding="utf-8")
    monkeypatch.setattr(context_mod, "_draft_paths", dict)
    assert write_generated_card(GeneratedCard(**card()), keep=False) == drafts / "dental-billing-2.md"
    assert drafts.joinpath("dental-billing.md").read_text(encoding="utf-8") == "someone else's card"
    assert not [p for p in drafts.iterdir() if p.suffix == ".tmp"], "scratch was left behind"


def test_an_identical_draft_is_not_reused_once_an_installed_card_owns_its_stem(user_cards):
    """Installed cards win the lookup, so reusing that draft would show one card and reason on another (#765)."""
    drafted = write_generated_card(GeneratedCard(**card()), keep=False)
    user_cards.mkdir()
    user_cards.joinpath("dental-billing.md").write_text("- Business domain: someone else's", encoding="utf-8")
    again = write_generated_card(GeneratedCard(**card()), keep=False)
    assert again == drafted.with_name("dental-billing-2.md")
    assert "dental practice billing in Spain" in load_context([again.stem])


def _denied(_path):
    raise SessionUnreadableError("could not create the card root: denied")


def test_a_card_root_the_store_cannot_create_keeps_every_card_rather_than_failing(monkeypatch):
    """`ensure_store_dir` wraps the denial; after a paid judgment it must degrade, not abort (#765)."""
    monkeypatch.setattr(context_mod, "ensure_store_dir", _denied)
    meta, grounding, cards, _routing = DiscoveryService(_Judge(_uncovered())).claim_and_ground(
        "a dental billing request", cards=None, slug=None)
    assert cards is None and meta.context_cards is None and grounding.written is None
    assert "not written" in grounding.note and "denied" in grounding.note


def test_an_uncovered_verdict_on_a_session_this_call_did_not_create_writes_nothing(user_cards):
    """`installed`'s preconditions hold for the write too: a card is only written for the session it narrows."""
    first = SessionService().create_session("a dental billing request")
    meta, grounding, cards, _routing = DiscoveryService(_Judge(_uncovered())).claim_and_ground(
        "a dental billing request", cards=None, slug=None)
    assert meta.slug == first.slug and cards is None and grounding.written is None
    assert "earlier run" in grounding.note and not user_cards.exists() and not card_draft_root().exists()


_UNCOVERED_REPLY = json.dumps({"decision": "uncovered", "reason": "dental billing in Spain", "card": card()})


def test_a_first_discover_shows_the_written_card_then_reasons_against_it_alone(user_cards):
    """#598's done-when: an off-domain first run writes a card, shows it, and reasons the turn on it alone."""
    client = FakeClient(_ROUTING_REPLY, _UNCOVERED_REPLY, _ENGINE_REPLY)
    text = run_cli(["discover", "a dental billing request", "--once"], client=client)
    assert "- Business domain: dental practice billing in Spain" in text and str(card_draft_root()) in text
    system = client.calls[2]["system"]
    prompt = system if isinstance(system, str) else "".join(b["text"] for b in system)
    assert "## dental-billing" in prompt and f"## {CARD}" not in prompt, "the turn was not scoped to the card"
    assert [s.context_cards for s in SessionService().list_sessions()] == [["dental-billing"]]
    assert not user_cards.exists() and "Save this card" not in text, "no terminal, no flag: never kept"


@pytest.mark.parametrize("answer, argv_tail, kept", [
    ("y", [], True), ("", [], False), (None, ["--save-card"], True),
], ids=["yes-at-a-terminal", "default-no", "flag-without-a-terminal"])
def test_a_written_card_is_kept_only_on_consent_at_the_cli(user_cards, monkeypatch, answer, argv_tail, kept):
    """The question is the CLI's, asked before the turn; the flag answers it where nobody can (#598)."""
    asked: list[str] = []
    if answer is not None:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(builtins, "input", lambda prompt="": asked.append(prompt) or answer)
    client = FakeClient(_ROUTING_REPLY, _UNCOVERED_REPLY, _ENGINE_REPLY)
    run_cli(["discover", "a dental billing request", "--once", *argv_tail], client=client)
    assert user_cards.joinpath("dental-billing.md").exists() is kept
    assert kept or card_draft_root().joinpath("dental-billing.md").exists(), "a declined card was not written"
    assert len(asked) == (1 if answer is not None else 0) and all("[y/N]" in p for p in asked)
    assert [s.context_cards for s in SessionService().list_sessions()] == [["dental-billing"]]


def test_a_judgment_writing_a_card_over_an_installed_stem_rides_the_retry_loop():
    """The provider refuses the collision before the service has to (#598)."""
    cards = [CardSummary(stem="b2b-platform", domain="enterprise management")]
    taken = json.dumps({"decision": "uncovered", "reason": "r", "card": card(stem="b2b-platform")})
    client = FakeClient(taken, taken, taken)
    with pytest.raises(ProviderOutputError):
        judge_context(client, "a request", cards)
    assert len(client.calls) == 3, "the correction did not ride the retry loop"
    good = json.dumps({"decision": "uncovered", "reason": "r", "card": card()})
    assert judge_context(FakeClient(good), "a request", cards).card.stem == "dental-billing"


# ── every surface names the cards (#492) ───────────────────────────────────────


@pytest.fixture(autouse=True)
def _proposal(tmp_path) -> Path:
    """A complete model on disk -- `model apply` takes a path, not inline JSON."""
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(full_model()), encoding="utf-8")
    return path


def _seed(cards: list[str] | None, proposal: Path) -> None:
    """A session at revision 1, grounded either on one named card or on nothing in particular."""
    argv = ["session", "init", "A leave approval request", "--slug", SLUG]
    if cards is not None:
        argv += ["--context", ",".join(cards)]
    run_cli(argv)
    run_cli(["model", "apply", SLUG, str(proposal)])


def _terminal_status(cards, proposal):
    _seed(cards, proposal)
    return run_cli(["status", SLUG])


def _terminal_session_show(cards, proposal):
    _seed(cards, proposal)
    return run_cli(["session", "show", SLUG])


def _web_primary_screen(cards, proposal):
    """The rendered page, cut at *Traceability details* (#492)."""
    _seed(cards, proposal)
    client = TestClient(create_app(), base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    response = client.get(f"/sessions/{SLUG}")
    assert response.status_code == 200, f"the session page answered {response.status_code}"
    marker = response.text.find("Traceability")
    return response.text[:marker] if marker != -1 else response.text


def _terminal_renderer(cards, _proposal):
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_grounding(cards)
    return buf.getvalue()


def _wipe(workspace: Path) -> None:
    """Remove the seeded session so the next `session init` in the same test can claim the slug again."""
    import shutil
    shutil.rmtree(workspace / ".requivo", ignore_errors=True)


SURFACES = [("terminal status", _terminal_status), ("terminal session show", _terminal_session_show),
            ("web primary screen", _web_primary_screen), ("terminal renderer", _terminal_renderer)]
_IDS = [s for s, _ in SURFACES]


@pytest.mark.parametrize("surface,render", SURFACES, ids=_IDS)
def test_every_surface_names_the_cards_a_session_was_grounded_on(surface, render, _proposal, tmp_path):
    """The must-fire half: a narrowed and an unnarrowed grounding never render the same way."""
    narrowed = render([CARD], _proposal)
    assert CARD in narrowed, f"{surface} renders a session's state without naming what it was reasoned against"
    _wipe(tmp_path)
    assert narrowed != render(None, _proposal), f"{surface} says the same thing narrowed or on every card"


def test_the_view_model_states_which_of_the_two_it_is_rather_than_leaving_it_to_the_template():
    """`narrowed` is a fact about the session; the template's job is to word it."""
    assert grounding_view({"context_cards": [CARD]}) == {"narrowed": True, "readable": True, "cards": [CARD]}
    unnarrowed = grounding_view({"context_cards": None})
    assert unnarrowed["narrowed"] is False
    assert CARD in unnarrowed["cards"], "an unnarrowed session is grounded on every card in the install"


@pytest.mark.parametrize("surface,render", [s for s in SURFACES if s[0] != "terminal session show"],
                         ids=[s for s in _IDS if s != "terminal session show"])
def test_an_unreadable_card_directory_degrades_the_grounding_line_rather_than_the_verb(surface, render, _proposal, monkeypatch):
    """The third state, found in review of #518: the surfaces that enumerate the install."""
    from requivo.core.errors import ContextUnreadableError

    def _refuse():
        raise ContextUnreadableError("the card directory could not be enumerated", details={})

    monkeypatch.setattr(context_mod, "available_cards", _refuse)
    rendered = render(None, _proposal)
    assert "Product context" in rendered, f"{surface} dropped the grounding line entirely rather than degrading it"
    assert "could not be read" in rendered, f"{surface} must not render an unreadable directory as 'no cards'"
    assert "session verify" not in rendered, f"{surface} points at a session remedy for an install-level fact"


def test_no_surface_claims_a_relevance_verdict_it_cannot_reach(_proposal, tmp_path):
    for cards in ([CARD], None):
        for _, render in SURFACES:
            _wipe(tmp_path)
            rendered = render(cards, _proposal).lower()
            for word in ("mismatched", "irrelevant", "wrong product", "not relevant"):
                assert word not in rendered, f"a surface renders {word!r}: relevance is a judgment no surface can reach"
