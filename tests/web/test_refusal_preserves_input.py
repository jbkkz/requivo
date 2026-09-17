"""A refusal must not cost the reader what they typed (#30)."""

from __future__ import annotations

import re

import pytest

from requivo.core.context import available_cards
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS, MAX_REQUEST_CHARS, MAX_SLUG_CHARS
from tests.web.conftest import engine_reply

LONG_REQUEST = "The client wrote: " + "x" * MAX_REQUEST_CHARS
LONG_ANSWERS = "They replied: " + "y" * MAX_ANSWERS_CHARS


def textarea_body(html: str, name: str) -> str:
    """What is actually inside `<textarea name="...">…</textarea>`."""
    m = re.search(rf'<textarea[^>]*\bname="{name}"[^>]*>(.*?)</textarea>', html, re.S)
    assert m, f"no <textarea name={name!r}> in the response"
    return m.group(1)


def input_value(html: str, name: str) -> str:
    """The `value` of `<input name="...">`, or "" when it carries none."""
    m = re.search(rf'<input[^>]*\bname="{name}"[^>]*>', html)
    assert m, f"no <input name={name!r}> in the response"
    v = re.search(r'\bvalue="([^"]*)"', m.group(0))
    return v.group(1) if v else ""


# ── the request textarea ──────────────────────────────────────────────────────

def test_an_oversized_request_comes_back_in_the_textarea(client):
    r = client.post("/sessions", data={"request_text": LONG_REQUEST, "provider": "create_only"})
    assert r.status_code == 413
    assert LONG_REQUEST in textarea_body(r.text, "request_text")
    assert not SessionService().list_entries()          # still refused, not truncated into a session


def test_the_refusal_is_the_home_page_not_a_dead_end(client):
    """The reader lands back on the form with a banner, not on an error page whose only affordance is *Back to
    sessions* — which is where the pasted text went to die."""
    r = client.post("/sessions", data={"request_text": LONG_REQUEST, "provider": "create_only"})
    assert 'action="/sessions"' in r.text               # the form is here to resubmit from
    assert "Back to sessions" not in r.text
    assert f"{MAX_REQUEST_CHARS:,}" in r.text           # the limit is still stated (it always was)


def test_a_request_at_the_ceiling_is_not_refused(client):
    """Must fire. Every assertion above is satisfied by an implementation that refuses everything."""
    r = client.post("/sessions", data={"request_text": "x" * MAX_REQUEST_CHARS,
                                       "provider": "create_only"}, follow_redirects=False)
    assert r.status_code == 303
    assert len(SessionService().list_entries()) == 1


# ── the session-name field ────────────────────────────────────────────────────

def test_an_oversized_session_name_preserves_both_fields(client):
    long_slug = "a" * (MAX_SLUG_CHARS + 1)
    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "slug": long_slug, "provider": "create_only"})
    assert r.status_code == 413
    assert "A leave approval system." in textarea_body(r.text, "request_text")
    assert input_value(r.text, "slug") == long_slug


def test_an_unusable_session_name_re_renders_rather_than_navigating_away(client):
    """The same field's other refusal. Making one of a field's two refusals keep the reader's work and the
    other throw it away is a worse state than either, so both arms round-trip."""
    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "slug": "Not A Slug", "provider": "create_only"})
    assert r.status_code == 400
    assert "A leave approval system." in textarea_body(r.text, "request_text")
    assert input_value(r.text, "slug") == "Not A Slug"


def test_the_empty_request_refusal_also_keeps_the_session_name(client):
    """The refusal that already re-rendered still dropped the other field on the way through."""
    r = client.post("/sessions", data={"request_text": "   ", "slug": "leave-approval",
                                       "provider": "create_only"})
    assert r.status_code == 400
    assert input_value(r.text, "slug") == "leave-approval"


# Every refusal on the form, paired with a session-name field the reader left **blank**.
REFUSALS_WITH_NO_NAME = [
    ("an over-long request", {"request_text": LONG_REQUEST}, 413),
    ("an empty request", {"request_text": "   "}, 400),
]


@pytest.mark.parametrize("label, data, status", REFUSALS_WITH_NO_NAME)
def test_a_refusal_never_fills_in_a_session_name_the_reader_did_not_type(client, label, data, status):
    """A refusal must hand back the form as submitted -- a field left blank was submitted blank (#30)."""
    r = client.post("/sessions", data={**data, "provider": "create_only"})
    assert r.status_code == status, label
    assert input_value(r.text, "slug") == "", label
    assert "None" not in input_value(r.text, "slug"), label


# ── the context-card selection ────────────────────────────────────────────────

def test_a_refusal_keeps_the_context_cards_the_reader_picked(client):
    """Cards are not decoration: a session's identity is its request **and** its card selection."""
    cards = available_cards()
    if len(cards) < 2:
        pytest.skip(f"needs two bundled context cards to tell selected from unselected; got {cards}")
    picked, unpicked = cards[0], cards[1]

    r = client.post("/sessions", data={"request_text": LONG_REQUEST, "cards": [picked],
                                       "provider": "create_only"})
    assert r.status_code == 413
    checked = re.findall(r'<input[^>]*name="cards"[^>]*value="([^"]+)"[^>]*checked', r.text)
    assert picked in checked
    assert unpicked not in checked                      # must fire: not simply every box ticked


# ── the answers textarea (an HTMX swap, so worse than the others) ─────────────

def test_oversized_answers_come_back_in_the_textarea(client, with_provider):
    """The answers route posts with `hx-swap="outerHTML"` onto `#session-body`."""
    with_provider(engine_reply())
    client.post("/sessions", data={"request_text": "A leave approval system.",
                                   "slug": "leave-approval", "provider": "anthropic"},
                follow_redirects=False)
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": LONG_ANSWERS, "expected_revision": "1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 413
    assert 'id="session-body"' in r.text                # the region came back, not an error fragment
    assert LONG_ANSWERS in textarea_body(r.text, "answers")
    assert f"{MAX_ANSWERS_CHARS:,}" in r.text           # …with the refusal stated on it


def test_the_answers_refusal_never_reaches_the_provider(client, with_provider):
    """Must fire in the other direction: preserving the text must not have turned the refusal into an
    acceptance."""
    fake = with_provider(engine_reply(), engine_reply())
    client.post("/sessions", data={"request_text": "A leave approval system.",
                                   "slug": "leave-approval", "provider": "anthropic"},
                follow_redirects=False)
    before = SessionService().meta("leave-approval").current_revision

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": LONG_ANSWERS, "expected_revision": str(before)},
                    headers={"HX-Request": "true"})
    assert r.status_code == 413
    assert len(fake.calls) == 1                         # discovery only; the refused turn cost nothing
    assert SessionService().meta("leave-approval").current_revision == before


def test_answers_at_the_ceiling_still_run(client, with_provider):
    """Must fire. Without this, refusing every submission passes every assertion above."""
    fake = with_provider(engine_reply(), engine_reply())
    client.post("/sessions", data={"request_text": "A leave approval system.",
                                   "slug": "leave-approval", "provider": "anthropic"},
                follow_redirects=False)
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "y" * MAX_ANSWERS_CHARS, "expected_revision": "1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert len(fake.calls) == 2
