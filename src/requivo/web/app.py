"""The FastAPI application factory for Requivo Web: `create_app()` wires the routers, mounts only
the local static files, sets the security headers and turns a `RequivoError` into a clean page.
Importing this module binds no port.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from requivo.core.errors import RequivoError
from requivo.http import http_status_for as _status_for
from requivo.security_headers import CACHE_CONTROL, DEFAULT_CSP, REFERRER_POLICY, apply_security_headers
from requivo.web.routes import artifacts, discovery, health, home, sessions
from requivo.web.security import install_cross_site_guard
from requivo.web.templating import STATIC_DIR, templates

# The error-code → HTTP-status table lives in `requivo.http` (#422), framework-free, imported as
# `_status_for`; `test_every_error_code_has_an_explicit_http_status` keeps it honest.

# `requivo.security_headers.DEFAULT_CSP` (#503): same-origin everything, vendored HTMX, no CDN.
_CSP = DEFAULT_CSP

# `requivo.security_headers.REFERRER_POLICY` (#503): `same-origin`, never `no-referrer`, which nulls
# the `Origin` header on a plain form post and made the cross-site guard refuse the app's own forms
# (#47, #43). `test_the_policy_this_app_sends_and_the_origin_guard_it_runs_agree`.
_REFERRER_POLICY = REFERRER_POLICY

# Nothing this app answers with may be written to the browser's disk cache except the assets it ships
# (#218): keyed on the path and failing closed, since `export` and the artifact download are not HTML.
# `test_no_response_carrying_the_readers_material_may_be_written_to_the_disk_cache`, `test_a_bundled_asset_stays_cacheable`.
_BUNDLED_ASSET_PREFIXES = ("/static/",)
_BUNDLED_ASSET_PATHS = ("/favicon.ico",)
# `requivo.security_headers.CACHE_CONTROL` (#503).
_CACHE_CONTROL = CACHE_CONTROL


def _is_bundled_asset(path: str) -> bool:
    """Does this path name a file shipped inside the package rather than anything of the reader's?"""
    return path in _BUNDLED_ASSET_PATHS or path.startswith(_BUNDLED_ASSET_PREFIXES)


def _apply_security_headers(response, path: str):
    """This app's header policy on one response: one definition, read by the middleware and by the
    unhandled-exception handler, which runs outside the user middleware stack (#340, #462).
    `test_the_500_page_carries_every_header_an_ordinary_page_carries`."""
    return apply_security_headers(response, path, csp=_CSP, is_bundled_asset=_is_bundled_asset)

# Under `requivo web` this rides uvicorn's handler, so a traceback lands in the operator's terminal.
logger = logging.getLogger("requivo.web")


def _render_error(request: Request, status: int, code: str, message: str):
    """Render an error as an HTMX fragment or a full page; never a traceback."""
    ctx = {"status": status, "code": code, "message": message}
    if request.headers.get("HX-Request") == "true":
        response = templates.TemplateResponse(request, "errors/_error.html", ctx, status_code=status)
        # Retargeted to `#flash`, outside every swap target, so an error notice never replaces the
        # textarea the reader typed into (#203, #30).
        response.headers["HX-Retarget"] = "#flash"
        response.headers["HX-Reswap"] = "innerHTML"
        return response
    template = "errors/404.html" if status == 404 else (
        "errors/500.html" if status >= 500 else "errors/error.html")
    return templates.TemplateResponse(request, template, ctx, status_code=status)


def create_app() -> FastAPI:
    app = FastAPI(title="Requivo Web", docs_url=None, redoc_url=None, openapi_url=None)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Installed before the header middleware so it ends up inside it.
    install_cross_site_guard(app, _render_error)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        return _apply_security_headers(await call_next(request), request.url.path)

    @app.exception_handler(RequivoError)
    async def _requivo_error(request: Request, exc: RequivoError):
        status = _status_for(exc)
        if status >= 500:
            # A 5xx is our fault, and the operator needs a record of it.
            logger.error("%s serving %s %s: %s", exc.code, request.method, request.url.path,
                         exc.message)
        return _render_error(request, status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return _render_error(request, 400, "invalid_request", "The form submission was not valid.")

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):
        return _render_error(request, 404, "not_found", "That page does not exist.")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Logged with the method and path, never the body, which is the user's own request.
        logger.exception("unhandled error serving %s %s", request.method, request.url.path)
        # Stated here too: this handler runs outside the user middleware stack (#340, #462).
        return _apply_security_headers(
            _render_error(request, 500, "internal_error", "Something went wrong on the server."),
            request.url.path)

    app.include_router(health.router)
    app.include_router(home.router)
    app.include_router(sessions.router)
    app.include_router(discovery.router)
    app.include_router(artifacts.router)
    return app
