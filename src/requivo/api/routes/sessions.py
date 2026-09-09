"""Session read routes -- list, one session, its model, its revisions, its status, its impact
(#425, slice 1). Every route is a thin, one-call view over `SessionService`; none composes core
calls or re-validates.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from requivo.api.dependencies import get_sessions, safe_slug
from requivo.services.sessions import SessionService

router = APIRouter()

# The five-key shape `deterministic/sessions.py`'s private `_session_list_row` already publishes as
# `session list --json`'s own row (present-and-null rather than absent-when-unknown, so a consumer
# never has to branch on a differently-shaped row). Restated here rather than imported: that
# function is module-private to a file this lane does not own, and reaching into another surface's
# private helper is a worse coupling than the eleven lines below -- see #425's own report for the
# call. "no further detail" mirrors `deterministic/_shared.py`'s `_NO_DETAIL` literally, for the
# same reason.
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
    """Every session, degrading per member rather than failing for the set (invariant 15) --
    `SessionService.list_entries`, in `session list --json`'s own row shape."""
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
    """The provenance log -- `SessionService.meta`'s own `revisions` field, which already carries
    provider, model, surface, prompt hash and (since #292) usage per applied revision."""
    return {"revisions": sessions.meta(slug).model_dump()["revisions"]}


@router.get("/sessions/{slug}/revisions/{revision}")
def get_revision(revision: int, slug: str = Depends(safe_slug),
                  sessions: SessionService = Depends(get_sessions)) -> dict:
    """A historical model revision -- the basis for "what moved since?" (`SessionService.load_revision`)."""
    return sessions.load_revision(slug, revision).model_dump()


@router.get("/sessions/{slug}/status")
def get_status(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)) -> dict:
    """The understanding checklist, open questions and readiness -- verbatim, the same payload
    `requivo status --json` publishes (`SessionService.status`)."""
    return sessions.status(slug)


@router.get("/sessions/{slug}/impact")
def get_impact(slots: str = Query(...), slug: str = Depends(safe_slug),
               sessions: SessionService = Depends(get_sessions)) -> dict:
    """What rests on the named slots -- decisions to re-validate, challenges to re-examine, artifacts
    that go stale (`SessionService.impact`, the XS addition this route exists for). `slots` is
    comma-separated slot ids or label words, e.g. `?slots=permissions,workflow`.

    `slots` is a **required** query parameter, so an entirely omitted one is a 400 before this body
    runs -- an API is a concurrent surface by definition, and silently defaulting a caller's own
    selector is the same shape #255 already argues against for input caps. A **present but blank**
    value (`?slots=`) is not the same thing: it reaches `SessionService.impact(slug, [])`, which
    `resolve_slots([])` reads as "no slots named" rather than as a malformed token -- the one route
    onto that behaviour (found in review, #425: an earlier draft split `slots.split(",")`
    unconditionally, so `?slots=` produced `['']`, one empty token, and `normalize_tokens` refused it
    as `empty_selector_token` -- correct for a genuinely malformed list like `permissions,,workflow`,
    wrong for the caller asking "what does changing nothing reach?", which is exactly the question an
    empty selection answers).

    Deliberately narrower than `requivo impact` with no slots at all, which renders a full per-slot
    dependency map -- a different response shape this route does not attempt to produce in slice 1."""
    tokens = [] if not slots.strip() else slots.split(",")
    return sessions.impact(slug, tokens).to_dict()
