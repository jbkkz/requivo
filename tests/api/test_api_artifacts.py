"""Artifact routes (#425): the freshness listing, one artifact in its JSON envelope or as raw markdown,
the provider-backed generate and the external-reasoner save."""

from __future__ import annotations

import json
import logging

import pytest

from requivo.services.artifacts import ArtifactService
from tests.api.conftest import SESSIONS, Spend, refused, seed_session

PRD_REPLY = json.dumps({"title": "Leave approval -- PRD", "problem": "Approvals are lost in email."})
SAVED_PRD = "# PRD\n\nExternally reasoned."
BRIEF = "# Brief\n\ncontent"


@pytest.fixture
def saved_brief():
    slug = seed_session()
    ArtifactService().save(slug, "brief", BRIEF, source_revision=1)
    return f"{SESSIONS}/{slug}/artifacts"


def test_list_artifacts_reports_freshness_and_is_empty_before_anything_is_saved(client, saved_brief):
    row = client.get(saved_brief).json()["brief"]
    assert (row["filename"], row["stale"], row["revision"]) == ("solution-assessment.md", False, 1)
    assert client.get(f"{SESSIONS}/{seed_session('bare')}/artifacts").json() == {}


def test_show_artifact_returns_the_json_envelope_or_the_raw_document_by_accept(client, saved_brief):
    body = client.get(f"{saved_brief}/brief").json()
    assert body == {"type": "brief", "filename": "solution-assessment.md", "source_revision": 1,
                    "updated_at": body["updated_at"], "stale": False, "content": BRIEF}
    resp = client.get(f"{saved_brief}/brief", headers={"Accept": "text/markdown"})
    assert resp.status_code == 200 and resp.text == BRIEF and "text/markdown" in resp.headers["content-type"]


@pytest.mark.parametrize(("artifact_type", "status", "code"), [
    ("brief", 404, "session_not_found"),                 # nothing was ever saved
    ("not-a-real-type", 400, "unknown_artifact_type"),
])
def test_show_artifact_refuses_what_it_cannot_serve(client, artifact_type, status, code):
    refused(client.get(f"{SESSIONS}/{seed_session()}/artifacts/{artifact_type}"), status, code)


@pytest.mark.parametrize("spend", [None, Spend(input_tokens=9000, output_tokens=3000, cache_read_input_tokens=400)],
                         ids=["unpriced", "priced"])
def test_generate_an_artifact_saves_it_and_reports_usage(client, with_provider, spend):
    """`usage` is `None` when the fake reports no figures, and populated when it does (found in review: every
    usage assertion here could once only observe `None`)."""
    slug = seed_session()
    with_provider(PRD_REPLY, spend=spend)
    body = client.post(f"{SESSIONS}/{slug}/artifacts/prd").json()
    assert body["type"] == "prd" and body["status"]["revision"] == 1 and body["artifact"]["title"] == "Leave approval -- PRD"
    assert "Leave approval" in ArtifactService().show(slug, "prd")
    usage = body["usage"]
    if spend is None:
        assert usage is None
    else:
        assert (usage["calls"], usage["tokens"], usage["cached"]) == (1, 12400, 400)
        assert (usage["cost"] is None) != (usage["unpriced_reason"] is None)   # priced or unpriced, never both


def test_generate_an_unknown_artifact_type_is_refused(client, with_provider):
    fake = with_provider()  # an unknown type must never reach the provider
    refused(client.post(f"{SESSIONS}/{seed_session()}/artifacts/not-a-real-type"), 400, "unknown_artifact_type")
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
    svc.update_model(meta.slug, json.dumps({"model": model, "questions": [], "summary": {"objective": "grow"}}), expected_revision=0)
    refused(client.post(f"{SESSIONS}/{meta.slug}/artifacts/prd"), 409, "artifact_type_not_owned")
    assert fake.calls == []


def test_save_an_artifact_records_its_source_revision_and_refuses_one_left_unstated(client):
    """The 400 is the service's own refusal (#57): `ArtifactService.save`, the route adds no rule."""
    path = f"{SESSIONS}/{seed_session()}/artifacts/prd"
    body = client.put(path, json={"content": SAVED_PRD, "source_revision": 1}).json()
    assert (body["revision"], body["stale"]) == (1, False) and ArtifactService().show("leave-approval", "prd") == SAVED_PRD
    refused(client.put(path, json={"content": "# PRD"}), 400, "unstated_source_revision")


def test_a_failed_paid_call_still_logs_what_it_spent(client, with_provider, caplog):
    """A call that failed after the provider answered is still billed: the retry loop's three attempts, one line."""
    slug = seed_session()
    with_provider("not json", "not json", "not json", spend=Spend(input_tokens=100, output_tokens=10))
    with caplog.at_level(logging.INFO, logger="requivo.api"):
        resp = client.post(f"{SESSIONS}/{slug}/artifacts/prd")
    assert resp.status_code >= 400 and "usage" not in resp.json()
    logged = [rec.getMessage() for rec in caplog.records]
    assert any("api-prd spent 330 tokens" in line for line in logged), f"a paid failure was not recorded: {logged!r}"
