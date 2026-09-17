"""Shared fixtures for the Requivo Web tests — no network, no real provider."""

from __future__ import annotations

import json

import pytest
from _fakes import FakeClient, Spend, full_slots, seed_session  # noqa: F401  (re-exported for the web suite)
from fastapi.testclient import TestClient

from requivo.services.discovery import DiscoveryService
from requivo.web.app import create_app
from requivo.web.dependencies import get_discovery
from requivo.web.security import CSRF_HEADER, csrf_token


def engine_reply(*, converged: bool = False, questions: list[dict] | None = None,
                 **slot_overrides) -> str:
    if questions is None:
        questions = [] if converged else [
            {"q": "How are exceptions handled?", "slot": "business_rules", "why": "uncertainty × impact"}]
    return json.dumps({
        "model": full_slots(**slot_overrides),
        "questions": questions,
        "summary": {"objective": "A leave approval system"},
    })


BRIEF_REPLY = json.dumps({"complexity": "medium", "problem": "P", "solution": "S",
                          "risks": ["a race on approval"], "next_steps": ["confirm exceptions"]})
PRD_REPLY = json.dumps({"title": "Leave approval — PRD", "problem": "Approvals are lost in email."})
CRITERIA_REPLY = json.dumps({"title": "Leave approval — acceptance criteria", "features": [
    {"name": "Request leave", "scenarios": [
        {"id": "SC-1", "title": "Manager approves", "when": "the manager approves",
         "then": ["the request is marked approved"]}]}]})


HIGH_EXPLICIT = {"completeness": 90, "confidence": "explicit", "impact": "high"}
HIGH_INFERRED = {"completeness": 30, "confidence": "inferred", "impact": "high"}


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Isolate every test's sessions under a fresh temp workspace; no credential by default (#332)."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    # Since #334 the credential guard asks the SDK, which also discovers an active profile on disk.
    monkeypatch.setattr("anthropic._client.default_credentials", lambda **kw: None, raising=False)
    return tmp_path


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def raw_client(app):
    """A client that sends nothing a browser wouldn't: loopback host, no request token."""
    return TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)


# The only two forms in this app that carry `hx-post` (#428).
_HTMX_POST_PATHS = ("/answers", "/artifacts/")


@pytest.fixture
def client(raw_client):
    """The everyday client: `raw_client` plus the CSRF token every rendered form carries (as a header, so
    tests can keep posting plain `data=` dicts) and `HX-Request: true` on the two htmx-post forms (#428)."""
    raw_client.headers[CSRF_HEADER] = csrf_token()
    original_post = raw_client.post

    def _post(url, *args, **kwargs):
        if any(p in str(url) for p in _HTMX_POST_PATHS):
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("HX-Request", "true")
            kwargs["headers"] = headers
        return original_post(url, *args, **kwargs)

    raw_client.post = _post
    return raw_client


@pytest.fixture
def with_provider(app):
    """Swap in a DiscoveryService backed by a FakeClient (shared across requests, so replies pop in order over
    a multi-step flow)."""
    def _install(*replies, spend=None):
        fake = FakeClient(*replies, spend=spend)
        disco = DiscoveryService(client=fake)
        app.dependency_overrides[get_discovery] = lambda: disco
        return fake
    yield _install
    app.dependency_overrides.clear()


def _make_session(slug="leave-approval", **model_over):
    return seed_session(slug, "A leave approval request", objective="Leave system", **model_over)


full_model = full_slots
