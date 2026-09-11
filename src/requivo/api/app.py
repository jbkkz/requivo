"""The FastAPI application factory for the Requivo API (#425 -- slice 1: skeleton, error handler,
`/health`, read routes; slice 2: the write routes and their cross-site floor).

`create_api()` wires every route and an error handler that turns a `RequivoError` into the same
serializable envelope every other surface already publishes (`error.to_dict()` plus
`http_status_for` for the status), and turns OpenAPI docs ON -- unlike Requivo Web, which keeps them
off because a browser app's routes are not an invitation to script it. This *is* the invitation: a
machine client is exactly this surface's audience. Docs are self-hosted (#504, `api/routes/docs.py`
+ `api/static/`) rather than loaded from a CDN, and every response -- docs included -- carries the
same security-header policy Requivo Web does (#503, `requivo.security_headers`), the same host
allowlist (#508, `requivo.host_policy`) -- the DNS-rebinding guard, which is transport-level and so
applies to any local listener, forms or no forms -- and, since slice 2, a JSON content-type floor on
every unsafe method (see `require_json_content_type` below). The `Sec-Fetch-Site`/`Origin` checks
that complete the posture `docs/decisions/0004-the-http-api-facade.md` §5 describes are still slice
4's, alongside the bearer-token bind discipline and the `requivo api serve` verb -- none of the three
exists yet, so this app is unauthenticated and loopback-only in exactly the way slice 1 shipped it.

Nothing here binds a port at import time: this is a factory, mirroring `web.app.create_app`. No CLI
verb calls it yet -- that is slice 4's `requivo api serve` -- so a caller wanting to run it today
constructs the app and hands it to `uvicorn.run(...)` (or any other ASGI server) itself.
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


# Every method a write route in this slice uses. `DELETE` is listed even though nothing routes it
# yet (`docs/decisions/0004-the-http-api-facade.md` §1: `DELETE /sessions/{slug}` is reserved until
# #238) -- an unsafe method the app does not yet route is still refused by FastAPI's own 405 before
# this middleware would matter, but naming it here rather than deriving it from the routes that
# happen to exist keeps this set stable as slices land, instead of silently widening later.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# A fixed, short hint on 503 `session_locked` (`_requivo_error` below) -- not a measured figure,
# because none is available: the error carries no lock-hold duration. Short because the documented
# remedy is "resubmit unchanged shortly", not "come back later" -- a long value would encourage a
# caller to poll something else in the meantime and lose the "the write never started" guarantee's
# whole point, which is that resubmitting *now*, unchanged, is already correct.
_SESSION_LOCKED_RETRY_AFTER_SECONDS = 1


def require_json_content_type(request):
    """The one piece of slice 4's cross-site posture this slice ships early (#425 slice 2, brought
    forward from slice 4 by the issue that funded it): every unsafe method on this API must carry
    `Content-Type: application/json`, or the request is refused with 415 before it reaches a route.

    `docs/decisions/0004-the-http-api-facade.md` §5 explains why this one check substitutes for the
    web app's synchronizer token on a JSON API with no forms and no cookies: a cross-origin page
    cannot send this content type without triggering a CORS preflight, and this app sends no CORS
    headers, so the preflight fails -- the browser attack the web guard exists for (a hostile page
    burning the server's key with fire-and-forget form posts) has no JSON-shaped equivalent. The
    `Sec-Fetch-Site`/`Origin` checks that complete that posture, and the host allowlist that already
    shipped with slice 1 (`host_guard` below), are the other two legs of the same stool -- this is
    not a substitute for either, only the one leg cheap enough, and load-bearing enough on its own,
    not to ship a write surface without it.

    Deliberately unconditional across every unsafe method, including the bodyless ones (`POST
    .../discover`, `POST .../artifacts/{type}`): a caller has to say what it is sending even when it
    is sending nothing, because a route that happens to have no request model today is not a
    contract that it never will.

    Matched on the media type alone (`application/json`, case-insensitively, ignoring a trailing
    `; charset=...`), the same way a real client's `json=` parameter sets it -- refusing on an exact
    string match would refuse every legitimate client that also states an encoding."""
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
        # `/docs` and `/redoc` are wired by hand below (`api/routes/docs.py`), not by FastAPI's own
        # defaults -- both of which hardcode a `cdn.jsdelivr.net` URL (#504). `docs_url=None,
        # redoc_url=None` is the same switch `web/app.py` uses to turn its docs off entirely; here it
        # only stops FastAPI from wiring its *own* routes at these two paths, leaving them free for
        # this module's replacements. `openapi_url` is untouched -- the spec itself is already
        # same-origin JSON, never third-party.
        docs_url=None, redoc_url=None, openapi_url="/openapi.json",
    )

    app.mount("/api-static", StaticFiles(directory=str(STATIC_DIR)), name="api-static")

    # Installed before the header middleware so it ends up *inside* it: a request either guard turns
    # away still leaves with the same CSP and nosniff headers as any other response -- the same
    # ordering, for the same reason, as `web/app.py`'s own comment on this line.
    #
    # Both render their own refusal rather than raising, because an exception raised in a
    # `BaseHTTPMiddleware` is outside the app's `ExceptionMiddleware` and the `RequivoError` handler
    # registered below would never see it -- the caller would get a bare 500 where a 403/415 was
    # intended. `web/security.py`'s `install_cross_site_guard` makes the identical point.
    #
    # Only the host axis, and that is the decision rather than an omission (#508). It is the one
    # cross-site check that applies to **reads**, which is all slice 1 served, and the one a
    # formless, cookieless JSON API needs for exactly the same transport-level reason a browser app
    # does: DNS rebinding does not care what content type the listener speaks.
    @app.middleware("http")
    async def host_guard(request: Request, call_next):
        try:
            check_host(request.headers.get("host"))
        except CrossSiteRequestError as exc:
            return JSONResponse(exc.to_dict(), status_code=http_status_for(exc))
        return await call_next(request)

    # Slice 2's floor under the write routes -- see `require_json_content_type`'s own docstring for
    # what this does and does not cover. The `Sec-Fetch-Site`/`Origin` checks that complete the
    # posture `docs/decisions/0004-the-http-api-facade.md` §5 describes are still slice 4's.
    @app.middleware("http")
    async def content_type_guard(request: Request, call_next):
        refusal = require_json_content_type(request)
        return refusal if refusal is not None else await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        return _apply_security_headers(await call_next(request), request.url.path)

    @app.exception_handler(RequivoError)
    async def _requivo_error(request: Request, exc: RequivoError):
        status = http_status_for(exc)
        headers = {}
        if exc.code == "session_locked":
            # "The write never started; retrying it unchanged is correct" (the code's own contract,
            # `docs/decisions/0004-the-http-api-facade.md` §2) -- so this is the one code worth
            # naming a wait for. No dynamic figure is available (`SessionLockedError` carries none:
            # it fires either after `_LOCK_TIMEOUT_SECONDS` already elapsed, or immediately from the
            # non-blocking first-discovery guard, and neither tells this handler how long the holder
            # has left), so the value is a fixed, short, reasoned hint rather than a computed one.
            headers["Retry-After"] = str(_SESSION_LOCKED_RETRY_AFTER_SECONDS)
        if status >= 500:
            # A 5xx here is this server's own fault, not the caller's -- the same argument
            # `web/app.py`'s identical handler makes, restated for a client with no browser console
            # to read a traceback in even if one leaked.
            logger.error("%s serving %s %s: %s", exc.code, request.method, request.url.path,
                         exc.message)
        return JSONResponse(exc.to_dict(), status_code=status, headers=headers or None)

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
    app.include_router(discovery.router, prefix="/api/v1")
    app.include_router(artifacts.router, prefix="/api/v1")
    return app
