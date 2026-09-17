"""Shared fixtures for the Requivo Web tests — no network, no real provider."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.app import create_app
from requivo.web.dependencies import get_discovery
from requivo.web.security import CSRF_HEADER, csrf_token


def full_model(**overrides) -> dict:
    """A complete required-slot model (empty/low by default), with per-slot overrides."""
    _, required = schema_slot_ids()
    model = {sid: {"completeness": 0, "confidence": "empty", "impact": "low"}
             for sid in _schema_order() if sid in required}
    model.update(overrides)
    return model


def engine_reply(*, converged: bool = False, questions: list[dict] | None = None,
                 **slot_overrides) -> str:
    if questions is None:
        questions = [] if converged else [
            {"q": "How are exceptions handled?", "slot": "business_rules", "why": "uncertainty × impact"}]
    return json.dumps({
        "model": full_model(**slot_overrides),
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


def _make_session(slug="leave-approval", **model_over):
    """Seed a discovered session directly through the service (no provider), for view/security tests."""
    svc = SessionService()
    svc.create_session("A leave approval request", slug=slug)
    model = {"model": full_model(**model_over), "questions": [], "summary": {"objective": "Leave system"}}
    svc.update_model(slug, json.dumps(model))
    return slug


class Spend:
    """The token counts the SDK reports on a response, under the names it uses."""

    def __init__(self, input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                 cache_creation_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class FakeClient:
    """Returns canned JSON replies in order."""

    def __init__(self, *replies, spend=None):
        self._replies = list(replies)
        self._spend = spend
        self.calls = []
        self.messages = self  # client.messages.create → self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._replies.pop(0), self._spend)


class _FakeResponse:
    def __init__(self, text, usage=None):
        self.content = [_Block(text)]
        self.stop_reason = "end_turn"
        self.usage = usage


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


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
