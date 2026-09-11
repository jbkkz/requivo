"""Artifact write routes (#425, slice 2): generate (provider-backed) and save (the
external-reasoner path, no provider call)."""

from __future__ import annotations

import json

from requivo.services.artifacts import ArtifactService
from tests.api.conftest import seed_session

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
