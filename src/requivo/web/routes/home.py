"""Home: paste a request, and the sessions in progress. There is no separate 'new discovery' page."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from requivo.core.context import available_cards, average_card_byte_size
from requivo.services.sessions import SessionService
from requivo.web.config import provider_status
from requivo.web.dependencies import get_sessions
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import session_list

router = APIRouter()


def empty_form() -> dict:
    """A blank create form. A fresh dict per call, so no caller can edit the next reader's page."""
    return {"request_text": "", "slug": "", "cards": [], "provider": "auto"}


def home_context(sessions: SessionService, **extra) -> dict:
    """Everything the home page renders, shared with the create route, which re-renders here with the
    submission intact when it refuses (#30). `test_an_oversized_request_comes_back_in_the_textarea`."""
    cards = available_cards()
    # The measured per-card cost (#257), computed rather than typed so it cannot go stale.
    return {"sessions": session_list(sessions), "provider": provider_status(),
            "cards": cards, "card_avg_bytes": average_card_byte_size(),
            "form": empty_form(), **extra}


@router.get("/")
def home(request: Request, sessions: SessionService = Depends(get_sessions)):
    return templates.TemplateResponse(request, "home.html", home_context(sessions))


@router.get("/sessions/new")
def new_session():
    """Retired: the request form is the home page; a redirect so a bookmark still lands somewhere."""
    return RedirectResponse(url="/", status_code=307)
