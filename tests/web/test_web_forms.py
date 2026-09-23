"""The web forms: the no-JS fallback (#428), counting-not-clipping (#239), invariant 3 at the web edge (#142, #8)
and a refusal that must not cost the reader what they typed (#30)."""

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
_SESSION = "/sessions/leave-approval"
LONG_REQUEST = "The client wrote: " + "x" * MAX_REQUEST_CHARS
LONG_ANSWERS = "They replied: " + "y" * MAX_ANSWERS_CHARS
# The two htmx forms: the provider replies they need, where they post, what they post, and what the reply says.
_FORMS = {
    "answers": ((_ASKING, _CONVERGED), f"{_SESSION}/answers", _ANSWER, "What changed", "No question left"),
    "generate": ((engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY), f"{_SESSION}/artifacts/brief", {},
                 "Decision brief", "Up to date"),
}


def _seed(client, slug="leave-approval"):
    """A session created through the browser's own form, token as a field."""
    client.post("/sessions", data={"request_text": "x", "slug": slug, "provider": "anthropic", CSRF_FIELD: csrf_token()})


def _create(client, **data):
    return client.post("/sessions", data={"provider": "create_only", **data}, follow_redirects=False)


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
    page = client.get(_SESSION).text
    assert re.search(rf'<form[^>]*method="post"[^>]*action="({_SESSION}/answers)"', page), "no answers fallback"
    assert f'hx-post="{_SESSION}/answers"' in page
    assert re.findall(rf'<form[^>]*method="post"[^>]*action="({_SESSION}/artifacts/[^"]+)"', page), "no generate fallback"


