"""The API factory and its session routes (#425): the envelope, the reads, the writes and the two
discovery turns -- each backed by one `SessionService` call, offline, over a tmp workspace."""

from __future__ import annotations

import json
import sys

import pytest
from _fakes import full_model
from fastapi.testclient import TestClient

from requivo.core import persistence as store
from requivo.core.contracts import Challenge, DesignDecision, EngineOutput
from requivo.core.errors import RequivoError
from requivo.services.sessions import SessionService
from tests.api.conftest import HIGH_EXPLICIT, engine_reply, seed_session


def _break(slug: str) -> None:
    store.create_session(slug, "a request")
    (store.canonical_dir(slug) / "session.json").write_text("not json", encoding="utf-8")


# ── the skeleton: health, docs, the one error envelope ─────────────────────────────────────────


def test_health_reports_ok(client):
    body = client.get("/api/v1/health").json()
    assert body == {"status": "ok", "service": "requivo-api", "version": body["version"]} and body["version"]


def test_openapi_docs_are_on_and_carry_the_experimental_notice(client):
    """Decision doc #425: OpenAPI on for the API, off for the web app (the must-fire control)."""
    from requivo.web.app import create_app

    assert "EXPERIMENTAL" in client.get("/openapi.json").json()["info"]["description"]
    assert client.get("/docs").status_code == 200
    web = TestClient(create_app(), base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    assert web.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(("path", "status", "code"), [
    ("/api/v1/sessions/no-such-session", 404, "session_not_found"),                   # a RequivoError
    ("/api/v1/sessions/some-slug/revisions/not-a-number", 400, "invalid_request"),     # FastAPI's own 422
])
def test_a_refusal_is_reported_as_the_one_json_envelope(client, path, status, code):
    resp = client.get(path)
    assert resp.status_code == status
    assert resp.json()["code"] == code and "message" in resp.json()


def test_the_missing_api_extra_reports_a_clean_hint(monkeypatch):
    """Mirrors `test_the_missing_web_extra_keeps_its_published_error_code` (tests/test_cli.py)."""
    monkeypatch.setitem(sys.modules, "fastapi", None)
    from requivo.api.app import create_api

    with pytest.raises(RequivoError) as exc:
        create_api()
    assert exc.value.code == "provider_unavailable" and "requivo[api]" in str(exc.value)


# ── reads ───────────────────────────────────────────────────────────────────────────────────────


def test_list_sessions_is_empty_over_a_fresh_workspace(client):
    assert client.get("/api/v1/sessions").json() == {"sessions": [], "degraded": 0}


def test_list_sessions_degrades_an_unreadable_entry_rather_than_failing_the_set(client):
    """Invariant 15: one broken session must not take the whole listing down."""
    seed_session("good-session")
    _break("broken-session")
    body = client.get("/api/v1/sessions").json()
    assert body["degraded"] == 1
    rows = {r["slug"]: r for r in body["sessions"]}
    good, broken = rows["good-session"], rows["broken-session"]
    assert good["readable"] is True and good["revision"] == 1 and good["error"] is None
    assert broken["readable"] is False and broken["error"]
    assert broken["revision"] is None and broken["provider"] is None and broken["updated_at"] is None


def test_the_api_list_row_is_the_same_row_session_list_json_publishes(client):
    """`api/routes/sessions.py` restates `lifecycle.py`'s `_session_list_row` rather than importing across
    two optional extras (#425); the route is built from it, not merely agreeing with it."""
    from requivo.api.routes.sessions import _session_list_row as api_row
    from requivo.deterministic.sessions.lifecycle import _session_list_row as cli_row

    seed_session("good-session")
    _break("broken-session")
    entries = SessionService().list_entries()
    assert {e.readable for e in entries} == {True, False}, "the fixture needs one readable and one degraded entry"
    for entry in entries:
        assert api_row(entry) == cli_row(entry), f"the API and `session list --json` disagree on {entry.slug!r}"
    served = {r["slug"]: r for r in client.get("/api/v1/sessions").json()["sessions"]}
    assert served == {e.slug: cli_row(e) for e in entries}


@pytest.mark.parametrize(("route", "check"), [
    ("", lambda b: b["slug"] == "leave-approval" and b["current_revision"] == 1
                   and "artifact_status" in b and "session_id" in b),
    ("/model", lambda b: b["model"]["business_rules"]["completeness"] == 90),
    ("/revisions", lambda b: [r["revision"] for r in b["revisions"]] == [1] and "created_at" in b["revisions"][0]),
    ("/revisions/1", lambda b: "model" in b),
    ("/status", lambda b: b["slug"] == "leave-approval" and b["revision"] == 1 and "readiness" in b),
], ids=["session", "model", "revisions", "revision", "status"])
def test_a_read_route_returns_its_cli_json_shape(client, route, check):
    seed_session("leave-approval", business_rules=HIGH_EXPLICIT)
    resp = client.get(f"/api/v1/sessions/leave-approval{route}")
    assert resp.status_code == 200
    assert check(resp.json()), resp.json()


def test_get_revision_out_of_range_is_a_404(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/revisions/99")
    assert resp.status_code == 404 and resp.json()["code"] == "session_not_found"


# ── impact ──────────────────────────────────────────────────────────────────────────────────────


def test_impact_reports_what_rests_on_a_named_slot(client):
    """The must-fire half: a slot a real decision was derived from shows up in the report."""
    slug = "impact-session"
    store.create_session(slug, "a request")
    model = EngineOutput.model_validate({
        **full_model(workflow={**HIGH_EXPLICIT, "completeness": 80}), "summary": {"objective": "Invoice lifecycle"},
        "decisions": [DesignDecision(decision="Draft-first invoices reviewed by Finance", derived_from=["workflow"]).model_dump()],
        "challenges": [Challenge(headline="Invoice at signature", premise="p", alternative="a", consequence="c",
                                 recommendation="r", contests=["workflow"]).model_dump()],
    })
    SessionService().update_model(slug, json.dumps(model.model_dump()))
    body = client.get(f"/api/v1/sessions/{slug}/impact", params={"slots": "workflow"}).json()
    assert any("Draft-first invoices reviewed by Finance" in d["decision"] for d in body["decisions"])
    assert any("Invoice at signature" in c["headline"] for c in body["challenges"])


def test_impact_reports_no_decisions_or_challenges_for_a_slot_nothing_rests_on(client):
    """The must-not-fire control for the test above."""
    seed_session("leave-approval")
    body = client.get("/api/v1/sessions/leave-approval/impact", params={"slots": "current_process"}).json()
    assert body["decisions"] == [] and body["challenges"] == []


def test_impact_with_a_blank_slots_value_is_the_empty_report_not_a_refusal(client):
    """Review find (#425): a blank `slots` is the empty report. `evidence` counts what the #493 review
    examined, so a review that looked at nothing cannot pass as one that did."""
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/impact", params={"slots": ""})
    assert resp.status_code == 200
    assert resp.json() == {"changed": [], "decisions": [], "challenges": [], "exclusions": [], "thresholds": [],
                           "artifacts": [], "evidence": {"reviewed": 0, "flagged": [], "could_not_tell": []}}


@pytest.mark.parametrize(("params", "code"), [
    ({"slots": "not-a-real-slot"}, "unknown_slot"),
    ({}, "invalid_request"),
    ({"slots": "permissions,,workflow"}, "empty_selector_token"),   # a blank *token* is still malformed
])
def test_impact_refuses_a_bad_selector(client, params, code):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/impact", params=params)
    assert resp.status_code == 400 and resp.json()["code"] == code


# ── writes ──────────────────────────────────────────────────────────────────────────────────────


def test_create_session_is_201_and_fresh(client):
    resp = client.post("/api/v1/sessions", json={"request": "A leave approval system."})
    assert resp.status_code == 201
    assert resp.json()["current_revision"] == 0 and resp.json()["slug"] and "session_id" in resp.json()


def test_repeating_the_same_identity_is_200_not_201(client):
    """Idempotent by identity (invariant 11): the same request and card selection is the same session."""
    body = {"request": "A leave approval system.", "slug": "leave-approval"}
    first, second = client.post("/api/v1/sessions", json=body), client.post("/api/v1/sessions", json=body)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["slug"] == "leave-approval"
    assert second.json()["session_id"] == first.json()["session_id"]


def test_an_explicit_slug_taken_by_a_different_identity_is_409(client):
    client.post("/api/v1/sessions", json={"request": "A leave approval system.", "slug": "s"})
    resp = client.post("/api/v1/sessions", json={"request": "A totally different request.", "slug": "s"})
    assert resp.status_code == 409 and resp.json()["code"] == "session_exists"


@pytest.mark.parametrize("preview", [False, True], ids=["apply", "preview"])
def test_a_revision_is_applied_or_only_planned(client, preview):
    slug = seed_session("leave-approval")
    proposal = full_model(business_rules=HIGH_EXPLICIT)
    path = f"/api/v1/sessions/{slug}/revisions" + ("/preview" if preview else "")
    body = client.post(path, json={"proposal": proposal}).json()
    assert body["status"] == ("planned" if preview else "applied")
    assert "business_rules" in body["changed_slots"]
    assert SessionService().meta(slug).current_revision == (1 if preview else 2)


def test_apply_a_revision_with_a_stale_expected_revision_is_409(client):
    slug = seed_session("leave-approval")
    resp = client.post(f"/api/v1/sessions/{slug}/revisions", json={"proposal": full_model(), "expected_revision": 0})
    assert resp.status_code == 409 and resp.json()["code"] == "revision_conflict"


def test_rescope_updates_the_context_card_selection(client):
    slug = seed_session("leave-approval")  # no explicit cards: the "every card" selection
    body = client.put(f"/api/v1/sessions/{slug}/context-cards", json={"context_cards": ["document-management"]}).json()
    assert (body["slug"], body["context_cards"], body["changed"]) == (slug, ["document-management"], True)


def test_rescope_to_the_current_selection_is_a_no_op(client):
    """The must-not-fire control for the test above (#168)."""
    slug = seed_session("leave-approval")
    resp = client.put(f"/api/v1/sessions/{slug}/context-cards", json={"context_cards": None})
    assert resp.status_code == 200 and resp.json()["changed"] is False


# ── discovery ───────────────────────────────────────────────────────────────────────────────────


def test_discover_runs_the_first_turn(client, with_provider):
    with_provider(engine_reply(business_rules=HIGH_EXPLICIT))
    slug = client.post("/api/v1/sessions", json={"request": "A leave approval system."}).json()["slug"]
    body = client.post(f"/api/v1/sessions/{slug}/discover").json()
    assert body["status"] == "applied" and body["revision"] == 1
    assert SessionService().meta(slug).current_revision == 1


def test_answer_folds_into_the_model_as_a_new_revision(client, with_provider):
    """The must-fire control for the refusals below: the current revision reaches the provider once."""
    slug = seed_session("leave-approval")
    fake = with_provider(engine_reply(business_rules=HIGH_EXPLICIT))
    body = client.post(f"/api/v1/sessions/{slug}/answers",
                       json={"answers": "Exceptions go to Finance.", "expected_revision": 1}).json()
    assert body["status"] == "applied" and body["revision"] == 2
    assert len(fake.calls) == 1


@pytest.mark.parametrize(("route", "body", "status", "code"), [
    ("/discover", None, 409, "revision_conflict"),                                   # invariant 13
    ("/answers", {"answers": "Exceptions go to Finance."}, 400, "invalid_request"),   # no expected_revision
    ("/answers", {"answers": "Exceptions go to Finance.", "expected_revision": 0}, 409, "revision_conflict"),  # #205
], ids=["discover-above-zero", "answer-without-precondition", "answer-stale-precondition"])
def test_a_turn_that_cannot_land_is_refused_before_any_provider_call(client, with_provider, route, body, status, code):
    slug = seed_session("leave-approval")  # already at revision 1
    fake = with_provider()  # no replies queued: a reached call raises, rather than merely costing money
    resp = client.post(f"/api/v1/sessions/{slug}{route}", json=body)
    assert resp.status_code == status and resp.json()["code"] == code
    assert fake.calls == []


def test_discover_reports_session_locked_with_a_retry_after_header(client, app):
    """503 `session_locked` with `Retry-After`: the write never started, so resubmitting is the recovery."""
    from requivo.api.dependencies import get_discovery
    from requivo.core.errors import SessionLockedError

    class _LockedDiscovery:
        def run_discovery(self, slug, *, surface):
            raise SessionLockedError(f"session '{slug}' is locked by another process", details={"slug": slug})

    app.dependency_overrides[get_discovery] = lambda: _LockedDiscovery()
    resp = client.post("/api/v1/sessions/locked-session/discover")   # never created: the fake never reads the store
    assert resp.status_code == 503 and resp.json()["code"] == "session_locked"
    assert resp.headers["retry-after"] == "1"

