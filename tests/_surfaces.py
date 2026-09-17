"""The FastAPI surfaces this project ships, as one table two policy guards read (#503)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from requivo.api.app import create_api
from requivo.web.app import create_app


class Surface:
    """One FastAPI app under these policies."""

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
