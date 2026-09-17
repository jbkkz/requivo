"""The security-header policy is one definition consumed by every FastAPI surface (#503)."""

from __future__ import annotations

import pytest

from tests._surfaces import SURFACES as _SURFACES


@pytest.mark.parametrize("surface", _SURFACES, ids=repr)
def test_security_headers_present(surface):
    """Every response -- success and 404 alike -- carries the full header set (#503)."""
    client = surface.client()
    for path, expected_status in ((surface.ok_path, 200), (surface.ok_path + "-does-not-exist", 404)):
        r = client.get(path)
        assert r.status_code == expected_status, f"{surface}: {path} answered {r.status_code}"
        h = r.headers
        assert h["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in h
        assert "default-src 'self'" in h["Content-Security-Policy"]
        assert "script-src 'self'" in h["Content-Security-Policy"]
        assert "Referrer-Policy" in h
        assert h["Cache-Control"] == "no-store"


@pytest.mark.parametrize("surface", _SURFACES, ids=repr)
def test_the_500_page_carries_every_header_an_ordinary_page_carries(surface):
    """A bare, unhandled exception is answered by Starlette's `ServerErrorMiddleware`, *outside* the app's own
    `@app.middleware("http")` stack."""
    app = surface.factory()

    @app.get("/_boom")
    def _boom():
        raise ValueError("a bug, not a refusal")

    client = surface.client(app)
    ordinary = client.get(surface.ok_path)
    unhandled = client.get("/_boom")
    assert ordinary.status_code == 200
    assert unhandled.status_code == 500
    missing = set(ordinary.headers) - set(unhandled.headers)
    assert not missing, f"{surface}: the 500 page is missing {sorted(missing)}"


@pytest.mark.parametrize("surface", _SURFACES, ids=repr)
def test_a_bundled_asset_stays_cacheable(surface):
    """The other half of the `Cache-Control` boundary."""
    client = surface.client()
    for path in surface.bundled_asset_paths:
        response = client.get(path)
        assert response.status_code == 200, f"{path} was not served, so this row asserts nothing"
        assert "Content-Security-Policy" in response.headers, (
            f"the header middleware never ran on {path}")
        assert "no-store" not in response.headers.get("Cache-Control", ""), (
            f"{path} is a bundled asset and must stay cacheable")
