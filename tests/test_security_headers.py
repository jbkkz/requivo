"""The security-header policy is one definition consumed by every FastAPI surface (#503).

Found by the v3.2.0 release audit: `web/app.py` installed `security_headers` as HTTP middleware,
`src/requivo/api/app.py` installed none at all, and neither pull request's own diff contained the
defect -- it existed only in their composition (see `requivo.security_headers` for the fuller
account). The fix is `requivo.security_headers.apply_security_headers`, one function both
`web/app.py` and `api/app.py` now call; this file is the guard, parameterised over both apps so a
*third* surface is a one-line addition to `tests/_surfaces.py` rather than a forgotten one -- the
actual acceptance criterion, not merely "the API has headers now".

That table moved out of this file when #508 gave the host allowlist the same treatment and needed
the same list: two guards over two policies, one statement of what the surfaces are.
"""

from __future__ import annotations

import pytest

from tests._surfaces import SURFACES as _SURFACES


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
