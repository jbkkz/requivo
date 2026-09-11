"""Artifact write routes (#425, slice 2): generate (provider-backed) and save (the
external-reasoner path, no provider call)."""

from __future__ import annotations

import json
import logging

from requivo.services.artifacts import ArtifactService
from tests.api.conftest import Spend, seed_session

PRD_REPLY = json.dumps({"title": "Leave approval -- PRD", "problem": "Approvals are lost in email."})
SAVED_PRD = "# PRD\n\nExternally reasoned."


def test_generate_an_artifact_saves_it_and_reports_usage(client, with_provider):
    slug = seed_session("leave-approval")
    with_provider(PRD_REPLY)

    resp = client.post(f"/api/v1/sessions/{slug}/artifacts/prd")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "prd"
    assert body["status"]["revision"] == 1
    assert body["artifact"]["title"] == "Leave approval -- PRD"
    # The fake reports no usage figures (`FakeClient._FakeResponse.usage = None`), so this is the
    # "nothing to report" state, not a manufactured zero -- see `api/usage.py`'s own docstring.
    assert body["usage"] is None

    saved = ArtifactService().show(slug, "prd")
    assert "Leave approval" in saved


def test_generate_an_unknown_artifact_type_is_refused(client, with_provider):
    slug = seed_session("leave-approval")
    fake = with_provider()  # an unknown type must never reach the provider

    resp = client.post(f"/api/v1/sessions/{slug}/artifacts/not-a-real-type")
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_artifact_type"
    assert fake.calls == []


def test_save_an_artifact_records_its_source_revision(client):
    slug = seed_session("leave-approval")
    resp = client.put(f"/api/v1/sessions/{slug}/artifacts/prd",
                      json={"content": SAVED_PRD, "source_revision": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 1
    assert body["stale"] is False
    assert ArtifactService().show(slug, "prd") == SAVED_PRD


def test_save_an_artifact_with_no_source_revision_is_refused(client):
    """The service's own refusal (#57), unchanged: this route adds no requiredness of its own, so
    the 400 has to come from `ArtifactService.save` reaching its `UnstatedSourceRevisionError` arm."""
    slug = seed_session("leave-approval")
    resp = client.put(f"/api/v1/sessions/{slug}/artifacts/prd", json={"content": "# PRD"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "unstated_source_revision"


def test_generate_reports_a_populated_usage_object_when_the_provider_priced_the_call(client, with_provider):
    """The must-fire half of the `usage is None` assertion above (found in review: every usage
    assertion in this package could only ever observe `None`, so a regression that nulled out the
    figure for a priced call would have shipped green). 9000 + 400 + 3000 tokens, written out so the
    assertion is a claim about the arithmetic rather than a copy of whatever the code produced."""
    slug = seed_session("leave-approval")
    with_provider(PRD_REPLY, spend=Spend(input_tokens=9000, output_tokens=3000,
                                         cache_read_input_tokens=400))

    body = client.post(f"/api/v1/sessions/{slug}/artifacts/prd").json()
    usage = body["usage"]
    assert usage is not None
    assert usage["calls"] == 1
    assert usage["tokens"] == 12400
    assert usage["cached"] == 400
    # Priced or unpriced is the fake model's business, not this test's; the two are exclusive either way.
    assert (usage["cost"] is None) != (usage["unpriced_reason"] is None)


def test_a_failed_paid_call_still_logs_what_it_spent(client, with_provider, caplog):
    """A call that failed after the provider answered is still billed, and the API is not allowed
    to be the one surface on which that leaves no trace (found in review; the same contract as
    `test_a_failed_paid_call_still_records_what_it_spent` on the Web). The error envelope has no
    `usage`, so the operator's log is the only channel left -- it has to be written from a
    `finally`, which is what `track_api_usage` exists for."""
    slug = seed_session("leave-approval")
    # Three malformed replies: the JSON retry loop spends on every attempt, then gives up as a clean
    # `EngineError` -- a failure that reaches the recording exit, unlike one the fake itself raised.
    with_provider("not json", "not json", "not json",
                  spend=Spend(input_tokens=100, output_tokens=10))

    with caplog.at_level(logging.INFO, logger="requivo.api"):
        resp = client.post(f"/api/v1/sessions/{slug}/artifacts/prd")

    assert resp.status_code >= 400, "the failure still has to reach the caller as an error"
    assert "usage" not in resp.json()
    logged = [rec.getMessage() for rec in caplog.records]
    # One *operation* -- the ledger files the retry loop's three attempts as one billed call --
    # that spent 3 x 110 tokens, none of which the success body could report.
    assert any("api-prd spent 330 tokens" in line for line in logged), (
        "a paid call that failed was not recorded anywhere: " + repr(logged))
