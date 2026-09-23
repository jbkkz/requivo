"""The rendered page must not announce the client's language as English (#277)."""
from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser

import pytest

from requivo.services.sessions import SessionService
from tests.web.conftest import HIGH_EXPLICIT, HIGH_INFERRED, full_slots

# A French request and a French reasoning turn: the shape #277 is about.
REQUEST_FR = "Nous aimerions que les managers approuvent les demandes de congé des employés."
OBJECTIVE_FR = "Permettre aux managers d'approuver les demandes de congé, avec une escalade."
SCOPE_FR = "Un circuit d'approbation configurable par client."
ASSUMPTION_FR = "Nous supposons que le manager direct est l'approbateur par défaut."
BLIND_SPOT_FR = "Personne n'a dit ce qui se passe quand le manager est déjà absent."
QUESTION_FR = "Qui approuve lorsque le manager est lui-même en congé ?"
WHY_FR = "Une escalade non définie change le circuit et le coût du développement."
# English chrome rendered by the same templates, and the brief's own content, English on a French session.
CHROME = ("What Requivo understood", "Are we ready?", "The request", "Why it matters")
DECISION_EN = "Escalation is time-based rather than delegated"
CHALLENGE_EN = "Fixed five-day escalation"
# Six questions is the contract's own ceiling and one more than `PRIORITY_QUESTIONS`.
OVERFLOW_SLOTS = ("problem", "workflow", "permissions", "integrations", "edge_cases", "business_rules")
OVERFLOW_Q_FR = "Que fait-on d'une tournée annulée après la facturation ?"
OVERFLOW_WHY_FR = "Le rattrapage facturé deux fois est le coût que personne n'a chiffré."


class _LangRegions(HTMLParser):
    """The page's text split by whether an enclosing element declared `lang=""` (unknown) or inherited the document's."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[bool] = []
        self.unknown_language: list[str] = []
        self.inherited_language: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        inside = bool(self._stack and self._stack[-1]) or dict(attrs).get("lang") == ""
        if tag not in ("br", "hr", "img", "input", "meta", "link"):   # void elements never close
            self._stack.append(inside)

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            (self.unknown_language if self._stack and self._stack[-1] else self.inherited_language).append(text)


def _regions(page: str) -> tuple[str, str]:
    """`(tagged, untagged)`: the text under an unknown-language region and the text inheriting the document's."""
    parser = _LangRegions()
    parser.feed(page)
    return " ".join(parser.unknown_language), " ".join(parser.inherited_language)


def _french_session(slug: str = "conge-approbation", questions: list[dict] | None = None, **summary) -> str:
    """A session whose request and whose whole reasoning turn are French."""
    svc = SessionService()
    svc.create_session(REQUEST_FR, slug=slug)
    svc.update_model(slug, json.dumps({
        "model": full_slots(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
        "questions": questions if questions is not None else [{"q": QUESTION_FR, "slot": "business_rules", "why": WHY_FR}],
        "summary": {"objective": OBJECTIVE_FR, **summary},
        "decisions": [{"decision": DECISION_EN, "derived_from": ["business_rules"]}],
        "challenges": [{"headline": CHALLENGE_EN, "premise": "A stalled request should escalate after exactly five days.",
                        "alternative": "Remind first, then escalate on a per-client window.",
                        "consequence": "A hard jump can bypass the intended sign-off.",
                        "recommendation": "Confirm the window before building it in.", "contests": ["business_rules"]}],
    }))
    return slug


@pytest.fixture
def page(client) -> str:
    return client.get(f"/sessions/{_french_session(scope=SCOPE_FR, assumptions=[ASSUMPTION_FR], blind_spot=BLIND_SPOT_FR)}").text


# -- must fire: the engine's and the client's own words are not announced as English --------------

def test_the_clients_and_the_engines_own_words_are_not_announced_in_the_pages_language(page):
    """The request verbatim, the objective, scope, assumptions and blind spot, and each question with its stake (#277)."""
    tagged, untagged = _regions(page)
    assert REQUEST_FR in tagged, f'the request blockquote inherits lang="en". Untagged text: {untagged}'
    for prose in (OBJECTIVE_FR, SCOPE_FR, ASSUMPTION_FR, BLIND_SPOT_FR, QUESTION_FR, WHY_FR):
        assert prose in tagged, f'engine-authored prose still inherits lang="en": {prose!r}'


def test_the_overflow_questions_under_traceability_are_tagged_too(client):
    """`_traceability.html` renders its own copy of the question markup."""
    questions = [{"q": f"{QUESTION_FR} ({slot})", "slot": slot, "why": WHY_FR} for slot in OVERFLOW_SLOTS[:-1]]
    questions.append({"q": OVERFLOW_Q_FR, "slot": OVERFLOW_SLOTS[-1], "why": OVERFLOW_WHY_FR})
    page = client.get(f"/sessions/{_french_session('tournees-maintenance', questions)}").text
    tagged, _ = _regions(page)
    assert "All open questions" in page, "the traceability overflow block did not render, so nothing was asserted about it"
    assert OVERFLOW_Q_FR in tagged and OVERFLOW_WHY_FR in tagged, "the sixth question's copy of the markup inherits English"


def test_the_home_rows_title_is_not_announced_in_the_pages_language(client):
    """A row's title is the opening of the request itself (`viewmodels/sessions.py::_title`)."""
    _french_session()
    tagged, _ = _regions(client.get("/").text)
    assert REQUEST_FR[:40] in tagged, "the session row's title is the client's own text and still inherits the document language"


# -- must not fire: the chrome is English and still says so ---------------------------------------

def test_the_page_still_declares_english_for_its_own_chrome_and_the_briefs_own_content(page):
    """The counterweight: the chrome and the brief's English content are rendered under the document's English, never tagged."""
    assert '<html lang="en">' in page
    tagged, untagged = _regions(page)
    for english in CHROME + (DECISION_EN, CHALLENGE_EN):
        assert english in untagged, f"{english!r} is not rendered under the declared English -- is it on the page at all?"
        assert english not in tagged, f"{english!r} was swept into an unknown-language region"


def test_the_tab_title_is_a_stated_gap_rather_than_an_oversight(page):
    """The one region left declared English while it may hold the engine's mirroring prose (#277)."""
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S)
    assert title is not None, "the page rendered no <title> at all"
    rendered = html.unescape(title.group(1))    # `<title>` is text-only, so autoescaping is still in force (#39)
    assert "lang=" not in title.group(0), "the tab title now declares a language: revisit the reason in sessions/detail.html"
    assert OBJECTIVE_FR in rendered and "Requivo" in rendered, f"the gap rests on objective + English chrome in one string: {rendered!r}"
