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
from tests.api.conftest import HIGH_EXPLICIT, SESSIONS, engine_reply, refused, seed_session

REQUEST = {"request": "A leave approval system."}
EMPTY_IMPACT = {"changed": [], "decisions": [], "challenges": [], "exclusions": [], "thresholds": [], "artifacts": [],
                "evidence": {"reviewed": 0, "flagged": [], "could_not_tell": []}}


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
    ("/no-such-session", 404, "session_not_found"),                   # a RequivoError
    ("/some-slug/revisions/not-a-number", 400, "invalid_request"),     # FastAPI's own 422
    ("/leave-approval/revisions/99", 404, "session_not_found"),        # out of range on a real session
], ids=["missing", "not-a-number", "out-of-range"])
def test_a_refusal_is_reported_as_the_one_json_envelope(client, path, status, code):
    seed_session()
    refused(client.get(f"{SESSIONS}{path}"), status, code)


def test_the_missing_api_extra_reports_a_clean_hint(monkeypatch):
    """Mirrors `test_the_missing_web_extra_keeps_its_published_error_code` (tests/test_cli.py)."""
    monkeypatch.setitem(sys.modules, "fastapi", None)
    from requivo.api.app import create_api

    with pytest.raises(RequivoError) as exc:
        create_api()
    assert exc.value.code == "provider_unavailable" and "requivo[api]" in str(exc.value)


# ── reads ───────────────────────────────────────────────────────────────────────────────────────


def test_list_sessions_degrades_an_unreadable_entry_rather_than_failing_the_set(client):
    """Invariant 15: one broken session must not take the listing down, and each row is the very
    `_session_list_row` `session list --json` publishes (restated across two extras, #425)."""
    from requivo.api.routes.sessions import _session_list_row as api_row
    from requivo.deterministic.sessions.lifecycle import _session_list_row as cli_row

    assert client.get(SESSIONS).json() == {"sessions": [], "degraded": 0}
    seed_session("good-session")
    _break("broken-session")
    body = client.get(SESSIONS).json()
    rows = {r["slug"]: r for r in body["sessions"]}
    good, broken = rows["good-session"], rows["broken-session"]
    assert body["degraded"] == 1 and good["readable"] and good["revision"] == 1 and good["error"] is None
    assert not broken["readable"] and broken["error"]
    assert (broken["revision"], broken["provider"], broken["updated_at"]) == (None, None, None)
    entries = SessionService().list_entries()
    assert {e.readable for e in entries} == {True, False}
    assert rows == {e.slug: cli_row(e) for e in entries} == {e.slug: api_row(e) for e in entries}


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
    resp = client.get(f"{SESSIONS}/leave-approval{route}")
    assert resp.status_code == 200 and check(resp.json()), resp.json()


# ── impact ──────────────────────────────────────────────────────────────────────────────────────


def test_impact_reports_what_rests_on_a_named_slot(client):
    """The must-fire half: a slot a real decision was derived from shows up; the must-not-fire control is
    a slot of the same session nothing rests on."""
    slug = "impact-session"
    store.create_session(slug, "a request")
    model = EngineOutput.model_validate({
        **full_model(workflow={**HIGH_EXPLICIT, "completeness": 80}), "summary": {"objective": "Invoice lifecycle"},
        "decisions": [DesignDecision(decision="Draft-first invoices reviewed by Finance", derived_from=["workflow"]).model_dump()],
        "challenges": [Challenge(headline="Invoice at signature", premise="p", alternative="a", consequence="c",
                                 recommendation="r", contests=["workflow"]).model_dump()],
    })
    SessionService().update_model(slug, json.dumps(model.model_dump()))
    body = client.get(f"{SESSIONS}/{slug}/impact", params={"slots": "workflow"}).json()
    assert any("Draft-first invoices reviewed by Finance" in d["decision"] for d in body["decisions"])
    assert any("Invoice at signature" in c["headline"] for c in body["challenges"])
    control = client.get(f"{SESSIONS}/{slug}/impact", params={"slots": "current_process"}).json()
    assert control["decisions"] == [] and control["challenges"] == []


def test_impact_with_a_blank_slots_value_is_the_empty_report_not_a_refusal(client):
    """Review find (#425): a blank `slots` is the empty report, `evidence.reviewed` counting what the #493 review examined."""
    seed_session()
    resp = client.get(f"{SESSIONS}/leave-approval/impact", params={"slots": ""})
    assert resp.status_code == 200 and resp.json() == EMPTY_IMPACT


@pytest.mark.parametrize(("params", "code"), [
    ({"slots": "not-a-real-slot"}, "unknown_slot"),
    ({}, "invalid_request"),
    ({"slots": "permissions,,workflow"}, "empty_selector_token"),   # a blank *token* is still malformed
])
def test_impact_refuses_a_bad_selector(client, params, code):
    seed_session()
    refused(client.get(f"{SESSIONS}/leave-approval/impact", params=params), 400, code)


