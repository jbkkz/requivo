"""Artifact routes (#425): the freshness listing, one artifact in its JSON envelope or as raw markdown,
the provider-backed generate and the external-reasoner save."""

from __future__ import annotations

import json
import logging

import pytest

from requivo.services.artifacts import ArtifactService
from tests.api.conftest import Spend, seed_session

PRD_REPLY = json.dumps({"title": "Leave approval -- PRD", "problem": "Approvals are lost in email."})
SAVED_PRD = "# PRD\n\nExternally reasoned."
BRIEF = "# Brief\n\ncontent"


@pytest.fixture
def saved_brief():
    slug = seed_session("leave-approval")
    ArtifactService().save(slug, "brief", BRIEF, source_revision=1)
    return slug


def test_list_artifacts_is_empty_before_anything_is_saved(client):
    seed_session("leave-approval")
    assert client.get("/api/v1/sessions/leave-approval/artifacts").json() == {}


def test_list_artifacts_reports_freshness(client, saved_brief):
    row = client.get(f"/api/v1/sessions/{saved_brief}/artifacts").json()["brief"]
    assert (row["filename"], row["stale"], row["revision"]) == ("solution-assessment.md", False, 1)


def test_show_artifact_returns_the_json_envelope(client, saved_brief):
    body = client.get(f"/api/v1/sessions/{saved_brief}/artifacts/brief").json()
    assert body == {"type": "brief", "filename": "solution-assessment.md", "source_revision": 1,
                    "updated_at": body["updated_at"], "stale": False, "content": BRIEF}


def test_show_artifact_with_accept_markdown_returns_the_raw_document(client, saved_brief):
    resp = client.get(f"/api/v1/sessions/{saved_brief}/artifacts/brief", headers={"Accept": "text/markdown"})
    assert resp.status_code == 200 and resp.text == BRIEF
    assert "text/markdown" in resp.headers["content-type"]


@pytest.mark.parametrize(("artifact_type", "status", "code"), [
    ("brief", 404, "session_not_found"),                 # nothing was ever saved
    ("not-a-real-type", 400, "unknown_artifact_type"),
])
def test_show_artifact_refuses_what_it_cannot_serve(client, artifact_type, status, code):
    seed_session("leave-approval")
    resp = client.get(f"/api/v1/sessions/leave-approval/artifacts/{artifact_type}")
    assert resp.status_code == status and resp.json()["code"] == code


@pytest.mark.parametrize("spend", [None, Spend(input_tokens=9000, output_tokens=3000, cache_read_input_tokens=400)],
                         ids=["unpriced", "priced"])
def test_generate_an_artifact_saves_it_and_reports_usage(client, with_provider, spend):
    """`usage` is `None` when the fake reports no figures, and populated when it does (found in review: every
    usage assertion here could once only observe `None`)."""
    slug = seed_session("leave-approval")
    with_provider(PRD_REPLY, spend=spend)
    body = client.post(f"/api/v1/sessions/{slug}/artifacts/prd").json()
    assert body["type"] == "prd" and body["status"]["revision"] == 1
    assert body["artifact"]["title"] == "Leave approval -- PRD"
    assert "Leave approval" in ArtifactService().show(slug, "prd")
    usage = body["usage"]
    if spend is None:
        assert usage is None
    else:
        assert (usage["calls"], usage["tokens"], usage["cached"]) == (1, 12400, 400)
        assert (usage["cost"] is None) != (usage["unpriced_reason"] is None)   # priced or unpriced, never both


def test_generate_an_unknown_artifact_type_is_refused(client, with_provider):
    slug = seed_session("leave-approval")
    fake = with_provider()  # an unknown type must never reach the provider
    resp = client.post(f"/api/v1/sessions/{slug}/artifacts/not-a-real-type")
    assert resp.status_code == 400 and resp.json()["code"] == "unknown_artifact_type"
    assert fake.calls == []


def test_generate_an_artifact_type_the_sessions_perimeter_does_not_own_is_refused_not_a_500(client, with_provider):
    """#609: the third reader of `GENERATABLE`."""
    from requivo.core.contracts import schema_slot_ids
    from requivo.core.perimeters import GO_TO_MARKET
    from requivo.services.sessions import SessionService

    fake = with_provider()
    svc = SessionService()
    meta = svc.create_session("grow the funnel", slug="gtm-api", perimeter=GO_TO_MARKET)
    model = {sid: {"completeness": 90, "confidence": "explicit", "impact": "high", "value": "x", "evidence": "y"}
             for sid in schema_slot_ids(GO_TO_MARKET)[1]}
    svc.update_model(meta.slug, json.dumps({"model": model, "questions": [], "summary": {"objective": "grow"}}),
                     expected_revision=0)
    resp = client.post(f"/api/v1/sessions/{meta.slug}/artifacts/prd")
    assert resp.status_code == 409, f"a real request must not 500; got {resp.status_code}"
    assert resp.json()["code"] == "artifact_type_not_owned"
    assert fake.calls == []


def test_save_an_artifact_records_its_source_revision(client):
    slug = seed_session("leave-approval")
    body = client.put(f"/api/v1/sessions/{slug}/artifacts/prd", json={"content": SAVED_PRD, "source_revision": 1}).json()
    assert (body["revision"], body["stale"]) == (1, False)
    assert ArtifactService().show(slug, "prd") == SAVED_PRD


def test_save_an_artifact_with_no_source_revision_is_refused(client):
    """The service's own refusal (#57): the 400 comes from `ArtifactService.save`, the route adds no rule."""
    slug = seed_session("leave-approval")
    resp = client.put(f"/api/v1/sessions/{slug}/artifacts/prd", json={"content": "# PRD"})
    assert resp.status_code == 400 and resp.json()["code"] == "unstated_source_revision"


def test_a_failed_paid_call_still_logs_what_it_spent(client, with_provider, caplog):
    """A call that failed after the provider answered is still billed: the retry loop's three attempts, one line."""
    slug = seed_session("leave-approval")
    with_provider("not json", "not json", "not json", spend=Spend(input_tokens=100, output_tokens=10))
    with caplog.at_level(logging.INFO, logger="requivo.api"):
        resp = client.post(f"/api/v1/sessions/{slug}/artifacts/prd")
    assert resp.status_code >= 400 and "usage" not in resp.json()
    logged = [rec.getMessage() for rec in caplog.records]
    assert any("api-prd spent 330 tokens" in line for line in logged), f"a paid failure was not recorded: {logged!r}"
