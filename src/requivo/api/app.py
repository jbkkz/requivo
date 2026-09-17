"""The FastAPI application factory for the Requivo API (#425). `create_api()` wires the routes, an
error handler that turns a `RequivoError` into the shared envelope, self-hosted OpenAPI docs (#504),
the security headers (#503), the host allowlist (#508), the origin checks and JSON content-type floor
on unsafe methods, and the bearer-token bind discipline (`api/auth.py`,
`decision: the-http-api-facade`). Nothing binds a port at import time.
"""

from __future__ import annotations

import logging
from typing import Optional

from requivo import __version__
from requivo.api.auth import UnauthorizedError, check_bearer, require_token_for_bind, resolve_token
from requivo.host_policy import CrossSiteRequestError, check_host, check_request_origin, hostname
from requivo.providers.errors import EngineError
from requivo.security_headers import apply_security_headers

logger = logging.getLogger("requivo.api")

_DESCRIPTION = (
    "A local REST facade over the same application services the CLI and Requivo Web use -- one "
    "service method per route, no second implementation of an apply, a generation or a staleness "
    "rule.\n\n"
    "**EXPERIMENTAL.** This API is not yet frozen: paths, methods, statuses and response shapes may "
    "still change without a major-version bump. It ships this way deliberately -- see "
    "`docs/decisions/0004-the-http-api-facade.md` for the design and the three named preconditions "
    "gating the freeze (the workspace becoming constructor state, session delete, and the "
    "estimate-artifact decision). Once they land, `docs/compatibility.md` gains an API section in "
    "the same shape as every other public payload this project promises, and this notice comes down."
)

# This app's own static mount (#504): nothing of the reader's, so exempt from `Cache-Control: no-store`.
_BUNDLED_ASSET_PREFIX = "/api-static/"

# `DEFAULT_CSP` widened by one directive: `style-src 'unsafe-inline'`, because the vendored Swagger UI
# sets inline `style` attributes by construction and patching a vendored bundle is forbidden (#503,
# #504). `script-src` stays `'self'`; `form-action` tightens to `'none'`.
_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _is_bundled_asset(path: str) -> bool:
    """Does this path name a file shipped inside the package rather than anything of the reader's?"""
    return path.startswith(_BUNDLED_ASSET_PREFIX)


def _apply_security_headers(response, path: str):
    """This app's header policy on one response, through `requivo.security_headers` (#503)."""
    return apply_security_headers(response, path, csp=_CSP, is_bundled_asset=_is_bundled_asset)


# Every unsafe method, `DELETE` included though nothing routes it yet, so the set does not widen silently.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# A fixed, short hint on 503 `session_locked`: the remedy is "resubmit unchanged shortly".
_SESSION_LOCKED_RETRY_AFTER_SECONDS = 1


# Every route under this prefix requires the bearer token when one is configured; the liveness probe,
# the docs and the vendored assets are exempt (a browser navigation cannot carry a bearer header).
# Pinned in both directions by `tests/api/test_api_auth.py`.
_TOKEN_GATED_PREFIX = "/api/v1/"
_TOKEN_EXEMPT_PATHS = frozenset({"/api/v1/health"})


def requires_token(path: str) -> bool:
    """Is this path one the bearer token gates?"""
    return path.startswith(_TOKEN_GATED_PREFIX) and path not in _TOKEN_EXEMPT_PATHS


def _raw_authorization(request) -> Optional[bytes]:
    """The `Authorization` header as the bytes it arrived as, off the ASGI scope: `request.headers`
    decodes as latin-1 (`api/auth.py`)."""
    for name, value in request.scope.get("headers", ()):
        if name == b"authorization":
            return value
    return None


def require_json_content_type(request):
    """Every unsafe method must carry `Content-Type: application/json` or is refused with 415 before
    any route: a cross-origin page cannot send it without a CORS preflight this app fails
    (`decision: the-http-api-facade`). Unconditional, bodyless routes included; matched on the media
    type alone, ignoring a charset."""
    if request.method not in _UNSAFE_METHODS:
        return None
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() == "application/json":
        return None
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"code": "unsupported_content_type",
         "message": "unsafe methods on this API require `Content-Type: application/json`."},
        status_code=415)


