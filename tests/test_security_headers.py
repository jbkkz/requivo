"""The security-header policy is one definition consumed by every FastAPI surface (#503).

Found by the v3.2.0 release audit: `web/app.py` installed `security_headers` as HTTP middleware,
`src/requivo/api/app.py` installed none at all, and neither pull request's own diff contained the
defect -- it existed only in their composition (see `requivo.security_headers` for the fuller
account). The fix is `requivo.security_headers.apply_security_headers`, one function both
`web/app.py` and `api/app.py` now call; this file is the guard, parameterised over both apps so a
*third* surface is a one-line addition to `_SURFACES` below rather than a forgotten one -- the
actual acceptance criterion, not merely "the API has headers now".

Both apps built directly via their own factories, in-process (`TestClient`), no network, no port
bound -- the same shape `tests/web/conftest.py` and `tests/api/conftest.py` already use, restated
here because this file spans both and must not import a fixture scoped to only one of them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from requivo.api.app import create_api
from requivo.web.app import create_app


class _Surface:
    """One FastAPI app under this policy: its factory, the loopback base URL it answers to (the web
    app's own cross-site guard refuses anything else), a route known to succeed, and the bundled,
    fingerprint-free asset paths its own static mount serves -- exempt from `Cache-Control:
    no-store` per `requivo.security_headers.apply_security_headers`."""

    def __init__(self, name, factory, base_url, ok_path, bundled_asset_paths):
        self.name = name
        self.factory = factory
        self.base_url = base_url
        self.ok_path = ok_path
        self.bundled_asset_paths = bundled_asset_paths

    def __repr__(self):
        return self.name

    def client(self, app=None):
        return TestClient(app or self.factory(), base_url=self.base_url,
                          raise_server_exceptions=False)


_SURFACES = [
    _Surface("web", create_app, "http://127.0.0.1:8765", "/",
             ["/static/css/app.css", "/static/vendor/htmx.min.js", "/favicon.ico"]),
    _Surface("api", create_api, "http://127.0.0.1:8767", "/api/v1/health",
             ["/api-static/favicon.svg", "/api-static/vendor/swagger-ui/swagger-ui.css",
              "/api-static/vendor/redoc/redoc.standalone.js"]),
]


@pytest.mark.parametrize("surface", _SURFACES, ids=repr)
def test_security_headers_present(surface):
    """Every response -- success and 404 alike -- carries the full header set: `nosniff`, a CSP
    naming `default-src 'self'` and `script-src 'self'` (the directive #503 exists to land, and the
    one #504 requires to hold with no exception carved for it), a stated `Referrer-Policy`, and
    `Cache-Control: no-store` on this app's own dynamic content."""
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
    """A bare, unhandled exception is answered by Starlette's `ServerErrorMiddleware`, *outside* the
    app's own `@app.middleware("http")` stack -- so the header policy has to be restated at that one
    handler, and this is the test that would notice if a future header stopped being restated there
    (#340, #462, carried into `api/app.py` by this same issue)."""
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
    """The other half of the `Cache-Control` boundary: an asset this package ships, rather than
    anything of the reader's, must not be forced to `no-store` -- and must still carry the rest of
    the policy, or the middleware simply never ran on that path and this row proves nothing."""
    client = surface.client()
    for path in surface.bundled_asset_paths:
        response = client.get(path)
        assert response.status_code == 200, f"{path} was not served, so this row asserts nothing"
        assert "Content-Security-Policy" in response.headers, (
            f"the header middleware never ran on {path}")
        assert "no-store" not in response.headers.get("Cache-Control", ""), (
            f"{path} is a bundled asset and must stay cacheable")
