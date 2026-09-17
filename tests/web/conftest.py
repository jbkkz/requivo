"""Shared fixtures for the Requivo Web tests — no network, no real provider."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from _fakes import FakeClient, Spend, full_slots, seed_session  # noqa: F401  (re-exported for the web suite)
from fastapi.testclient import TestClient

from requivo.core.persistence import canonical_dir
from requivo.services.discovery import DiscoveryService
from requivo.web.app import create_app
from requivo.web.dependencies import get_discovery
from requivo.web.security import CSRF_HEADER, csrf_token
from requivo.web.templating import STATIC_DIR


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

# Enough tokens that the rendered figure is unmistakable in a page of other numbers: 9000 + 400 + 3000.
PAID = Spend(input_tokens=9000, output_tokens=3000, cache_read_input_tokens=400)
PAID_TOKENS = "12,400"


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
    """`raw_client` plus the CSRF token as a header and `HX-Request: true` on the two htmx-post forms (#428)."""
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
    """Swap in a DiscoveryService backed by a FakeClient shared across requests, so replies pop in order."""
    def _install(*replies, spend=None):
        fake = FakeClient(*replies, spend=spend)
        disco = DiscoveryService(client=fake)
        app.dependency_overrides[get_discovery] = lambda: disco
        return fake
    yield _install
    app.dependency_overrides.clear()


def _make_session(slug="leave-approval", **model_over):
    """Seed a discovered session directly through the service (no provider), for view/security tests."""
    return seed_session(slug, "A leave approval request", objective="Leave system", **model_over)


def seed_row(slug: str, *, analysed: bool = True, updated_at: str | None = None) -> str:
    """A listing row, offline, optionally pinned to a chosen `updated_at` instant."""
    seed_session(slug, analysed=analysed, objective=f"Objective for {slug}")
    if updated_at is not None:
        p = canonical_dir(slug) / "session.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["updated_at"] = updated_at
        p.write_text(json.dumps(data), encoding="utf-8")
    return slug


def create_via_post(client, slug="leave-approval", provider="anthropic", request_text="x", follow=False):
    """`POST /sessions` through the everyday client; the browser's own door onto a session."""
    return client.post("/sessions", data={"request_text": request_text, "slug": slug, "provider": provider},
                       follow_redirects=follow)


def analysed_via_post(client, with_provider, *replies, spend=PAID):
    """A session at revision 1, created through the web (redirect followed) by a provider that reports `spend`."""
    fake = with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED), *replies, spend=spend)
    create_via_post(client, request_text="A leave approval system", follow=True)
    return fake


def run_js_harness(name: str, what: str, issue: str):
    """Execute the real `static/js/app.js` under `tests/web/<name>.js` on node, or skip naming what went untested."""
    node = shutil.which("node")
    if node is None:
        pytest.skip(f"node is not on PATH, so {what} in static/js/app.js was NOT asserted in this run — "
                    f"it is browser behaviour and nothing else in this suite can see it ({issue})")
    harness = Path(__file__).parent / f"{name}.js"
    proc = subprocess.run([node, str(harness), str(STATIC_DIR / "js" / "app.js")], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, so nothing was observed:\n" + proc.stderr
    return json.loads(proc.stdout)
