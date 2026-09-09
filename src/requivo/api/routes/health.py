"""Liveness endpoint -- mirrors `web/routes/health.py`'s `/health`, minus the favicon route, which
is a browser concern this surface has no reason to answer."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from requivo import __version__

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "requivo-api", "version": __version__})
