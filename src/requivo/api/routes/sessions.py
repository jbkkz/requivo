"""Session routes -- create, list, one session, its model, its revisions, its status, its impact,
the apply and its dry run, and the context-card rescope (#425, slices 1 and 2). Every route is a
thin, one- or two-call view over `SessionService`; none composes core calls or re-validates.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from requivo.api.dependencies import get_sessions, safe_slug
from requivo.api.schemas import ApplyRevisionRequest, ContextCardsRequest, CreateSessionRequest, PreviewRevisionRequest
from requivo.services.sessions import SessionService

router = APIRouter()

# The five-key shape `deterministic/sessions/lifecycle.py`'s private `_session_list_row` already publishes as
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


@router.post("/sessions")
def create_session(body: CreateSessionRequest,
                   sessions: SessionService = Depends(get_sessions)) -> JSONResponse:
    """Create a session from a request -- no provider call
    (`SessionService.create_session_report`, slice 2). Idempotent by identity (request + context
    cards, invariant 11): a repeat call carrying the same identity returns the existing session, 200
    rather than the first call's 201. 409 `session_exists` -- the service's own refusal -- when an
    explicit slug is already occupied by a different identity.

    `create_session_report` rather than `create_session`: the boolean it also returns is the one fact
    this route needs and the plain method's return value cannot carry. `strict_slug=True` is this
    route's own opt-in to refuse-on-conflict rather than the CLI's/Web's silently-suffixed default --
    see that method's own docstring for why the default must not simply change under every caller."""
    meta, created = sessions.create_session_report(
        body.request, context_cards=body.context_cards, slug=body.slug, strict_slug=True)
    return JSONResponse(meta.model_dump(), status_code=201 if created else 200)


@router.put("/sessions/{slug}/context-cards")
def rescope_session(body: ContextCardsRequest, slug: str = Depends(safe_slug),
                    sessions: SessionService = Depends(get_sessions)) -> dict:
    """Re-scope an existing session's context-card selection (`SessionService.rescope`) ->
    `RescopeResult.to_dict()`. Semantics unchanged from #168: a new revision is minted only once a
    model exists, nothing already saved is marked stale, and the next turn reasons under the new
    selection."""
    return sessions.rescope(slug, body.context_cards).to_dict()


@router.post("/sessions/{slug}/revisions")
def apply_revision(body: ApplyRevisionRequest, slug: str = Depends(safe_slug),
                   sessions: SessionService = Depends(get_sessions)) -> dict:
    """The apply -- validate a proposal and append it as a new revision
    (`SessionService.update_model`). This is the wire path for an external reasoner: something else
    reasons, this validated path applies -- the Claude Code shape (proposal file + `model apply
    --json`), given over an HTTP body instead of the filesystem. 409 `revision_conflict` when
    `expected_revision` is stale against the session's current revision."""
    result = sessions.update_model(slug, body.proposal, expected_revision=body.expected_revision,
                                   provenance={"surface": "api-apply"})
    return result.to_dict()


@router.post("/sessions/{slug}/revisions/preview")
def preview_revision(body: PreviewRevisionRequest, slug: str = Depends(safe_slug),
                     sessions: SessionService = Depends(get_sessions)) -> dict:
    """The dry run of the apply -- `UpdateResult` with `status: "planned"`, nothing written
    (`SessionService.diff`). Unlike `ApplyRevisionRequest`, this body carries no `expected_revision`:
    nothing is written, so there is no precondition to hold."""
    return sessions.diff(slug, body.proposal).to_dict()
