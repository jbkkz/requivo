"""Session routes — create a discovery, list is on home, view one session, export its model."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from requivo.core.context import resolve_cards
from requivo.core.errors import (
    InputTooLargeError,
    InvalidSlugError,
    ProviderOutputError,
    RequivoError,
    SessionNotFoundError,
)
from requivo.core.persistence import validate_slug
from requivo.http import http_status_for
from requivo.providers.errors import EngineError
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_REQUEST_CHARS, MAX_SLUG_CHARS, provider_status
from requivo.web.dependencies import get_artifacts, get_discovery, get_sessions, safe_slug
from requivo.web.example import is_example, seed_example
from requivo.web.routes.home import home_context
from requivo.web.spend import pop_web_usage, track_web_usage
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import session_detail

router = APIRouter()

# The same logger `app.py` writes its 5xx lines to.
logger = logging.getLogger("requivo.web")

# Both ways a first analysis fails at the provider seam: `_status_for` treats them as one family (502),
# so the recovery path answers for both.
_PROVIDER_FAILURE = (EngineError, ProviderOutputError)


# How long a provider's words may be on a URL: not a security boundary, but a redirect is not the place for an unbounded string.
_MAX_NOTICE_CHARS = 300

# `completion.py`'s connector text, duplicated rather than imported across the surface-provider boundary;
# `analysis_failed()` matches the whole clause via `endswith`, so a truncated notice never dangles.
_SAVED_NOTE_PREFIX = " — the reply that failed validation was saved to "


def analysis_failed(slug: str, exc: EngineError | ProviderOutputError) -> RedirectResponse:
    """Send the reader to the session that *was* saved, carrying why the analysis was not; shared with
    `routes/discovery.py`. A redirect, so a refresh cannot re-POST a paid call (#207). The saved-reply
    path rides its own untruncated parameter, with the whole connector clause stripped before
    truncation (#283, #362). `test_a_failed_first_analysis_lands_on_the_session_that_was_saved`,
    `test_a_retry_exhausted_analysis_carries_the_full_saved_reply_path_on_the_web_surface`."""
    message = exc.message
    saved_path = exc.details.get("raw_reply_path") if isinstance(exc, ProviderOutputError) else None
    if saved_path:
        full_clause = f"{_SAVED_NOTE_PREFIX}{saved_path}"
        if message.endswith(full_clause):
            message = message[: -len(full_clause)]
    notice = quote(message[:_MAX_NOTICE_CHARS])
    url = f"/sessions/{slug}?analysis_failed={notice}"
    if saved_path:
        url += f"&analysis_failed_path={quote(str(saved_path))}"
    return RedirectResponse(url=url, status_code=303)


@router.post("/sessions")
def create_session(
    request: Request,
    request_text: str = Form(...),
    slug: str = Form(""),
    cards: list[str] = Form(default=[]),
    provider: str = Form("auto"),
    sessions: SessionService = Depends(get_sessions),
    discovery: DiscoveryService = Depends(get_discovery),
):
    """Create a session from a request and, by default, analyse it. `provider` is `auto` unless the
    reader chose otherwise; `create_only` is what `auto` resolves to with no provider configured."""
    # Bounds are refusals, not truncations (invariant 3).
    text = request_text.strip()
    # Two names for two meanings: `typed_slug` is what the reader submitted (always a string),
    # `chosen_slug` what the service takes (`None` derives); one variable re-rendered `value="None"`.
    typed_slug = slug.strip()

    def refused(status: int, code: str, message: str):
        """Re-render the form with the submission still in it, reading `typed_slug`, never
        `chosen_slug` (#30). `test_an_unusable_session_name_re_renders_rather_than_navigating_away`."""
        return templates.TemplateResponse(request, "home.html", home_context(
            sessions, error=message, error_code=code,
            form={"request_text": text, "slug": typed_slug, "cards": cards, "provider": provider},
        ), status_code=status)

    if len(text) > MAX_REQUEST_CHARS:
        return refused(413, InputTooLargeError.code,
                       f"the product request exceeds {MAX_REQUEST_CHARS:,} characters — trim it and "
                       "resubmit")
    if len(typed_slug) > MAX_SLUG_CHARS:
        return refused(413, InputTooLargeError.code,
                       f"the session name exceeds {MAX_SLUG_CHARS} characters")
    if typed_slug:
        # The session-name field's other refusal re-renders for the same reason.
        try:
            validate_slug(typed_slug)
        except InvalidSlugError as exc:
            return refused(400, exc.code, exc.message)
    # An empty box means *derive*, spelled `None` for the service, computed as its own value.
    chosen_slug = typed_slug or None
    # An unknown card is an error, not filtered (invariant 3); left to raise, since the boxes are
    # checkboxes this page rendered, so a bad value did not come from a typo.
    picked = resolve_cards(cards)

    if not text:
        return refused(400, "empty_request",
                       "A request is required — paste the client or stakeholder email, or describe "
                       "what was asked in your own words.")

    if provider == "auto":
        provider = "anthropic" if provider_status().available else "create_only"
    if provider == "anthropic":
        # Claim, then reason, so the route holds the slug before the paid call can fail (#207).
        new_slug = discovery.claim_session(text, cards=picked, slug=chosen_slug).slug
        # Logged always; carried to the following GET server-side when there is a figure (#253).
        with track_web_usage("web-discover", carry_to=new_slug):
            try:
                discovery.run_discovery(new_slug, surface="web-discover")
            except _PROVIDER_FAILURE as e:
                # The request is captured at revision 0 and the target page offers the retry button.
                # `test_a_failed_first_analysis_lands_on_the_session_that_was_saved`.
                return analysis_failed(new_slug, e)
    else:
        new_slug = discovery.create_only(text, cards=picked, slug=chosen_slug)
    return RedirectResponse(url=f"/sessions/{new_slug}", status_code=303)


@router.post("/sessions/example")
def create_example(sessions: SessionService = Depends(get_sessions),
                   artifacts: ArtifactService = Depends(get_artifacts)):
    """Materialise the bundled example and go to it, the keyless activation path (#226): a POST,
    since it writes, carrying the cross-site token. The policy lives in `web/example.py`.
    `test_seeding_is_refused_without_the_cross_site_token`,
    `test_one_click_also_seeds_the_decision_brief_no_key_needed`."""
    return RedirectResponse(url=f"/sessions/{seed_example(sessions, artifacts)}", status_code=303)


def _unreadable_session(request: Request, slug: str, exc: BaseException):
    """The session page for a session nobody could read (#240): names the session and what to run.
    The status does not move (409 for a newer format, 500 for a store that could not answer):
    `test_opening_an_unreadable_session_answers_with_the_status_it_always_did`. Logged too."""
    status = http_status_for(exc) if isinstance(exc, RequivoError) else 500
    code = exc.code if isinstance(exc, RequivoError) else "session_unreadable"
    # The traceback rides the non-`RequivoError` arm only: anything else may be a defect here.
    logger.error("session '%s' could not be read (%s): %s", slug, code, exc,
                 exc_info=None if isinstance(exc, RequivoError) else exc)
    return templates.TemplateResponse(request, "sessions/unreadable.html", {
        "slug": slug, "status": status, "code": code, "detail": str(exc),
    }, status_code=status)


@router.get("/sessions/{slug}")
def session_page(request: Request, slug: str = Depends(safe_slug),
                 sessions: SessionService = Depends(get_sessions)):
    """One session, or the page for one nobody could read (#240). The guard wraps the reads and
    stops before the render: Starlette renders a `TemplateResponse` eagerly, so a `try` spanning it
    would report a missing context key as a corrupt session. The existence check is inside the
    `try`, since `_probe` raises `SessionUnreadableError` on `EACCES`."""
    try:
        template, context = _session_view(request, slug, sessions)
    except SessionNotFoundError:
        # Re-raised: "no such session" is a 404, not the third state.
        raise
    except Exception as exc:  # noqa: BLE001 - the set of ways a session can be broken is open
        # Bare catch, as `SessionService.list_entries` argues: the failure modes are open-ended.
        return _unreadable_session(request, slug, exc)
    return templates.TemplateResponse(request, template, context)


def _session_view(request: Request, slug: str, sessions: SessionService) -> tuple[str, dict]:
    """Everything this route reads and nothing it renders: (template name, context), so the guard
    sits above all of the reads (#7). `request` is read for its query parameters only."""
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    meta = sessions.meta(slug)
    # Popped unconditionally: read-once by construction (#253, `spend.py`).
    usage = pop_web_usage(slug)
    if meta.current_revision == 0:
        # Read once and used twice on this branch.
        request_text = sessions.request_text(slug)
        # 'Create session only' with no discovery yet; `usage` is set only when a first analysis spent and failed.
        return "sessions/detail.html", {
            "pending": True, "slug": slug,
            "request_text": request_text, "context_cards": meta.context_cards,
            "provider": provider_status(),
            # A seeded example whose apply did not land is still the example (#226).
            "is_example": is_example(request_text),
            # Set only by `analysis_failed`'s redirect; Jinja escapes it.
            "analysis_failed": request.query_params.get("analysis_failed"),
            # The saved-reply path (#283, #362), absent when there was none.
            "analysis_failed_path": request.query_params.get("analysis_failed_path"),
            "usage": usage,
        }
    detail = session_detail(sessions, slug)
    return "sessions/detail.html", {
        "pending": False, "s": detail,
        # Lifted to the top level so `detail.html` announces the sample once (#226).
        "is_example": detail["is_example"],
        "provider": provider_status(),
        "usage": usage,
    }


@router.get("/sessions/{slug}/export")
def export_model(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)):
    """Download the validated model — the durable product — as JSON."""
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    model_json = sessions.load_model(slug).model_dump_json(indent=2)
    return PlainTextResponse(model_json, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{slug}.model.json"'})


@router.get("/sessions/{slug}/delete")
def delete_session_confirm(request: Request, slug: str = Depends(safe_slug),
                           sessions: SessionService = Depends(get_sessions)):
    """The explicit confirmation step (#238): a GET that only renders; the POST below removes."""
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    return templates.TemplateResponse(request, "sessions/delete_confirm.html", {"slug": slug})


@router.post("/sessions/{slug}/delete")
def delete_session(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)):
    """Irreversibly remove a session and send the reader home (#238); a missing slug is the usual 404."""
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    sessions.delete_session(slug)
    return RedirectResponse(url="/", status_code=303)
