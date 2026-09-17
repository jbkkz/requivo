"""The web forms: no-JS fallback for the three htmx-only forms (#428), the counting-not-clipping affordance
(#239), invariant 3 at the web edge (#142, #8) and a refusal that must not cost the reader what they typed (#30)."""

from __future__ import annotations

import re

import pytest

from requivo.core.context import available_cards
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS, MAX_REQUEST_CHARS, MAX_SLUG_CHARS
from requivo.web.security import CSRF_FIELD, csrf_token
from requivo.web.templating import TEMPLATES_DIR
from tests.web.conftest import (
    BRIEF_REPLY,
    HIGH_EXPLICIT,
    HIGH_INFERRED,
    PAID,
    PAID_TOKENS,
    _make_session,
    create_via_post,
    engine_reply,
    run_js_harness,
)

_ASKING = engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED)
_CONVERGED = engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT)
_ANSWER = {"answers": "Exceptions go to HR.", "expected_revision": "1"}
LONG_REQUEST = "The client wrote: " + "x" * MAX_REQUEST_CHARS
LONG_ANSWERS = "They replied: " + "y" * MAX_ANSWERS_CHARS


def _seed(client, slug="leave-approval"):
    """A session created through the browser's own form, token as a field."""
    client.post("/sessions", data={"request_text": "x", "slug": slug, "provider": "anthropic", CSRF_FIELD: csrf_token()})


def textarea_body(html: str, name: str) -> str:
    m = re.search(rf'<textarea[^>]*\bname="{name}"[^>]*>(.*?)</textarea>', html, re.S)
    assert m, f"no <textarea name={name!r}> in the response"
    return m.group(1)


def input_value(html: str, name: str) -> str:
    """The `value` of `<input name="...">`, or "" when it carries none."""
    m = re.search(rf'<input[^>]*\bname="{name}"[^>]*>', html)
    assert m, f"no <input name={name!r}> in the response"
    v = re.search(r'\bvalue="([^"]*)"', m.group(0))
    return v.group(1) if v else ""


# ── the no-JS fallback (#428) ─────────────────────────────────────────────────


