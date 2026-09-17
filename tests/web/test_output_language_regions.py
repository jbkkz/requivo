"""The rendered page must not announce the client's language as English (#277)."""
from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser

from requivo.services.sessions import SessionService
from tests.web.conftest import HIGH_EXPLICIT, HIGH_INFERRED, full_slots

# A French request and a French reasoning turn -- the shape #277 is about.
REQUEST_FR = "Nous aimerions que les managers approuvent les demandes de congé des employés."
OBJECTIVE_FR = "Permettre aux managers d'approuver les demandes de congé, avec une escalade."
SCOPE_FR = "Un circuit d'approbation configurable par client."
ASSUMPTION_FR = "Nous supposons que le manager direct est l'approbateur par défaut."
BLIND_SPOT_FR = "Personne n'a dit ce qui se passe quand le manager est déjà absent."
QUESTION_FR = "Qui approuve lorsque le manager est lui-même en congé ?"
WHY_FR = "Une escalade non définie change le circuit et le coût du développement."

# English chrome rendered by the same templates.
CHROME = ("What Requivo understood", "Are we ready?", "The request", "Why it matters")

# The decision brief's own content, in English as the policy requires of a buildable artifact, on a session whose request is French.
DECISION_EN = "Escalation is time-based rather than delegated"
CHALLENGE_EN = "Fixed five-day escalation"


