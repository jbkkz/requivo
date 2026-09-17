"""Invariant 3 at the web edge: refuse, don't truncate; refuse, don't filter (#142)."""

from __future__ import annotations

from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS, MAX_REQUEST_CHARS, MAX_SLUG_CHARS
from requivo.web.templating import TEMPLATES_DIR
from tests.web.conftest import HIGH_EXPLICIT, HIGH_INFERRED, engine_reply


def test_an_oversized_request_is_refused_not_truncated(client):
    r = client.post("/sessions", data={"request_text": "x" * (MAX_REQUEST_CHARS + 1),
                                       "provider": "create_only"})
    assert r.status_code == 413
    assert not SessionService().list_sessions()      # nothing was created from the truncated half


def test_a_request_of_exactly_the_ceiling_is_accepted(client):
    """`MAX_REQUEST_CHARS` is the maximum *permitted* length (#8)."""
    r = client.post("/sessions", data={"request_text": "x" * MAX_REQUEST_CHARS,
                                       "provider": "create_only"}, follow_redirects=False)
    assert r.status_code == 303
    assert len(SessionService().list_sessions()) == 1


def test_oversized_answers_are_refused_before_the_paid_turn(client, with_provider):
    """The answers field has the same ceiling and the same refusal, and had no test at all."""
    fake = with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                         engine_reply(converged=True, problem=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval",
                                   "provider": "anthropic"})
    assert len(fake.calls) == 1                       # discovery ran

    too_long = client.post("/sessions/leave-approval/answers",
                           data={"answers": "y" * (MAX_ANSWERS_CHARS + 1), "expected_revision": "1"})
    assert too_long.status_code == 413
    assert len(fake.calls) == 1                       # refused before the provider was billed for it

    at_ceiling = client.post("/sessions/leave-approval/answers",
                             data={"answers": "y" * MAX_ANSWERS_CHARS, "expected_revision": "1"})
    assert at_ceiling.status_code == 200
    assert len(fake.calls) == 2                       # …and the legal one did run


def test_an_oversized_session_name_is_refused(client):
    """The session-name field lost its `maxlength` too — same class, same file, same `>` refusal."""
    too_long = client.post("/sessions", data={"request_text": "x", "slug": "a" * (MAX_SLUG_CHARS + 1),
                                              "provider": "create_only"})
    assert too_long.status_code == 413
    assert not SessionService().list_sessions()

    at_ceiling = client.post("/sessions", data={"request_text": "x", "slug": "a" * MAX_SLUG_CHARS,
                                                "provider": "create_only"}, follow_redirects=False)
    assert at_ceiling.status_code == 303
    assert len(SessionService().list_sessions()) == 1


def test_no_rendered_field_clips_what_the_user_pasted(client, with_provider):
    """Invariant 3 (refuse, don't truncate) at the one place a real user meets it (#8)."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval",
                                   "provider": "anthropic"})
    home = client.get("/").text
    session = client.get("/sessions/leave-approval").text
    # Positive control first: an empty or errored render would satisfy "no maxlength" by rendering no fields at all, and the silence would read as a pass.
    assert 'name="request_text"' in home and 'name="slug"' in home
    assert 'name="answers"' in session
    for page_name, page in (("home", home), ("session", session)):
        assert "maxlength" not in page, f"the {page_name} page renders a field that silently clips input"


def test_no_template_carries_a_clipping_attribute():
    """The rendered check above only sees what those two routes produce."""
    templates = sorted(TEMPLATES_DIR.rglob("*.html"))
    # Positive control: the scan found the form templates it is supposed to be reading.
    bodies = {p: p.read_text(encoding="utf-8") for p in templates}   # explicit: cp1252 can't read these
    assert len(bodies) >= 10
    assert any("<textarea" in body for body in bodies.values())
    clipping = sorted(p.name for p, body in bodies.items() if "maxlength" in body)
    assert not clipping, f"templates clip input client-side: {clipping}"


def test_an_unknown_context_card_is_refused(client):
    # Filtering it out would leave an empty selection, which every reader treats as "all cards".
    r = client.post("/sessions", data={"request_text": "x", "provider": "create_only",
                                       "cards": ["no-such-card"]})
    assert r.status_code == 400
    assert not SessionService().list_sessions()
