"""FastAPI dependencies for the `[api]` extra (#425), mirroring `web/dependencies.py`: the shared
services per request, and a `safe_slug` duplicated rather than imported across two optional extras.
"""

from __future__ import annotations

from requivo.core.persistence import _refuse_new_reserved_slug, _slug_shape
from requivo.paths import session_root
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService


def get_sessions() -> SessionService:
    return SessionService()


def get_artifacts() -> ArtifactService:
    return ArtifactService()


def get_discovery() -> DiscoveryService:
    return DiscoveryService()


def safe_slug(slug: str) -> str:
    """Validate a `{slug}` path parameter in Core before any route uses it; read-time only, so the
    reserved-name refusal is conditional on nothing occupying the name (`POST /sessions` takes its
    slug from the body and never reaches this), as `web/dependencies.py`'s twin argues."""
    slug = _slug_shape(slug)
    _refuse_new_reserved_slug(slug, session_root() / slug)
    return slug