# ── writes ──────────────────────────────────────────────────────────────────────────────────────


def test_create_session_is_201_fresh_200_on_the_same_identity_and_409_on_another(client):
    """Idempotent by identity (invariant 11): the same request and card selection is the same session."""
    first = client.post(SESSIONS, json={**REQUEST, "slug": "leave-approval"})
    assert first.status_code == 201 and first.json()["current_revision"] == 0 and "session_id" in first.json()
    second = client.post(SESSIONS, json={**REQUEST, "slug": "leave-approval"})
    assert second.status_code == 200 and second.json()["session_id"] == first.json()["session_id"]
    refused(client.post(SESSIONS, json={"request": "A totally different request.", "slug": "leave-approval"}), 409, "session_exists")


@pytest.mark.parametrize("preview", [False, True], ids=["apply", "preview"])
def test_a_revision_is_applied_or_only_planned(client, preview):
    slug = seed_session()
    path = f"{SESSIONS}/{slug}/revisions" + ("/preview" if preview else "")
    body = client.post(path, json={"proposal": full_model(business_rules=HIGH_EXPLICIT)}).json()
    assert body["status"] == ("planned" if preview else "applied") and "business_rules" in body["changed_slots"]
    assert SessionService().meta(slug).current_revision == (1 if preview else 2)


def test_apply_a_revision_with_a_stale_expected_revision_is_409(client):
    slug = seed_session()
    resp = client.post(f"{SESSIONS}/{slug}/revisions", json={"proposal": full_model(), "expected_revision": 0})
    refused(resp, 409, "revision_conflict")


def test_rescope_to_the_current_selection_is_a_no_op(client):
    """The must-not-fire control (#168) first, on the "every card" selection; then a real change."""
    slug = seed_session()
    resp = client.put(f"{SESSIONS}/{slug}/context-cards", json={"context_cards": None})
    assert resp.status_code == 200 and resp.json()["changed"] is False
    body = client.put(f"{SESSIONS}/{slug}/context-cards", json={"context_cards": ["document-management"]}).json()
    assert (body["slug"], body["context_cards"], body["changed"]) == (slug, ["document-management"], True)


# ── discovery ───────────────────────────────────────────────────────────────────────────────────


def test_discover_runs_the_first_turn(client, with_provider):
    with_provider(engine_reply(business_rules=HIGH_EXPLICIT))
    slug = client.post(SESSIONS, json=REQUEST).json()["slug"]
    body = client.post(f"{SESSIONS}/{slug}/discover").json()
    assert (body["status"], body["revision"], SessionService().meta(slug).current_revision) == ("applied", 1, 1)


def test_answer_folds_into_the_model_as_a_new_revision(client, with_provider):
    """The must-fire control for the refusals below: the current revision reaches the provider once."""
    slug = seed_session()
    fake = with_provider(engine_reply(business_rules=HIGH_EXPLICIT))
    body = client.post(f"{SESSIONS}/{slug}/answers", json={"answers": "Exceptions go to Finance.", "expected_revision": 1}).json()
    assert (body["status"], body["revision"], len(fake.calls)) == ("applied", 2, 1)


@pytest.mark.parametrize(("route", "body", "status", "code"), [
    ("/discover", None, 409, "revision_conflict"),                                   # invariant 13
    ("/answers", {"answers": "Exceptions go to Finance."}, 400, "invalid_request"),   # no expected_revision
    ("/answers", {"answers": "Exceptions go to Finance.", "expected_revision": 0}, 409, "revision_conflict"),  # #205
], ids=["discover-above-zero", "answer-without-precondition", "answer-stale-precondition"])
def test_a_turn_that_cannot_land_is_refused_before_any_provider_call(client, with_provider, route, body, status, code):
    slug = seed_session()  # already at revision 1
    fake = with_provider()  # no replies queued: a reached call raises, rather than merely costing money
    refused(client.post(f"{SESSIONS}/{slug}{route}", json=body), status, code)
    assert fake.calls == []


def test_discover_reports_session_locked_with_a_retry_after_header(client, app):
    """503 `session_locked` with `Retry-After`: the write never started, so resubmitting is the recovery."""
    from requivo.api.dependencies import get_discovery
    from requivo.core.errors import SessionLockedError

    class _LockedDiscovery:
        def run_discovery(self, slug, *, surface):
            raise SessionLockedError(f"session '{slug}' is locked by another process", details={"slug": slug})

    app.dependency_overrides[get_discovery] = lambda: _LockedDiscovery()
    resp = client.post(f"{SESSIONS}/locked-session/discover")   # never created: the fake never reads the store
    refused(resp, 503, "session_locked")
    assert resp.headers["retry-after"] == "1"
