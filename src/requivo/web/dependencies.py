"""FastAPI dependencies: the shared services per request, stateless over a `FileSessionRepository`,
so a route never reaches the filesystem directly.
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
    """Validate a `{slug}` path parameter in Core before any route uses it. The read-time half of
    #372's split (#396): every route here addresses an existing session, so the reserved-name
    refusal is conditional on nothing occupying the name; creation (`POST /sessions`, a form field)
    stays unconditional. `test_a_reserved_slug_already_on_disk_is_reachable_through_the_web_read_routes`."""
    slug = _slug_shape(slug)
    # Probed against the session root, where "does something claim this name?" is decided.
    _refuse_new_reserved_slug(slug, session_root() / slug)
    return slug
