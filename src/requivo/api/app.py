"""The FastAPI application factory for the Requivo API (#425, slice 1: skeleton, error handler,
`/health`, read routes).

`create_api()` wires the read routes and an error handler that turns a `RequivoError` into the same
serializable envelope every other surface already publishes (`error.to_dict()` plus
`http_status_for` for the status), and turns OpenAPI docs ON -- unlike Requivo Web, which keeps them
off because a browser app's routes are not an invitation to script it. This *is* the invitation: a
machine client is exactly this surface's audience. Docs are self-hosted (#504, `api/routes/docs.py`
+ `api/static/`) rather than loaded from a CDN, and every response -- docs included -- carries the
same security-header policy Requivo Web does (#503, `requivo.security_headers`) and the same host
allowlist (#508, `requivo.host_policy`) -- the DNS-rebinding guard, which is transport-level and so
applies to any local listener, forms or no forms.

Nothing here binds a port at import time: this is a factory, mirroring `web.app.create_app`. Slice 1
ships no CLI verb to call it from yet -- that is slice 4's `requivo api serve` -- so a caller wanting
to run it today constructs the app and hands it to `uvicorn.run(...)` (or any other ASGI server)
itself.
"""

from __future__ import annotations

import logging

from requivo import __version__
from requivo.host_policy import CrossSiteRequestError, check_host
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

# This app's own static mount: the vendored `/docs`/`/redoc` assets and favicon (#504) -- nothing of
# the reader's, unlike everything under `/api/v1/`, so it is exempt from `Cache-Control: no-store`
# below on the same argument `web/app.py` makes for `/static/`.
_BUNDLED_ASSET_PREFIX = "/api-static/"

# Widened by exactly one directive from `requivo.security_headers.DEFAULT_CSP`: `style-src` carries
# `'unsafe-inline'` in addition to `'self'`. This is not the CDN exception #503 was filed to refuse
# -- that is `script-src`, which stays `'self'` with no exception, here or anywhere else in this
# app -- it is a different directive, governing presentation rather than execution, widened for a
# different reason: the vendored `swagger-ui-bundle.js`/`swagger-ui-standalone-preset.js` (#504) are
# a React application that sets computed layout via inline `style` attributes throughout its own
# DOM (expand/collapse arrows, resizable panels, syntax highlighting), unconditionally and by
# construction -- verified directly against the unmodified vendored files, not assumed. Patching a
# vendored third-party bundle to stop doing this is exactly what `THIRD-PARTY-NOTICES.md`'s own rule
# for htmx forbids ("no local edits, ever, or the version stops describing what is shipped"), so the
# alternative is a broken Swagger UI page under a CSP this strict, which trades a real usability
# regression for a directive that governs CSS, not code execution. `form-action` tightens instead,
# to `'none'`: this surface serves no HTML form, unlike Requivo Web's `'self'`.
_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _is_bundled_asset(path: str) -> bool:
    """Does this path name a file shipped inside the package rather than anything of the reader's?"""
    return path.startswith(_BUNDLED_ASSET_PREFIX)


def _apply_security_headers(response, path: str):
    """State this app's header policy on one response -- mirrors `web.app._apply_security_headers`,
    both now thin wrappers around `requivo.security_headers.apply_security_headers`, the one
    definition #503 was filed to get. See that module for why a header added there reaches both
    surfaces without a second edit."""
    return apply_security_headers(response, path, csp=_CSP, is_bundled_asset=_is_bundled_asset)


def create_api():
    """Build the FastAPI application.

    Every import that needs the `[api]` extra is deferred inside this function, never at module
    level, so `import requivo.api.app` -- and therefore `import requivo.api` -- succeeds with the
    base install; only calling this function needs fastapi. On a missing extra it raises
    `EngineError` (code `provider_unavailable` -- this project's existing vocabulary for "an optional
    install is absent", not a comment on this being a reasoning provider; `new_client()` and
    `_cmd_web` already answer the missing `[anthropic]`/`[web]` extras the same way) with an
    actionable message, rather than letting the bare `ImportError` traceback out.
    """
    try:
        from fastapi import FastAPI, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
        from fastapi.staticfiles import StaticFiles

        from requivo.api.routes import artifacts, docs, health, sessions
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
        # `/docs` and `/redoc` are wired by hand below (`api/routes/docs.py`), not by FastAPI's own
        # defaults -- both of which hardcode a `cdn.jsdelivr.net` URL (#504). `docs_url=None,
        # redoc_url=None` is the same switch `web/app.py` uses to turn its docs off entirely; here it
        # only stops FastAPI from wiring its *own* routes at these two paths, leaving them free for
        # this module's replacements. `openapi_url` is untouched -- the spec itself is already
        # same-origin JSON, never third-party.
        docs_url=None, redoc_url=None, openapi_url="/openapi.json",
    )

    app.mount("/api-static", StaticFiles(directory=str(STATIC_DIR)), name="api-static")

    # Installed before the header middleware so it ends up *inside* it: a request the guard turns
    # away still leaves with the same CSP and nosniff headers as any other response -- the same
    # ordering, for the same reason, as `web/app.py`'s own comment on this line.
    #
    # It renders its own refusal rather than raising, because an exception raised in a
    # `BaseHTTPMiddleware` is outside the app's `ExceptionMiddleware` and the `RequivoError` handler
    # registered below would never see it -- the caller would get a bare 500 where a 403 was
    # intended. `web/security.py`'s `install_cross_site_guard` makes the identical point.
    #
    # Only the host axis, and that is the decision rather than an omission (#508). It is the one
    # cross-site check that applies to **reads**, which is all this surface serves today, and the
    # one a formless, cookieless JSON API needs for exactly the same transport-level reason a
    # browser app does: DNS rebinding does not care what content type the listener speaks. The
    # `Sec-Fetch-Site`/`Origin` checks and the JSON content-type requirement that does the
    # synchronizer token's job here belong with the unsafe methods, which slice 4 adds --
    # `docs/decisions/0004-the-http-api-facade.md` §5 argues both, and marks which ships today.
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
        if status >= 500:
            # A 5xx here is this server's own fault, not the caller's -- the same argument
            # `web/app.py`'s identical handler makes, restated for a client with no browser console
            # to read a traceback in even if one leaked.
            logger.error("%s serving %s %s: %s", exc.code, request.method, request.url.path,
                         exc.message)
        return JSONResponse(exc.to_dict(), status_code=status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # No second vocabulary and no echoed detail -- mirrors `web/app.py`'s own handler for the
        # identical FastAPI exception, deliberately generic rather than serializing `exc.errors()`,
        # which can carry caller-supplied values back verbatim.
        return JSONResponse(
            {"code": "invalid_request", "message": "The request was not valid."}, status_code=400)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.exception("unhandled error serving %s %s", request.method, request.url.path)
        # Stated here too, not only in the middleware -- `Exception` is handled by Starlette's
        # `ServerErrorMiddleware`, *outside* the user middleware stack, so `security_headers` above
        # never sees this response. The identical shape as `web/app.py`'s `_unexpected` (#340, #462).
        return _apply_security_headers(
            JSONResponse(
                {"code": "internal_error", "message": "Something went wrong on the server."},
                status_code=500),
            request.url.path)

    app.include_router(docs.router)
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")
    app.include_router(artifacts.router, prefix="/api/v1")
    return app