class _LangRegions(HTMLParser):
    """Collect the page's text twice over."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[bool] = []
        self.unknown_language: list[str] = []
        self.inherited_language: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        declared = dict(attrs).get("lang")
        inside = bool(self._stack and self._stack[-1]) or declared == ""
        # Void elements never close, so pushing them would unbalance the stack for the rest of the document.
        if tag not in ("br", "hr", "img", "input", "meta", "link"):
            self._stack.append(inside)

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._stack and self._stack[-1]:
            self.unknown_language.append(text)
        else:
            self.inherited_language.append(text)


def _regions(page: str) -> _LangRegions:
    parser = _LangRegions()
    parser.feed(page)
    return parser


def _french_session(slug: str = "conge-approbation") -> str:
    """A session whose request and whose whole reasoning turn are French."""
    svc = SessionService()
    svc.create_session(REQUEST_FR, slug=slug)
    svc.update_model(slug, json.dumps({
        "model": full_slots(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
        "questions": [{"q": QUESTION_FR, "slot": "business_rules", "why": WHY_FR}],
        "summary": {"objective": OBJECTIVE_FR, "scope": SCOPE_FR,
                    "assumptions": [ASSUMPTION_FR], "blind_spot": BLIND_SPOT_FR},
        "decisions": [{"decision": DECISION_EN, "derived_from": ["business_rules"]}],
        "challenges": [{
            "headline": CHALLENGE_EN,
            "premise": "A stalled request should escalate after exactly five days.",
            "alternative": "Remind first, then escalate on a per-client window.",
            "consequence": "A hard jump can bypass the intended sign-off.",
            "recommendation": "Confirm the window before building it in.",
            "contests": ["business_rules"],
        }],
    }))
    return slug


# -- must fire: the engine's and the client's own words are not announced as English --------------

def test_the_clients_and_the_engines_own_words_are_not_announced_in_the_pages_language(client):
    """The request verbatim, the objective, scope, assumptions and blind spot, and each question with its stake (#277)."""
    slug = _french_session()
    regions = _regions(client.get(f"/sessions/{slug}").text)
    tagged = " ".join(regions.unknown_language)
    assert any(REQUEST_FR in t for t in regions.unknown_language), (
        f'the request blockquote inherits lang="en". Text outside any such region: {regions.inherited_language}')
    for prose in (OBJECTIVE_FR, SCOPE_FR, ASSUMPTION_FR, BLIND_SPOT_FR, QUESTION_FR, WHY_FR):
        assert prose in tagged, f'engine-authored prose still inherits lang="en": {prose!r}'


# Six questions is the contract's own ceiling and one more than `PRIORITY_QUESTIONS`.
OVERFLOW_SLOTS = ("problem", "workflow", "permissions", "integrations", "edge_cases", "business_rules")
OVERFLOW_Q_FR = "Que fait-on d'une tournée annulée après la facturation ?"
OVERFLOW_WHY_FR = "Le rattrapage facturé deux fois est le coût que personne n'a chiffré."


def test_the_overflow_questions_under_traceability_are_tagged_too(client):
    """`_traceability.html` renders its own copy of the question markup."""
    slug = "tournees-maintenance"
    svc = SessionService()
    svc.create_session(REQUEST_FR, slug=slug)
    questions = [{"q": f"{QUESTION_FR} ({slot})", "slot": slot, "why": WHY_FR}
                 for slot in OVERFLOW_SLOTS[:-1]]
    questions.append({"q": OVERFLOW_Q_FR, "slot": OVERFLOW_SLOTS[-1], "why": OVERFLOW_WHY_FR})
    svc.update_model(slug, json.dumps({
        "model": full_slots(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
        "questions": questions,
        "summary": {"objective": OBJECTIVE_FR},
    }))

    page = client.get(f"/sessions/{slug}").text
    regions = _regions(page)
    tagged = " ".join(regions.unknown_language)

    assert "All open questions" in page, (
        "the traceability overflow block did not render, so this test asserted nothing about it -- "
        "the session needs more questions than PRIORITY_QUESTIONS for that branch to be reached"
    )
    assert OVERFLOW_Q_FR in tagged, (
        "the sixth question is shown only under Traceability details, and that copy of the markup "
        "still inherits the document's English"
    )
    assert OVERFLOW_WHY_FR in tagged, "its stake, in the same block, likewise"


def test_the_home_rows_title_is_not_announced_in_the_pages_language(client):
    """A row's title is the opening of the request itself (`viewmodels/sessions.py::_title`)."""
    _french_session()

    regions = _regions(client.get("/").text)
    tagged = " ".join(regions.unknown_language)

    assert REQUEST_FR[:40] in tagged, (
        "the session row's title is the client's own request text and still inherits the document "
        "language"
    )


# -- must not fire: the chrome is English and still says so ---------------------------------------

def test_the_page_still_declares_english_for_its_own_chrome(client):
    """The counterweight to every assertion above."""
    slug = _french_session()

    page = client.get(f"/sessions/{slug}").text

    assert '<html lang="en">' in page


def test_the_decision_briefs_own_content_still_declares_english(client):
    """The other half of the policy, asserted from the page."""
    slug = _french_session()

    regions = _regions(client.get(f"/sessions/{slug}").text)
    tagged = " ".join(regions.unknown_language)
    untagged = " ".join(regions.inherited_language)

    for artifact_prose in (DECISION_EN, CHALLENGE_EN):
        assert artifact_prose in untagged, (
            f"the brief's own content {artifact_prose!r} is not rendered under the document's "
            f"declared English -- is it on the page at all?"
        )
        assert artifact_prose not in tagged, (
            f"the brief's own content {artifact_prose!r} was tagged as being of unknown language. "
            f"The policy anchors it in English; only what mirrors the request is tagged."
        )


def test_the_tab_title_is_a_stated_gap_rather_than_an_oversight(client):
    """The one region this change leaves declared English while it may hold the engine's mirroring prose."""
    slug = _french_session()

    page = client.get(f"/sessions/{slug}").text
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S)

    assert title is not None, "the page rendered no <title> at all"
    # `<title>` is text-only, so Jinja's autoescaping is still in force inside it (#39).
    rendered = html.unescape(title.group(1))
    assert "lang=" not in title.group(0), (
        "the tab title now declares a language. That may well be the better call -- but the reason "
        "it did not is written in sessions/detail.html and has to be revisited with it, not left "
        "standing as an explanation for something that is no longer true."
    )
    assert OBJECTIVE_FR in rendered, (
        f"the gap this test documents rests on the title carrying the engine's objective; it now "
        f"holds {rendered!r}"
    )
    assert "Requivo" in rendered, (
        "and on that objective being concatenated with English chrome, which is why no single "
        "`lang` value is true of the string"
    )


def test_the_english_chrome_is_not_swept_into_the_unknown_language_regions(client):
    """The positive control for the four tests above."""
    slug = _french_session()

    regions = _regions(client.get(f"/sessions/{slug}").text)
    tagged = " ".join(regions.unknown_language)
    untagged = " ".join(regions.inherited_language)

    for label in CHROME:
        assert label in untagged, (
            f"the English chrome {label!r} is not rendered outside the unknown-language regions -- "
            f"is it on the page at all? A vacuous pass here would hide a blanket tag."
        )
        assert label not in tagged, (
            f"the English chrome {label!r} was swept into an unknown-language region; the page now "
            f"says its own vocabulary has no known language"
        )
