"""Shared fixtures for the Requivo API tests (#425) -- no network, no real provider, no port bound:
`TestClient` drives the ASGI app in-process, exactly like `tests/web/conftest.py`'s `client`."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from requivo.api.app import create_api
from requivo.api.dependencies import get_discovery
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from tests._fakes import full_slots


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Isolate every test's sessions under a fresh temp workspace."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    return tmp_path


@pytest.fixture
def app():
    return create_api()


@pytest.fixture
def raw_client(app):
    """A client that sends nothing beyond what `httpx` sends unasked."""
    return TestClient(app, base_url="http://127.0.0.1:8767", raise_server_exceptions=False)


@pytest.fixture
def client(raw_client):
    """The everyday client: same as `raw_client`, plus `Content-Type: application/json` on every write."""
    original_request = raw_client.request

    def _request(method, url, *args, **kwargs):
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("Content-Type", "application/json")
            kwargs["headers"] = headers
        return original_request(method, url, *args, **kwargs)

    raw_client.request = _request
    return raw_client


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text, usage=None):
        self.content = [_FakeBlock(text)]
        self.stop_reason = "end_turn"
        self.usage = usage


class Spend:
    """The token counts the SDK reports on a response, under the SDK's own attribute names."""

    def __init__(self, input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                 cache_creation_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class FakeClient:
    """Returns canned JSON replies in order; records each `create()` call's kwargs."""

    def __init__(self, *replies, spend=None):
        self._replies = list(replies)
        self._spend = spend
        self.calls = []
        self.messages = self  # client.messages.create -> self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._replies.pop(0), self._spend)


@pytest.fixture
def with_provider(app):
    """Swap in a `DiscoveryService` backed by a `FakeClient` (shared across requests, so replies pop in order
    over a multi-step flow)."""
    def _install(*replies, spend=None):
        fake = FakeClient(*replies, spend=spend)
        disco = DiscoveryService(client=fake)
        app.dependency_overrides[get_discovery] = lambda: disco
        return fake
    yield _install
    app.dependency_overrides.clear()


def seed_session(slug: str = "leave-approval", **model_overrides) -> str:
    """Create a session and apply a complete model to it directly through the service."""
    svc = SessionService()
    svc.create_session("A leave approval request", slug=slug)
    model = {"model": full_slots(**model_overrides), "questions": [],
             "summary": {"objective": "A leave approval system"}}
    svc.update_model(slug, json.dumps(model))
    return slug