def create_api(*, bind_host: Optional[str] = None, token: Optional[str] = None):
    """Build the FastAPI application. `token` defaults to `REQUIVO_API_TOKEN`; with none the app is
    unauthenticated. `bind_host`, when given and not loopback with no token, raises
    `ApiTokenRequiredError` instead of building; `None` runs no bind check. Every `[api]` import is
    deferred here, and a missing extra is an `EngineError` (`provider_unavailable`)."""
    token = resolve_token(token)
    if bind_host is not None:
        require_token_for_bind(bind_host, token)
    try:
        from fastapi import FastAPI, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
        from fastapi.staticfiles import StaticFiles

        from requivo.api.routes import artifacts, discovery, docs, health, sessions
        from requivo.api.static_files import STATIC_DIR
    except ImportError as e:
        raise EngineError(
            "The HTTP API is not installed. Install it with `pip install 'requivo[api]'` "
            "(or `uv tool install 'requivo[api]'`). You do NOT need it for the CLI, Requivo Web, or "
            f"Claude Code. (import error: {e})"
        ) from e

    from requivo.core.errors import RequivoError
    from requivo.http import http_status_for

    app = FastAPI(
        title="Requivo API",
        version=__version__,
        description=_DESCRIPTION,
        # `/docs` and `/redoc` are wired by hand (`api/routes/docs.py`, #504): FastAPI's defaults hardcode a CDN.
        docs_url=None, redoc_url=None, openapi_url="/openapi.json",
    )

    app.mount("/api-static", StaticFiles(directory=str(STATIC_DIR)), name="api-static")

    # Three guards, installed before the header middleware so they run inside it, each rendering its
    # own refusal (an exception in a `BaseHTTPMiddleware` never reaches the handler below).
    # Registration order is the reverse of per-request order: `security_headers` -> `host_guard`
    # (#508) -> `bearer_guard` -> `cross_site_guard` (origin first, then content type).
    # `test_a_rebound_host_is_refused_before_the_content_type_check`,
    # `test_a_missing_token_is_refused_before_the_cross_site_checks`,
    # `test_a_cross_site_request_is_refused_before_the_content_type_check`.
    @app.middleware("http")
    async def cross_site_guard(request: Request, call_next):
        if request.method in _UNSAFE_METHODS:
            try:
                check_request_origin(hostname(request.headers.get("host") or ""),
                                     sec_fetch_site=request.headers.get("sec-fetch-site"),
                                     origin=request.headers.get("origin"),
                                     referer=request.headers.get("referer"))
            except CrossSiteRequestError as exc:
                return JSONResponse(exc.to_dict(), status_code=http_status_for(exc))
        refusal = require_json_content_type(request)
        return refusal if refusal is not None else await call_next(request)

    @app.middleware("http")
    async def bearer_guard(request: Request, call_next):
        if token is not None and requires_token(request.url.path):
            try:
                check_bearer(_raw_authorization(request), token)
            except UnauthorizedError as exc:
                return JSONResponse(exc.to_dict(), status_code=http_status_for(exc),
                                    headers={"WWW-Authenticate": exc.challenge})
        return await call_next(request)

    @app.middleware("http")
    async def host_guard(request: Request, call_next):
        try:
            check_host(request.headers.get("host"))
        except CrossSiteRequestError as exc:
            return JSONResponse(exc.to_dict(), status_code=http_status_for(exc))
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        return _apply_security_headers(await call_next(request), request.url.path)

    @app.exception_handler(RequivoError)
    async def _requivo_error(request: Request, exc: RequivoError):
        status = http_status_for(exc)
        headers = {}
        if exc.code == "session_locked":
            # "The write never started; retrying unchanged is correct": a fixed hint, since the error carries no duration.
            headers["Retry-After"] = str(_SESSION_LOCKED_RETRY_AFTER_SECONDS)
        if status >= 500:
            # A 5xx is this server's own fault, logged for an operator with no browser console.
            logger.error("%s serving %s %s: %s", exc.code, request.method, request.url.path,
                         exc.message)
        return JSONResponse(exc.to_dict(), status_code=status, headers=headers or None)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # Generic on purpose: `exc.errors()` can carry caller-supplied values back verbatim.
        return JSONResponse(
            {"code": "invalid_request", "message": "The request was not valid."}, status_code=400)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.exception("unhandled error serving %s %s", request.method, request.url.path)
        # Stated here too: `Exception` is handled outside the user middleware stack (#340, #462).
        return _apply_security_headers(
            JSONResponse(
                {"code": "internal_error", "message": "Something went wrong on the server."},
                status_code=500),
            request.url.path)

    app.include_router(docs.router)
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")
    app.include_router(discovery.router, prefix="/api/v1")
    app.include_router(artifacts.router, prefix="/api/v1")
    return app
