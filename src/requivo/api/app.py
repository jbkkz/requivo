"""The FastAPI application factory for the Requivo API (#425, slice 1: skeleton, error handler,
`/health`, read routes).

`create_api()` wires the read routes and an error handler that turns a `RequivoError` into the same
serializable envelope every other surface already publishes (`error.to_dict()` plus
`http_status_for` for the status), and turns OpenAPI docs ON -- unlike Requivo Web, which keeps them
off because a browser app's routes are not an invitation to script it. This *is* the invitation: a
machine client is exactly this surface's audience.

Nothing here binds a port at import time: this is a factory, mirroring `web.app.create_app`. Slice 1
ships no CLI verb to call it from yet -- that is slice 4's `requivo api serve` -- so a caller wanting
to run it today constructs the app and hands it to `uvicorn.run(...)` (or any other ASGI server)
itself.
"""

from __future__ import annotations

import logging

from requivo import __version__
from requivo.providers.errors import EngineError

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

        from requivo.api.routes import artifacts, health, sessions
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
        docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json",
    )

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
        return JSONResponse(
            {"code": "internal_error", "message": "Something went wrong on the server."},
            status_code=500)

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")
    app.include_router(artifacts.router, prefix="/api/v1")
    return app