def test_the_three_forms_all_carry_a_plain_post_fallback(client, with_provider, monkeypatch):
    """Static proof the GET-with-token-in-URL shape is unreachable: `method="post"` in the SAME tag as `action=`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with_provider(_ASKING)
    _seed(client)
    page = client.get("/sessions/leave-approval").text
    assert re.search(r'<form[^>]*method="post"[^>]*action="(/sessions/leave-approval/answers)"', page), (
        "the answers form has no method=post action= fallback")
    assert 'hx-post="/sessions/leave-approval/answers"' in page
    assert re.findall(r'<form[^>]*method="post"[^>]*action="(/sessions/leave-approval/artifacts/[^"]+)"', page), (
        "no generate-document form carries a method=post action= fallback")


def test_a_no_js_answers_submit_applies_the_answer_and_lands_on_the_session_page(raw_client, with_provider):
    """A no-JS submit that reaches the provider is real money, and the landing page says what it spent (#428)."""
    with_provider(_ASKING, _CONVERGED, spend=PAID)
    _seed(raw_client)
    r = raw_client.post("/sessions/leave-approval/answers", data={**_ANSWER, CSRF_FIELD: csrf_token()},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-approval"
    landing = raw_client.get(r.headers["location"]).text
    assert "No question left" in landing         # the second scripted reply only pops if the provider was reached
    assert PAID_TOKENS in landing, "the spend the no-JS turn just made is nowhere on the page it lands on"


def test_a_no_js_generate_submit_saves_the_document_and_lands_on_the_session_page(raw_client, with_provider, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY, spend=PAID)
    _seed(raw_client)
    r = raw_client.post("/sessions/leave-approval/artifacts/brief", data={CSRF_FIELD: csrf_token()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-approval"
    landing = raw_client.get(r.headers["location"]).text
    assert "Decision brief" in landing and "Up to date" in landing
    assert PAID_TOKENS in landing, "the spend the no-JS generation just made is nowhere on the page it lands on"


@pytest.mark.parametrize("form", ["answers", "generate"])
def test_a_js_submit_is_unchanged_a_fragment_not_a_redirect(client, with_provider, monkeypatch, form):
    """The must-fire pair: an htmx-tagged request still gets the fragment swap (#428)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    if form == "answers":
        with_provider(_ASKING, _CONVERGED)
        _seed(client)
        r = client.post("/sessions/leave-approval/answers", data=_ANSWER, follow_redirects=False)
        assert r.status_code == 200 and "What changed" in r.text
    else:
        with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY)
        _seed(client)
        r = client.post("/sessions/leave-approval/artifacts/brief", follow_redirects=False)
        assert r.status_code == 200 and "Decision brief" in r.text


def test_a_no_js_oversized_answers_submit_keeps_the_typed_text_on_a_full_page(raw_client, with_provider):
    with_provider(_ASKING)
    _seed(raw_client)
    too_long = "y" * (MAX_ANSWERS_CHARS + 1)
    r = raw_client.post("/sessions/leave-approval/answers",
                        data={"answers": too_long, "expected_revision": "1", CSRF_FIELD: csrf_token()})
    assert r.status_code == 413
    assert too_long in r.text, "the typed answer must survive the refusal, not be dropped (#30)"
    assert "characters" in r.text and "</html>" in r.text.lower()   # a full page, not a bare fragment


def test_a_no_js_redirect_does_not_leave_a_stash_the_next_unrelated_view_would_repeat(raw_client, with_provider):
    """The htmx path must not gain a `carry_to` it never had (#428)."""
    with_provider(_ASKING, _CONVERGED, spend=PAID)
    _seed(raw_client)
    fragment = raw_client.post("/sessions/leave-approval/answers", data={**_ANSWER, CSRF_FIELD: csrf_token()},
                               headers={"HX-Request": "true"})
    assert PAID_TOKENS in fragment.text          # shown once, inline, on the fragment itself
    assert PAID_TOKENS not in raw_client.get("/sessions/leave-approval").text, "a later view must not repeat it"


# ── the counting-not-clipping affordance (#239) ───────────────────────────────

_HARNESS_LIMIT = 20_000   # the ceiling counter_harness.js declares on its fields


def test_the_character_counter_counts_and_warns_without_ever_touching_the_text():
    """The whole rule, on the shipped asset. The two assertions that carry it are the negative ones (#239)."""
    assert _HARNESS_LIMIT == MAX_REQUEST_CHARS == MAX_ANSWERS_CHARS, (
        f"counter_harness.js declares a {_HARNESS_LIMIT}-character ceiling but the product refuses at "
        f"{MAX_REQUEST_CHARS}/{MAX_ANSWERS_CHARS} — update the harness and the expected strings together")
    t = {row["at"]: row for row in run_js_harness("counter_harness", "the counting-not-clipping rule", "#239")}
    assert t["initial"]["text"] == "" and t["just under the threshold"]["text"] == "", "a counter that is always on says nothing"
    at = t["at the threshold"]                                   # must fire
    assert at["text"] == "16,000 / 20,000 characters", "crossing 80% of the ceiling has to reveal the count"
    assert at["className"] == "counter" and at["live"] == "polite", "80% is a heads-up, announced, not a refusal"
    ceiling = t["exactly at the ceiling"]                        # the maximum *permitted* length
    assert ceiling["text"] == "20,000 / 20,000 characters" and ceiling["className"] == "counter"
    over = t["one over the ceiling"]
    assert over["className"] == "counter danger" and over["text"].startswith("20,001 / 20,000 characters")
    assert "refused" in over["text"], "past the ceiling the copy has to say what will happen, not only turn red"
    assert t["back down again"]["text"] == "" and t["back down again"]["className"] == "counter"   # never latched
    assert t["swapped in, already full"]["text"] == "19,000 / 20,000 characters"   # a swapped-in field (#30)
    # The third state: a ceiling the page cannot read builds no counter (the guard `!(limit > 0)`).
    for label in ("ceiling unparseable", "ceiling is zero", "no ceiling declared"):
        assert t[label]["text"] is None, f"at '{label}' the page built a counter against a ceiling it could not read"
    for label in ("swapped in, already full", "ceiling unparseable", "ceiling is zero"):
        assert t[label]["length"] >= 19_000                      # must fire: well past the threshold, same code path
    for label, row in t.items():                                 # the two halves that must never fire
        assert row["writes"] == [], f"at '{label}' the page assigned to the field (invariant 3, #8)"
        clipping = [name for name in row["fieldAttributes"] if "maxlength" in name.lower()]
        assert not clipping, f"at '{label}' the page gave the field {clipping}: a paste past the ceiling drops silently (#8)"


def test_the_limit_the_page_shows_is_the_limit_the_server_refuses_on(client, with_provider):
    """The number is rendered from `web/config.py`, never typed into a template; no rendered field clips (#8)."""
    with_provider(_ASKING)
    _make_session()
    home, session = client.get("/").text, client.get("/sessions/leave-approval").text
    assert 'name="request_text"' in home and 'name="slug"' in home and 'name="answers"' in session   # positive control
    assert f'data-limit="{MAX_REQUEST_CHARS}"' in home, "the request field declares no ceiling, or not the server's"
    assert f'data-limit="{MAX_ANSWERS_CHARS}"' in session, "the answers field declares no ceiling, or not the server's"
    for page_name, page in (("home", home), ("session", session)):
        assert "maxlength" not in page, f"the {page_name} page renders a field that silently clips input"


def test_no_template_carries_a_clipping_attribute():
    """The rendered check above only sees what those two routes produce."""
    bodies = {p: p.read_text(encoding="utf-8") for p in sorted(TEMPLATES_DIR.rglob("*.html"))}
    assert len(bodies) >= 10 and any("<textarea" in body for body in bodies.values())   # positive control
    clipping = sorted(p.name for p, body in bodies.items() if "maxlength" in body)
    assert not clipping, f"templates clip input client-side: {clipping}"


# ── invariant 3 at the web edge: refuse, don't truncate; refuse, don't filter (#142) ──


def test_an_oversized_request_is_refused_not_truncated(client):
    r = client.post("/sessions", data={"request_text": "x" * (MAX_REQUEST_CHARS + 1), "provider": "create_only"})
    assert r.status_code == 413
    assert not SessionService().list_sessions()      # nothing was created from the truncated half


def test_a_request_of_exactly_the_ceiling_is_accepted(client):
    """Must fire: `MAX_REQUEST_CHARS` is the maximum *permitted* length (#8)."""
    r = client.post("/sessions", data={"request_text": "x" * MAX_REQUEST_CHARS, "provider": "create_only"},
                    follow_redirects=False)
    assert r.status_code == 303 and len(SessionService().list_sessions()) == 1


def test_oversized_answers_are_refused_before_the_paid_turn(client, with_provider):
    """The answers field has the same ceiling and the same refusal; the legal turn still runs."""
    fake = with_provider(_ASKING, engine_reply(converged=True, problem=HIGH_EXPLICIT))
    create_via_post(client)
    assert len(fake.calls) == 1                       # discovery ran
    too_long = client.post("/sessions/leave-approval/answers",
                           data={"answers": "y" * (MAX_ANSWERS_CHARS + 1), "expected_revision": "1"})
    assert too_long.status_code == 413 and len(fake.calls) == 1   # refused before the provider was billed
    assert SessionService().meta("leave-approval").current_revision == 1
    at_ceiling = client.post("/sessions/leave-approval/answers",
                             data={"answers": "y" * MAX_ANSWERS_CHARS, "expected_revision": "1"})
    assert at_ceiling.status_code == 200 and len(fake.calls) == 2


def test_an_oversized_session_name_is_refused(client):
    """The session-name field lost its `maxlength` too — same class, same `>` refusal."""
    too_long = client.post("/sessions", data={"request_text": "x", "slug": "a" * (MAX_SLUG_CHARS + 1), "provider": "create_only"})
    assert too_long.status_code == 413 and not SessionService().list_sessions()
    at_ceiling = client.post("/sessions", data={"request_text": "x", "slug": "a" * MAX_SLUG_CHARS, "provider": "create_only"},
                             follow_redirects=False)
    assert at_ceiling.status_code == 303 and len(SessionService().list_sessions()) == 1


def test_an_unknown_context_card_is_refused(client):
    # Filtering it out would leave an empty selection, which every reader treats as "all cards".
    r = client.post("/sessions", data={"request_text": "x", "provider": "create_only", "cards": ["no-such-card"]})
    assert r.status_code == 400 and not SessionService().list_sessions()


# ── a refusal must not cost the reader what they typed (#30) ──────────────────


def test_an_oversized_request_comes_back_in_the_textarea(client):
    """The reader lands back on the form with a banner, not on an error page where the pasted text went to die."""
    r = client.post("/sessions", data={"request_text": LONG_REQUEST, "provider": "create_only"})
    assert r.status_code == 413
    assert LONG_REQUEST in textarea_body(r.text, "request_text")
    assert not SessionService().list_entries()          # still refused, not truncated into a session
    assert 'action="/sessions"' in r.text and "Back to sessions" not in r.text   # the form is here to resubmit from
    assert f"{MAX_REQUEST_CHARS:,}" in r.text           # the limit is still stated


@pytest.mark.parametrize("slug, status", [("a" * (MAX_SLUG_CHARS + 1), 413), ("Not A Slug", 400)],
                         ids=["oversized", "unusable"])
def test_an_unusable_session_name_re_renders_rather_than_navigating_away(client, slug, status):
    """The field's two refusals both round-trip both fields; one keeping and one dropping is worse than either."""
    r = client.post("/sessions", data={"request_text": "A leave approval system.", "slug": slug, "provider": "create_only"})
    assert r.status_code == status
    assert "A leave approval system." in textarea_body(r.text, "request_text")
    assert input_value(r.text, "slug") == slug


def test_the_empty_request_refusal_also_keeps_the_session_name(client):
    """The refusal that already re-rendered still dropped the other field on the way through."""
    r = client.post("/sessions", data={"request_text": "   ", "slug": "leave-approval", "provider": "create_only"})
    assert r.status_code == 400 and input_value(r.text, "slug") == "leave-approval"


@pytest.mark.parametrize("label, data, status", [("an over-long request", {"request_text": LONG_REQUEST}, 413),
                                                 ("an empty request", {"request_text": "   "}, 400)])
def test_a_refusal_never_fills_in_a_session_name_the_reader_did_not_type(client, label, data, status):
    """A refusal hands back the form as submitted -- a field left blank was submitted blank (#30)."""
    r = client.post("/sessions", data={**data, "provider": "create_only"})
    assert r.status_code == status, label
    assert input_value(r.text, "slug") == "" and "None" not in input_value(r.text, "slug"), label


def test_a_refusal_keeps_the_context_cards_the_reader_picked(client):
    """Cards are not decoration: a session's identity is its request **and** its card selection."""
    cards = available_cards()
    if len(cards) < 2:
        pytest.skip(f"needs two bundled context cards to tell selected from unselected; got {cards}")
    picked, unpicked = cards[0], cards[1]
    r = client.post("/sessions", data={"request_text": LONG_REQUEST, "cards": [picked], "provider": "create_only"})
    assert r.status_code == 413
    checked = re.findall(r'<input[^>]*name="cards"[^>]*value="([^"]+)"[^>]*checked', r.text)
    assert picked in checked and unpicked not in checked   # must fire: not simply every box ticked


def test_oversized_answers_come_back_in_the_textarea(client, with_provider):
    """The answers route posts with `hx-swap="outerHTML"` onto `#session-body`, so the region has to come back."""
    with_provider(engine_reply())
    create_via_post(client, request_text="A leave approval system.")
    r = client.post("/sessions/leave-approval/answers", data={"answers": LONG_ANSWERS, "expected_revision": "1"})
    assert r.status_code == 413
    assert 'id="session-body"' in r.text                # the region came back, not an error fragment
    assert LONG_ANSWERS in textarea_body(r.text, "answers")
    assert f"{MAX_ANSWERS_CHARS:,}" in r.text           # …with the refusal stated on it
