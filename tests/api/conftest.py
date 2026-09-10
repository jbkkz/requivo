"""Shared fixtures for the Requivo API tests (#425) -- no network, no real provider, no port bound:
`TestClient` drives the ASGI app in-process, exactly like `tests/web/conftest.py`'s `client`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from requivo.api.app import create_api
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
def client(app):
    return TestClient(app, base_url="http://127.0.0.1:8767", raise_server_exceptions=False)


def seed_session(slug: str = "leave-approval", **model_overrides) -> str:
    """Create a session and apply a complete model to it directly through the service, no provider
    involved -- for read-route tests that need a real revision 1 to read back."""
    svc = SessionService()
    svc.create_session("A leave approval request", slug=slug)
    model = {"model": full_slots(**model_overrides), "questions": [],
             "summary": {"objective": "A leave approval system"}}
    svc.update_model(slug, json.dumps(model))
    return slug
