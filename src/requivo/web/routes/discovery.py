"""Discovery routes: run the first turn on a captured request, and fold in answers, through
`DiscoveryService`. The answers turn carries `expected_revision`, so a stale submission is a clean conflict.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from requivo.core.errors import InputTooLargeError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS, provider_status
from requivo.web.dependencies import get_discovery, get_sessions, safe_slug
from requivo.web.routes.sessions import _PROVIDER_FAILURE, analysis_failed
from requivo.web.spend import track_web_usage
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import session_detail
from requivo.web.viewmodels.status import impact_view
from requivo.web.viewmodels.usage import usage_view

router = APIRouter()


@router.post("/sessions/{slug}/discover")
def run_discovery(slug: str = Depends(safe_slug),
                  discovery: DiscoveryService = Depends(get_discovery)):
    """Run the first discovery turn on a 'create session only' session; a provider failure goes back
    to this page with the cause stated (#207)."""
    # Logged always; a 303 has no body, so the figure is stashed for the next GET (#253).
    with track_web_usage("web-discover", carry_to=slug):
        try:
            discovery.run_discovery(slug, surface="web-discover")
        except _PROVIDER_FAILURE as e:
            return analysis_failed(slug, e)
    return RedirectResponse(url=f"/sessions/{slug}", status_code=303)


@router.post("/sessions/{slug}/answers")
def submit_answers(
    request: Request,
    slug: str = Depends(safe_slug),
    answers: str = Form(...),
    expected_revision: int = Form(...),
    discovery: DiscoveryService = Depends(get_discovery),
    sessions: SessionService = Depends(get_sessions),
):
    """Fold the answers into the model as a new revision (optimistic-locked), returning the refreshed
    status region for an HTMX swap; a conflict is a clean error fragment."""
    # A plain form submit carries no `HX-Request` header (#428).
    is_htmx = request.headers.get("HX-Request") == "true"
    text = answers.strip()
    if len(text) > MAX_ANSWERS_CHARS:
        # Refused, not truncated (invariant 3), and re-rendered with the submission in it (#30, #428).
        # `test_oversized_answers_come_back_in_the_textarea`.
        if not is_htmx:
            detail = session_detail(sessions, slug)
            return templates.TemplateResponse(request, "sessions/detail.html", {
                "pending": False, "s": detail, "is_example": detail["is_example"],
                "provider": provider_status(),
                "answers_error": f"the answers exceed {MAX_ANSWERS_CHARS:,} characters — split them "
                                 "across two turns",
                "answers_error_code": InputTooLargeError.code,
                "submitted_answers": text,
            }, status_code=413)
        return templates.TemplateResponse(request, "sessions/_session.html", {
            "s": session_detail(sessions, slug),
            "provider": provider_status(),
            "answers_error": f"the answers exceed {MAX_ANSWERS_CHARS:,} characters — split them "
                             "across two turns",
            "answers_error_code": InputTooLargeError.code,
            "submitted_answers": text,
        }, status_code=413)
    # A fragment carries its own footprint; a no-JS submit stashes it for the next GET (#253, #428).
    # `test_a_no_js_redirect_does_not_leave_a_stash_the_next_unrelated_view_would_repeat`.
    with track_web_usage("web-answer", carry_to=None if is_htmx else slug) as spend:
        result = discovery.answer(slug, text, expected_revision=expected_revision,
                                  surface="web-answer")
        usage = usage_view(spend)
    if not is_htmx:
        # No fragment to swap; a 303 so a refresh cannot re-POST (#428).
        return RedirectResponse(url=f"/sessions/{slug}", status_code=303)
    return templates.TemplateResponse(request, "sessions/_session.html", {
        "s": session_detail(sessions, slug),
        "update": impact_view(result),
        "provider": provider_status(),
        "usage": usage,
    })
