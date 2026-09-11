"""Session write routes (#425, slice 2): create, the apply, its dry run, and the context-card
rescope -- each backed by exactly one `SessionService` call, offline, over a tmp workspace."""

from __future__ import annotations

from requivo.services.sessions import SessionService
from tests._fakes import full_slots
from tests.api.conftest import seed_session


def test_create_session_is_201_and_fresh(client):
    resp = client.post("/api/v1/sessions", json={"request": "A leave approval system."})
    assert resp.status_code == 201
    body = resp.json()
    assert body["current_revision"] == 0
    assert "session_id" in body
    assert body["slug"]


def test_create_session_with_an_explicit_slug_uses_it(client):
    resp = client.post("/api/v1/sessions",
                       json={"request": "A leave approval system.", "slug": "leave-approval"})
    assert resp.status_code == 201
    assert resp.json()["slug"] == "leave-approval"


def test_repeating_the_same_identity_is_200_not_201(client):
    """Idempotent by identity (invariant 11): a repeat call carrying the same request and the same
    (absent) card selection returns the *same* session, 200 rather than the first call's 201."""
    first = client.post("/api/v1/sessions",
                        json={"request": "A leave approval system.", "slug": "leave-approval"})
    assert first.status_code == 201
    second = client.post("/api/v1/sessions",
                         json={"request": "A leave approval system.", "slug": "leave-approval"})
    assert second.status_code == 200
    assert second.json()["slug"] == first.json()["slug"]
    assert second.json()["session_id"] == first.json()["session_id"]


def test_an_explicit_slug_taken_by_a_different_identity_is_409(client):
    client.post("/api/v1/sessions", json={"request": "A leave approval system.", "slug": "s"})
    resp = client.post("/api/v1/sessions", json={"request": "A totally different request.", "slug": "s"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "session_exists"


def test_apply_a_revision_returns_the_update_result(client):
    slug = seed_session("leave-approval")
    proposal = {"model": full_slots(business_rules={"completeness": 90, "confidence": "explicit",
                                                     "impact": "high"}),
                "questions": [], "summary": {"objective": "A leave approval system"}}
    resp = client.post(f"/api/v1/sessions/{slug}/revisions", json={"proposal": proposal})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied"
    assert body["revision"] == 2
    assert "business_rules" in body["changed_slots"]


def test_apply_a_revision_with_a_stale_expected_revision_is_409(client):
    slug = seed_session("leave-approval")
    proposal = {"model": full_slots(), "questions": [], "summary": {"objective": "A leave approval system"}}
    resp = client.post(f"/api/v1/sessions/{slug}/revisions",
                       json={"proposal": proposal, "expected_revision": 0})
    assert resp.status_code == 409
    assert resp.json()["code"] == "revision_conflict"


def test_preview_a_revision_writes_nothing(client):
    slug = seed_session("leave-approval")
    proposal = {"model": full_slots(business_rules={"completeness": 90, "confidence": "explicit",
                                                     "impact": "high"}),
                "questions": [], "summary": {"objective": "A leave approval system"}}
    resp = client.post(f"/api/v1/sessions/{slug}/revisions/preview", json={"proposal": proposal})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "planned"
    # Nothing written: the session's real revision is unmoved.
    assert SessionService().meta(slug).current_revision == 1


def test_rescope_updates_the_context_card_selection(client):
    slug = seed_session("leave-approval")  # created with no explicit cards -- the "every card" selection
    resp = client.put(f"/api/v1/sessions/{slug}/context-cards",
                      json={"context_cards": ["document-management"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == slug
    assert body["context_cards"] == ["document-management"]
    assert body["changed"] is True


def test_rescope_to_the_current_selection_is_a_no_op(client):
    """The must-not-fire control for the test above: re-scoping to what a session already has changes
    nothing (#168's own documented no-op), rather than every PUT reporting `changed: True`."""
    slug = seed_session("leave-approval")
    resp = client.put(f"/api/v1/sessions/{slug}/context-cards", json={"context_cards": None})
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
