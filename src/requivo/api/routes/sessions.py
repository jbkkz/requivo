"""Session routes (#425): thin one- or two-call views over `SessionService`, composing and re-validating nothing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from requivo.api.dependencies import get_sessions, safe_slug
from requivo.api.schemas import ApplyRevisionRequest, ContextCardsRequest, CreateSessionRequest, PreviewRevisionRequest
from requivo.services.sessions import SessionService

router = APIRouter()

# The five-key row `session list --json` publishes (present-and-null), restated rather than imported
# from another surface's private helper (#425).
_NO_DETAIL = "no further detail"


def _session_list_row(entry) -> dict:
    if not entry.readable:
        return {"slug": entry.slug, "revision": None, "provider": None, "updated_at": None,
                "readable": False, "error": entry.error or _NO_DETAIL}
    m = entry.meta
    assert m is not None  # `readable` is exactly `meta is not None` -- narrows what pyright cannot
    return {"slug": m.slug, "revision": m.current_revision, "provider": m.provider,
            "updated_at": m.updated_at, "readable": True, "error": None}


@router.get("/sessions")
def list_sessions(sessions: SessionService = Depends(get_sessions)) -> dict:
    """Every session, degrading per member (invariant 15), in `session list --json`'s row shape."""
    entries = sessions.list_entries()
    return {"sessions": [_session_list_row(e) for e in entries],
            "degraded": sum(1 for e in entries if not e.readable)}


@router.get("/sessions/{slug}")
def get_session(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)) -> dict:
    """One session's metadata -- `session show --json`'s own payload (`SessionService.meta`)."""
    return sessions.meta(slug).model_dump()


@router.get("/sessions/{slug}/model")
def get_model(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)) -> dict:
    """The durable product -- the current validated model (`SessionService.load_model`)."""
    return sessions.load_model(slug).model_dump()


@router.get("/sessions/{slug}/revisions")
def list_revisions(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)) -> dict:
    """The provenance log: `SessionMeta.revisions`, usage included since #292."""
    return {"revisions": sessions.meta(slug).model_dump()["revisions"]}


@router.get("/sessions/{slug}/revisions/{revision}")
def get_revision(revision: int, slug: str = Depends(safe_slug),
                  sessions: SessionService = Depends(get_sessions)) -> dict:
    """A historical model revision -- the basis for "what moved since?" (`SessionService.load_revision`)."""
    return sessions.load_revision(slug, revision).model_dump()


@router.get("/sessions/{slug}/status")
def get_status(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)) -> dict:
    """The understanding checklist, open questions and readiness: `requivo status --json`'s payload."""
    return sessions.status(slug)


@router.get("/sessions/{slug}/impact")
def get_impact(slots: str = Query(...), slug: str = Depends(safe_slug),
               sessions: SessionService = Depends(get_sessions)) -> dict:
    """What rests on the named slots (`SessionService.impact`). `slots` is comma-separated ids or
    label words and is required, so an omitted one is a 400; a blank one (`?slots=`) reaches
    `impact(slug, [])`, "what does changing nothing reach?", rather than one empty token (#425)."""
    tokens = [] if not slots.strip() else slots.split(",")
    return sessions.impact(slug, tokens).to_dict()


@router.post("/sessions")
def create_session(body: CreateSessionRequest,
                   sessions: SessionService = Depends(get_sessions)) -> JSONResponse:
    """Create a session from a request, no provider call: 201 fresh, 200 for an idempotent repeat
    (invariant 11), 409 `session_exists` for an explicit slug taken by a different identity
    (`strict_slug=True`, this route's own opt-in)."""
    meta, created = sessions.create_session_report(
        body.request, context_cards=body.context_cards, slug=body.slug, strict_slug=True)
    return JSONResponse(meta.model_dump(), status_code=201 if created else 200)


@router.put("/sessions/{slug}/context-cards")
def rescope_session(body: ContextCardsRequest, slug: str = Depends(safe_slug),
                    sessions: SessionService = Depends(get_sessions)) -> dict:
    """Re-scope a session's context-card selection (`SessionService.rescope`, #168)."""
    return sessions.rescope(slug, body.context_cards).to_dict()


@router.post("/sessions/{slug}/revisions")
def apply_revision(body: ApplyRevisionRequest, slug: str = Depends(safe_slug),
                   sessions: SessionService = Depends(get_sessions)) -> dict:
    """The apply (`SessionService.update_model`): the wire path for an external reasoner, the Claude
    Code shape over HTTP. 409 `revision_conflict` when `expected_revision` is stale."""
    result = sessions.update_model(slug, body.proposal, expected_revision=body.expected_revision,
                                   provenance={"surface": "api-apply"})
    return result.to_dict()


@router.post("/sessions/{slug}/revisions/preview")
def preview_revision(body: PreviewRevisionRequest, slug: str = Depends(safe_slug),
                     sessions: SessionService = Depends(get_sessions)) -> dict:
    """The dry run of the apply (`SessionService.diff`): `status: "planned"`, nothing written, no precondition."""
    return sessions.diff(slug, body.proposal).to_dict()
