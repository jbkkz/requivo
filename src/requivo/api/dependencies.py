"""FastAPI dependencies for the `[api]` extra -- construct the shared application services per
request (#425).

Mirrors `web/dependencies.py`: the services are stateless over an injected `FileSessionRepository`
(they read/write the workspace on each call), so a fresh instance per request is correct and cheap.
Routes depend on these instead of importing the store, so the filesystem is only ever reached
through a service -- never from a route.

`safe_slug` is a second, small implementation of the web module's function of the same name rather
than an import of it: the two are different surfaces behind different optional extras, and neither
needs fastapi to define it, so importing across them would trade the ten lines this duplicates for a
dependency from one surface's package onto another's -- more coupling than the duplication it would
remove. The behaviour is identical on purpose, and the reasoning is `web/dependencies.py`'s own; see
that module's `safe_slug` docstring for the read-time/creation-time split this mirrors.
"""

from __future__ import annotations

from requivo.core.persistence import _refuse_new_reserved_slug, _slug_shape
from requivo.paths import session_root
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService


def get_sessions() -> SessionService:
    return SessionService()


def get_artifacts() -> ArtifactService:
    return ArtifactService()


def safe_slug(slug: str) -> str:
    """Validate a `{slug}` path parameter in Core (strict kebab-case, no traversal) before any route
    uses it. Raises `InvalidSlugError` -- the API's exception handler turns that into a clean 400.

    Read-time only: every route taking this dependency addresses a session that must already exist,
    so the reserved Windows device-name refusal is conditional on a name already occupying the
    session root (`_refuse_new_reserved_slug`), never unconditional. Slice 1 has no creation route,
    so that asymmetry is not yet load-bearing here the way it is in `web/dependencies.py` -- it is
    kept anyway, so a later slice's `POST /sessions` does not inherit a `safe_slug` that was widened
    for a surface that had no creation route to protect against."""
    slug = _slug_shape(slug)
    _refuse_new_reserved_slug(slug, session_root() / slug)
    return slug
