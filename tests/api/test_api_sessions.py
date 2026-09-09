"""Session read routes (#425, slice 1): list, one session, its model, its revisions, its status,
and its impact query -- each backed by exactly one `SessionService` call, offline, over a tmp
workspace."""

from __future__ import annotations

import json

from requivo.core import persistence as store
from requivo.core.contracts import Challenge, DesignDecision, EngineOutput
from requivo.services.sessions import SessionService
from tests._fakes import full_slots
from tests.api.conftest import seed_session


def test_list_sessions_is_empty_over_a_fresh_workspace(client):
    resp = client.get("/api/v1/sessions")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": [], "degraded": 0}


def test_list_sessions_reports_a_readable_row(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] == 0
    [row] = body["sessions"]
    assert row["slug"] == "leave-approval"
    assert row["revision"] == 1
    assert row["readable"] is True
    assert row["error"] is None


def test_list_sessions_degrades_an_unreadable_entry_rather_than_failing_the_set(client):
    """Invariant 15: one broken session must not take the whole listing down, and the degraded row
    states no fact it could not read."""
    seed_session("good-session")
    store.create_session("broken-session", "a request")
    (store.canonical_dir("broken-session") / "session.json").write_text("not json", encoding="utf-8")

    resp = client.get("/api/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] == 1
    rows = {r["slug"]: r for r in body["sessions"]}
    assert rows["good-session"]["readable"] is True
    broken = rows["broken-session"]
    assert broken["readable"] is False
    assert broken["revision"] is None and broken["provider"] is None and broken["updated_at"] is None
    assert broken["error"]


def test_get_session_returns_the_session_show_json_shape(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "leave-approval"
    assert body["current_revision"] == 1
    assert "artifact_status" in body
    assert "session_id" in body


def test_get_session_404s_for_a_nonexistent_slug(client):
    resp = client.get("/api/v1/sessions/no-such-session")
    assert resp.status_code == 404
    assert resp.json()["code"] == "session_not_found"


def test_get_model_returns_the_current_model(client):
    seed_session("leave-approval", business_rules={"completeness": 90, "confidence": "explicit",
                                                    "impact": "high"})
    resp = client.get("/api/v1/sessions/leave-approval/model")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"]["business_rules"]["completeness"] == 90


def test_list_revisions_returns_the_provenance_log(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/revisions")
    assert resp.status_code == 200
    [rev] = resp.json()["revisions"]
    assert rev["revision"] == 1
    assert "created_at" in rev


def test_get_revision_returns_a_historical_model(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/revisions/1")
    assert resp.status_code == 200
    assert "model" in resp.json()


def test_get_revision_out_of_range_is_a_404(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/revisions/99")
    assert resp.status_code == 404
    assert resp.json()["code"] == "session_not_found"


def test_get_status_returns_the_status_json_shape(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "leave-approval"
    assert body["revision"] == 1
    assert "readiness" in body


def test_impact_reports_what_rests_on_a_named_slot(client):
    """The must-fire half: a slot a real decision was derived from must show up in the report."""
    svc = SessionService()
    slug = "impact-session"
    store.create_session(slug, "a request")
    model = EngineOutput.model_validate({
        "model": full_slots(workflow={"completeness": 80, "confidence": "explicit", "impact": "high"}),
        "questions": [], "summary": {"objective": "Invoice lifecycle"},
        "decisions": [DesignDecision(decision="Draft-first invoices reviewed by Finance",
                                     derived_from=["workflow"]).model_dump()],
        "challenges": [Challenge(headline="Invoice at signature", premise="p", alternative="a",
                                 consequence="c", recommendation="r",
                                 contests=["workflow"]).model_dump()],
    })
    svc.update_model(slug, json.dumps(model.model_dump()))

    resp = client.get(f"/api/v1/sessions/{slug}/impact", params={"slots": "workflow"})
    assert resp.status_code == 200
    body = resp.json()
    assert any("Draft-first invoices reviewed by Finance" in d["decision"] for d in body["decisions"])
    assert any("Invoice at signature" in c["headline"] for c in body["challenges"])


def test_impact_reports_no_decisions_or_challenges_for_a_slot_nothing_rests_on(client):
    """The must-not-fire control for the test above: a slot no decision or challenge targets
    returns neither, rather than a copy of the last query's report. `current_process` is a real
    schema slot outside every decision/challenge this session carries -- `artifacts` still names
    `brief`, which maps to every slot deliberately (a judgment over the whole model), so this
    checks the two collections that are supposed to be selective."""
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/impact", params={"slots": "current_process"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decisions"] == [] and body["challenges"] == []


def test_impact_refuses_an_unknown_slot(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/impact", params={"slots": "not-a-real-slot"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_slot"


def test_impact_requires_the_slots_query_parameter(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/impact")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_impact_with_a_blank_slots_value_is_the_empty_report_not_a_refusal(client):
    """Found in review (#425): a first draft split `slots.split(",")` unconditionally, so a present
    but blank `?slots=` produced `['']` -- one empty token -- and was refused as
    `empty_selector_token` rather than reaching `SessionService.impact`'s documented "no slots named"
    behaviour. This is the one route onto that behaviour, and it must actually answer 200."""
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/impact", params={"slots": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"changed": [], "decisions": [], "challenges": [], "artifacts": []}


def test_impact_still_refuses_a_genuinely_malformed_list(client):
    """The must-not-fire control for the fix above: a blank *overall* value is forgiven, but a blank
    token *inside* an otherwise real list (a stray comma) is still refused -- the fix narrows the
    refusal, it does not remove it."""
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/impact",
                      params={"slots": "permissions,,workflow"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "empty_selector_token"
