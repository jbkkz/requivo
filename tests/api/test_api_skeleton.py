"""Skeleton tests for the Requivo API factory (#425, slice 1): the missing-extra hint, OpenAPI on,
and the error handler translating a `RequivoError`, a FastAPI validation failure and an unexpected
exception into one JSON envelope."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from requivo.core.errors import RequivoError


def test_health_reports_ok(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "service": "requivo-api", "version": body["version"]}
    assert body["version"]


def test_openapi_docs_are_on_and_carry_the_experimental_notice(client):
    """Decision doc #425: OpenAPI ON for the API, the experimental notice on the description."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert "EXPERIMENTAL" in resp.json()["info"]["description"]
    assert client.get("/docs").status_code == 200


def test_the_web_apps_own_docs_stay_off():
    """Must-fire control for the assertion above -- without a sibling that keeps docs off, "docs
    are on" could be a FastAPI default both apps happen to share rather than a deliberate choice."""
    from requivo.web.app import create_app

    # loopback base_url, so the assertion is about `docs_url=None` and not about the cross-site
    # guard those routes would otherwise be caught by first.
    web_client = TestClient(create_app(), base_url="http://127.0.0.1:8765",
                            raise_server_exceptions=False)
    assert web_client.get("/openapi.json").status_code == 404


def test_a_requivo_error_is_reported_as_its_own_envelope(client):
    """A `RequivoError` raised inside a route -- here, a nonexistent session -- comes back as
    `{code, message}` at the status `http_status_for` assigns, never as a generic 500 or an HTML
    page: the same envelope every `--json` verb already publishes (decision doc #425, section 3)."""
    resp = client.get("/api/v1/sessions/no-such-session")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "session_not_found"
    assert "message" in body


def test_a_validation_failure_is_reported_generically(client):
    """A path parameter FastAPI itself refuses (a non-integer revision) becomes the same envelope
    shape as a `RequivoError`, not FastAPI's own raw 422 body -- "no second vocabulary" applies to
    the failure channel as much as to the success one."""
    resp = client.get("/api/v1/sessions/some-slug/revisions/not-a-number")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_the_missing_api_extra_reports_a_clean_hint(monkeypatch):
    """Mirrors `test_the_missing_web_extra_keeps_its_published_error_code` (tests/test_cli.py): a
    missing `[api]` extra must not traceback, and must publish the same `provider_unavailable` code
    every other optional-install refusal in this project already uses (`new_client()` for
    `[anthropic]`, `_cmd_web` for `[web]`)."""
    monkeypatch.setitem(sys.modules, "fastapi", None)
    from requivo.api.app import create_api

    with pytest.raises(RequivoError) as exc:
        create_api()
    assert exc.value.code == "provider_unavailable"
    assert "requivo[api]" in str(exc.value)