@pytest.mark.parametrize("form", list(_FORMS))
def test_a_no_js_submit_applies_the_write_and_lands_on_the_session_page(raw_client, with_provider, monkeypatch, form):
    """A no-JS submit that reaches the provider is real money, and the landing page says what it spent (#428)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    replies, path, data, _, landed = _FORMS[form]
    with_provider(*replies, spend=PAID)
    _seed(raw_client)
    r = raw_client.post(path, data={**data, CSRF_FIELD: csrf_token()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == _SESSION
    landing = raw_client.get(r.headers["location"]).text
    assert landed in landing                          # the second scripted reply only pops if the provider was reached
    assert PAID_TOKENS in landing, "the spend the no-JS submit just made is nowhere on the page it lands on"


@pytest.mark.parametrize("form", list(_FORMS))
def test_a_js_submit_is_unchanged_a_fragment_not_a_redirect(client, with_provider, monkeypatch, form):
    """The must-fire pair: an htmx-tagged request still gets the fragment swap (#428)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    replies, path, data, fragment, _ = _FORMS[form]
    with_provider(*replies)
    _seed(client)
    r = client.post(path, data=data, follow_redirects=False)
    assert r.status_code == 200 and fragment in r.text


@pytest.mark.parametrize("no_js", [True, False], ids=["no-js-full-page", "htmx-region"])
def test_oversized_answers_come_back_in_the_textarea(raw_client, with_provider, no_js):
    """The typed answer survives the refusal (#30): on a full page without JS, in the `#session-body` region with it."""
    with_provider(_ASKING)
    _seed(raw_client)
    r = raw_client.post(f"{_SESSION}/answers", data={"answers": LONG_ANSWERS, "expected_revision": "1", CSRF_FIELD: csrf_token()},
                        headers={} if no_js else {"HX-Request": "true"})
    assert r.status_code == 413 and LONG_ANSWERS in textarea_body(r.text, "answers")
    assert f"{MAX_ANSWERS_CHARS:,}" in r.text          # the refusal is stated on it
    assert ("</html>" in r.text.lower()) if no_js else ('id="session-body"' in r.text)


def test_a_no_js_redirect_does_not_leave_a_stash_the_next_unrelated_view_would_repeat(raw_client, with_provider):
    """The htmx path must not gain a `carry_to` it never had (#428)."""
    with_provider(_ASKING, _CONVERGED, spend=PAID)
    _seed(raw_client)
    fragment = raw_client.post(f"{_SESSION}/answers", data={**_ANSWER, CSRF_FIELD: csrf_token()}, headers={"HX-Request": "true"})
    assert PAID_TOKENS in fragment.text          # shown once, inline, on the fragment itself
    assert PAID_TOKENS not in raw_client.get(_SESSION).text, "a later view must not repeat it"


# ── the counting-not-clipping affordance (#239) ───────────────────────────────

_HARNESS_LIMIT = 20_000   # the ceiling counter_harness.js declares on its fields


def test_the_character_counter_counts_and_warns_without_ever_touching_the_text():
    """The whole rule, on the shipped asset. The two assertions that carry it are the negative ones (#239)."""
    assert _HARNESS_LIMIT == MAX_REQUEST_CHARS == MAX_ANSWERS_CHARS, "counter_harness.js and web/config.py disagree on the ceiling"
    t = {row["at"]: row for row in run_js_harness("counter_harness", "the counting-not-clipping rule", "#239")}
    assert t["initial"]["text"] == "" and t["just under the threshold"]["text"] == "", "a counter that is always on says nothing"
    at, ceiling, over = t["at the threshold"], t["exactly at the ceiling"], t["one over the ceiling"]
    assert at["text"] == "16,000 / 20,000 characters" and at["className"] == "counter" and at["live"] == "polite"
    assert ceiling["text"] == "20,000 / 20,000 characters" and ceiling["className"] == "counter"
    assert over["className"] == "counter danger" and over["text"].startswith("20,001 / 20,000 characters")
    assert "refused" in over["text"], "past the ceiling the copy has to say what will happen, not only turn red"
    assert t["back down again"]["text"] == "" and t["back down again"]["className"] == "counter"   # never latched
    assert t["swapped in, already full"]["text"] == "19,000 / 20,000 characters"   # a swapped-in field (#30)
    for label in ("ceiling unparseable", "ceiling is zero", "no ceiling declared"):   # the guard `!(limit > 0)`
        assert t[label]["text"] is None, f"at '{label}' the page built a counter against a ceiling it could not read"
    for label in ("swapped in, already full", "ceiling unparseable", "ceiling is zero"):
        assert t[label]["length"] >= 19_000                      # must fire: well past the threshold, same code path
    for label, row in t.items():                                 # the two halves that must never fire
        assert row["writes"] == [], f"at '{label}' the page assigned to the field (invariant 3, #8)"
        assert not [n for n in row["fieldAttributes"] if "maxlength" in n.lower()], f"at '{label}' the field clips (#8)"


def test_the_limit_the_page_shows_is_the_limit_the_server_refuses_on(client, with_provider):
    """The number is rendered from `web/config.py`, never typed into a template; no rendered field clips (#8)."""
    with_provider(_ASKING)
    _make_session()
    home, session = client.get("/").text, client.get(_SESSION).text
    assert 'name="request_text"' in home and 'name="slug"' in home and 'name="answers"' in session   # positive control
    assert f'data-limit="{MAX_REQUEST_CHARS}"' in home and f'data-limit="{MAX_ANSWERS_CHARS}"' in session
    assert "maxlength" not in home and "maxlength" not in session, "a rendered field silently clips input"


def test_no_template_carries_a_clipping_attribute():
    """The rendered check above only sees what those two routes produce."""
    bodies = {p: p.read_text(encoding="utf-8") for p in sorted(TEMPLATES_DIR.rglob("*.html"))}
    assert len(bodies) >= 10 and any("<textarea" in body for body in bodies.values())   # positive control
    assert not [p.name for p, body in bodies.items() if "maxlength" in body]


# ── invariant 3 at the web edge: refuse, don't truncate; refuse, don't filter (#142) ──


@pytest.mark.parametrize("field, ceiling", [("request_text", MAX_REQUEST_CHARS), ("slug", MAX_SLUG_CHARS)])
def test_an_oversized_field_is_refused_and_the_ceiling_itself_is_accepted(client, field, ceiling):
    """One past the ceiling is a 413 with nothing created; the ceiling is the maximum *permitted* length (#8)."""
    over = _create(client, **{"request_text": "x", field: "a" * (ceiling + 1)})
    assert over.status_code == 413 and not SessionService().list_sessions()
    at = _create(client, **{"request_text": "x", field: "a" * ceiling})
    assert at.status_code == 303 and len(SessionService().list_sessions()) == 1


def test_oversized_answers_are_refused_before_the_paid_turn(client, with_provider):
    """The answers field has the same ceiling and the same refusal; the legal turn still runs."""
    fake = with_provider(_ASKING, engine_reply(converged=True, problem=HIGH_EXPLICIT))
    create_via_post(client)
    too_long = client.post(f"{_SESSION}/answers", data={"answers": "y" * (MAX_ANSWERS_CHARS + 1), "expected_revision": "1"})
    assert too_long.status_code == 413 and len(fake.calls) == 1   # refused before the provider was billed
    assert SessionService().meta("leave-approval").current_revision == 1
    at_ceiling = client.post(f"{_SESSION}/answers", data={"answers": "y" * MAX_ANSWERS_CHARS, "expected_revision": "1"})
    assert at_ceiling.status_code == 200 and len(fake.calls) == 2


def test_an_unknown_context_card_is_refused(client):
    # Filtering it out would leave an empty selection, which every reader treats as "all cards".
    r = _create(client, request_text="x", cards=["no-such-card"])
    assert r.status_code == 400 and not SessionService().list_sessions()


# ── a refusal must not cost the reader what they typed (#30) ──────────────────


def test_an_oversized_request_comes_back_in_the_textarea(client):
    """The reader lands back on the form with a banner, not on an error page where the pasted text went to die."""
    r = _create(client, request_text=LONG_REQUEST)
    assert r.status_code == 413 and LONG_REQUEST in textarea_body(r.text, "request_text")
    assert not SessionService().list_entries()          # still refused, not truncated into a session
    assert 'action="/sessions"' in r.text and "Back to sessions" not in r.text   # the form is here to resubmit from
    assert f"{MAX_REQUEST_CHARS:,}" in r.text           # the limit is still stated


@pytest.mark.parametrize("slug, status", [("a" * (MAX_SLUG_CHARS + 1), 413), ("Not A Slug", 400)],
                         ids=["oversized", "unusable"])
def test_an_unusable_session_name_re_renders_rather_than_navigating_away(client, slug, status):
    """The field's two refusals both round-trip both fields; one keeping and one dropping is worse than either."""
    r = _create(client, request_text="A leave approval system.", slug=slug)
    assert r.status_code == status and input_value(r.text, "slug") == slug
    assert "A leave approval system." in textarea_body(r.text, "request_text")


@pytest.mark.parametrize("data, status, slug", [
    ({"request_text": "   ", "slug": "leave-approval"}, 400, "leave-approval"),
    ({"request_text": LONG_REQUEST}, 413, ""),
    ({"request_text": "   "}, 400, ""),
], ids=["empty-keeps-the-name", "over-long-no-name", "empty-no-name"])
def test_a_refusal_hands_back_the_session_name_exactly_as_submitted(client, data, status, slug):
    """A field left blank was submitted blank, never filled in as `None` (#30)."""
    r = _create(client, **data)
    assert r.status_code == status and input_value(r.text, "slug") == slug


def test_a_refusal_keeps_the_context_cards_the_reader_picked(client):
    """Cards are not decoration: a session's identity is its request **and** its card selection."""
    cards = available_cards()
    if len(cards) < 2:
        pytest.skip(f"needs two bundled context cards to tell selected from unselected; got {cards}")
    r = _create(client, request_text=LONG_REQUEST, cards=[cards[0]])
    checked = re.findall(r'<input[^>]*name="cards"[^>]*value="([^"]+)"[^>]*checked', r.text)
    assert r.status_code == 413 and cards[0] in checked and cards[1] not in checked   # must fire: not every box ticked
