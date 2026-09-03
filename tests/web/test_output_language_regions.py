"""The rendered page must not announce the client's language as English (#277).

The policy this file guards is stated in `docs/requirements-model.md` ("The language of the
outputs") and anchored in the prompt assets: **the questions and the understanding rendered each
turn mirror the language of the client's request; every buildable artifact -- the decision brief
included -- is written in English.** So a French request produces a French objective and French
questions, sitting inside a page whose chrome, labels, headings and decision brief are English.

`base.html` declared `<html lang="en">` and nothing else in the template tree carried a `lang`
attribute at all, so every one of those French strings inherited `lang="en"` -- a screen reader
announced them with English pronunciation rules, and a translation tool offered to translate the
English chrome while leaving the French prose alone.

**The tag is an empty `lang`, not `lang="fr"`, and that is the whole design.** Nothing in the
session format records the request's language and this change adds no detection code (#277 puts
that out of scope), so the template layer genuinely does not know it. The empty string is HTML's own
way of saying exactly that: "if the attribute's value is the empty string, then the language is
unknown". An honest *unknown* stops the wrong assertion; a guessed `fr` would be a second wrong
assertion, right more often and no better founded. The third state, built rather than only
reported.

Two halves, and the second is what keeps the first from being satisfied by tagging the whole page:

  - every string the policy says *mirrors* is inside an unknown-language region (must fire);
  - the English chrome is not, the decision brief's own content is not, and the document still
    declares English (must not fire).

The brief's half of that is the load-bearing one. Its decisions, challenges and opportunities are
absorbed into the model (`absorb_reasoning`) and every later generator is
prompted with the whole model, so a mirroring brief would inject the request's language into the
English PRD, stories, criteria and epic. The brief anchors English, and the page says `en` about it
-- which is why this file cannot be satisfied by tagging everything the engine wrote.
"""
from __future__ import annotations

import json
from html.parser import HTMLParser

from requivo.services.sessions import SessionService
from tests.web.conftest import HIGH_EXPLICIT, HIGH_INFERRED, full_model

# A French request and a French reasoning turn -- the shape #277 is about. Accented characters on
# purpose: they are also what invariant 16's UTF-8 rule is about, so a page that mangles them fails
# the containment assertions below rather than passing on a mojibake match.
REQUEST_FR = "Nous aimerions que les managers approuvent les demandes de congé des employés."
OBJECTIVE_FR = "Permettre aux managers d'approuver les demandes de congé, avec une escalade."
SCOPE_FR = "Un circuit d'approbation configurable par client."
ASSUMPTION_FR = "Nous supposons que le manager direct est l'approbateur par défaut."
BLIND_SPOT_FR = "Personne n'a dit ce qui se passe quand le manager est déjà absent."
QUESTION_FR = "Qui approuve lorsque le manager est lui-même en congé ?"
WHY_FR = "Une escalade non définie change le circuit et le coût du développement."

# English chrome rendered by the same templates. If these end up inside an unknown-language region
# the page has stopped distinguishing the two halves of the policy, which is the failure a blanket
# tag makes look like a fix.
CHROME = ("What Requivo understood", "Are we ready?", "The request", "Why it matters")

# The decision brief's own content, in English as the policy requires of a buildable artifact, on a
# session whose request is French. The page must keep declaring `en` over it.
DECISION_EN = "Escalation is time-based rather than delegated"
CHALLENGE_EN = "Fixed five-day escalation"


class _LangRegions(HTMLParser):
    """Collect the page's text twice over: what sits inside some element declaring an empty `lang`,
    and what does not. A stack rather than a regex, so the assertions are about the rendered
    document and not about the order of attributes in a template line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[bool] = []
        self.unknown_language: list[str] = []
        self.inherited_language: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        declared = dict(attrs).get("lang")
        inside = bool(self._stack and self._stack[-1]) or declared == ""
        # Void elements never close, so pushing them would unbalance the stack for the rest of the
        # document -- and `<br>`/`<input>` really do appear inside these regions.
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


def _regions(html: str) -> _LangRegions:
    parser = _LangRegions()
    parser.feed(html)
    return parser


def _french_session(slug: str = "conge-approbation") -> str:
    """A session whose request and whose whole reasoning turn are French, seeded through the service
    so nothing here depends on a provider."""
    svc = SessionService()
    svc.create_session(REQUEST_FR, slug=slug)
    svc.update_model(slug, json.dumps({
        "model": full_model(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
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

def test_the_clients_own_request_is_not_announced_in_the_pages_language(client):
    """The blockquote is the client's text verbatim -- the one region whose language is certainly
    not the chrome's, and the minimum this change had to reach."""
    slug = _french_session()

    page = client.get(f"/sessions/{slug}").text
    regions = _regions(page)

    assert any(REQUEST_FR in t for t in regions.unknown_language), (
        'the request blockquote is not inside an element declaring an empty `lang`, so it inherits '
        f'the document lang="en". Text found outside any such region: {regions.inherited_language}'
    )


def test_the_engines_own_prose_is_not_announced_in_the_pages_language(client):
    """The objective, the scope, the assumptions and the blind spot are all written by the engine in
    the request's language under this policy -- every one of them, not just the headline."""
    slug = _french_session()

    regions = _regions(client.get(f"/sessions/{slug}").text)
    tagged = " ".join(regions.unknown_language)

    for prose in (OBJECTIVE_FR, SCOPE_FR, ASSUMPTION_FR, BLIND_SPOT_FR):
        assert prose in tagged, f'engine-authored prose still inherits lang="en": {prose!r}'


def test_a_question_and_its_stake_are_not_announced_in_the_pages_language(client):
    """`q.why` sits beside the English label "Why it matters", so tagging the question alone leaves
    half the sentence mis-announced -- which is why the label and the prose are separate elements."""
    slug = _french_session()

    regions = _regions(client.get(f"/sessions/{slug}").text)
    tagged = " ".join(regions.unknown_language)

    assert QUESTION_FR in tagged, 'the question text still inherits lang="en"'
    assert WHY_FR in tagged, "the question's stake still inherits the document language"


def test_the_home_rows_title_is_not_announced_in_the_pages_language(client):
    """A row's title is the opening of the request itself (`viewmodels/sessions.py::_title`), so the
    listing carries the client's words too."""
    _french_session()

    regions = _regions(client.get("/").text)
    tagged = " ".join(regions.unknown_language)

    assert REQUEST_FR[:40] in tagged, (
        "the session row's title is the client's own request text and still inherits the document "
        "language"
    )


# -- must not fire: the chrome is English and still says so ---------------------------------------

def test_the_page_still_declares_english_for_its_own_chrome(client):
    """The counterweight to every assertion above. Dropping `lang` from `<html>` would satisfy them
    all and would be strictly worse: the product's own labels, headings and copy are English, and a
    document that declares nothing is announced in whatever the reader's default happens to be."""
    slug = _french_session()

    page = client.get(f"/sessions/{slug}").text

    assert '<html lang="en">' in page


def test_the_decision_briefs_own_content_still_declares_english(client):
    """The other half of the policy, asserted from the page. `absorb_reasoning` folds the brief's
    decisions and challenges into the model and every later generator is prompted with that model,
    so the brief anchors English however the request arrived -- and the page has to say so rather
    than declaring the whole of the engine's output to be of unknown language."""
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


def test_the_english_chrome_is_not_swept_into_the_unknown_language_regions(client):
    """The positive control for the four tests above: a blanket empty `lang` on `<body>` would pass
    every one of them while stating that nothing on the page has a known language. The split is the
    point, so the English half has to be asserted from its own side."""
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
