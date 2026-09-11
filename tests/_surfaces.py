"""The FastAPI surfaces this project ships, as one table two policy guards read.

`tests/test_security_headers.py` (#503) and `tests/test_host_allowlist.py` (#508) both exist to prove
the same property about a different policy: it is stated once and every surface gets it, so a *third*
surface is a one-line addition here rather than a forgotten one. Keeping a copy of the table in each
file would have made the second policy's guard exactly the kind of second copy both issues were filed
about.

Both apps are built directly via their own factories, in-process (`TestClient`), no network and no
port bound -- the same shape `tests/web/conftest.py` and `tests/api/conftest.py` already use,
restated here because this table spans both and must not import a fixture scoped to only one of them.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from requivo.api.app import create_api
from requivo.web.app import create_app


class Surface:
    """One FastAPI app under these policies: its factory, the loopback base URL it answers to (the
    host allowlist refuses anything else), a route known to succeed, and the bundled,
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


SURFACES = [
    Surface("web", create_app, "http://127.0.0.1:8765", "/",
            ["/static/css/app.css", "/static/vendor/htmx.min.js", "/favicon.ico"]),
    Surface("api", create_api, "http://127.0.0.1:8767", "/api/v1/health",
            ["/api-static/favicon.svg", "/api-static/vendor/swagger-ui/swagger-ui.css",
             "/api-static/vendor/redoc/redoc.standalone.js"]),
]
