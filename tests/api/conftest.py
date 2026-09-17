"""Shared fixtures for the Requivo API tests (#425) -- no network, no real provider, no port bound:
`TestClient` drives the ASGI app in-process, exactly like `tests/web/conftest.py`'s `client`."""

from __future__ import annotations

import pytest
from _fakes import FakeClient, Spend  # noqa: F401  (re-exported for the api suite)
from _fakes import seed_session as _seed_session
from fastapi.testclient import TestClient

from requivo.api.app import create_api
from requivo.api.dependencies import get_discovery
from requivo.services.discovery import DiscoveryService


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
    return _seed_session(slug, "A leave approval request", **model_overrides)
