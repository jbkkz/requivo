"""Liveness endpoint — a cheap 200 for the CI smoke test and any local health check — and the one
other route small enough to sit beside it: the browser's own implicit favicon probe."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from requivo import __version__
from requivo.web.templating import STATIC_DIR

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "requivo-web", "version": __version__})


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """A browser requests `/favicon.ico` itself regardless of what `<link rel="icon">` names, which
    used to 404 into the operator's logs on every page load (#241). Served as the same SVG brand mark
    `base.html` draws inline, under `.ico` with an honest `image/svg+xml` media type rather than a
    synthesized binary ICO. Pinned by `test_favicon_is_served_and_linked`.
    """
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
