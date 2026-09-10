"""Artifact read routes (#425, slice 1): the freshness listing and one artifact's content, in both
the JSON envelope and the raw `Accept: text/markdown` form. Generation (`POST`) is a later slice."""

from __future__ import annotations

from requivo.services.artifacts import ArtifactService
from tests.api.conftest import seed_session


def test_list_artifacts_is_empty_before_anything_is_saved(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/artifacts")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_list_artifacts_reports_freshness(client):
    seed_session("leave-approval")
    ArtifactService().save("leave-approval", "brief", "# Brief\n\ncontent", source_revision=1)
    resp = client.get("/api/v1/sessions/leave-approval/artifacts")
    assert resp.status_code == 200
    row = resp.json()["brief"]
    assert row["filename"] == "solution-assessment.md"
    assert row["stale"] is False
    assert row["revision"] == 1


def test_show_artifact_returns_the_json_envelope(client):
    seed_session("leave-approval")
    ArtifactService().save("leave-approval", "brief", "# Brief\n\ncontent", source_revision=1)
    resp = client.get("/api/v1/sessions/leave-approval/artifacts/brief")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "type": "brief", "filename": "solution-assessment.md", "source_revision": 1,
        "updated_at": body["updated_at"], "stale": False, "content": "# Brief\n\ncontent",
    }


def test_show_artifact_with_accept_markdown_returns_the_raw_document(client):
    seed_session("leave-approval")
    ArtifactService().save("leave-approval", "brief", "# Brief\n\ncontent", source_revision=1)
    resp = client.get("/api/v1/sessions/leave-approval/artifacts/brief",
                      headers={"Accept": "text/markdown"})
    assert resp.status_code == 200
    assert resp.text == "# Brief\n\ncontent"
    assert "text/markdown" in resp.headers["content-type"]


def test_show_artifact_404s_when_nothing_was_ever_saved(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/artifacts/brief")
    assert resp.status_code == 404
    assert resp.json()["code"] == "session_not_found"


def test_show_artifact_refuses_an_unknown_type(client):
    seed_session("leave-approval")
    resp = client.get("/api/v1/sessions/leave-approval/artifacts/not-a-real-type")
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_artifact_type"
