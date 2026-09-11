"""Home — the primary product experience: paste a request, and the sessions already in progress.

There is no separate 'new discovery' page. A tool whose first screen explains itself and whose second
screen is the one that does the work has spent its best moment on an introduction; the request box
belongs where the reader lands.
"""

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
    """Everything the home page renders — shared with the create route, which re-renders this page
    when a submission is refused rather than sending the reader elsewhere to be told.

    `form` is what the reader last submitted, kept as a context key so every refusal on this page --
    not only the empty-request one -- re-renders here with the submission intact rather than sending
    the reader elsewhere to fetch it again from wherever it came from (#30). Refusing itself is
    unchanged (invariant 3, *refuse, don't truncate*); what changed is what the refusal costs. Pinned
    by `test_the_refusal_is_the_home_page_not_a_dead_end` and
    `test_an_oversized_request_comes_back_in_the_textarea`.
    """
    cards = available_cards()
    # #257: the create form's default (every box unchecked) reasons over every card, which is the
    # most expensive and most diluted path -- a one-line hint states the measured per-card cost so
    # the visibly-recommended path is the cheap and sharp one, not the default. Computed here rather
    # than typed into the template so a card added, removed or resized moves the number itself
    # instead of a literal quietly going stale (CLAUDE.md's own rule about a count nothing falsifies).
    return {"sessions": session_list(sessions), "provider": provider_status(),
            "cards": cards, "card_avg_bytes": average_card_byte_size(),
            "form": empty_form(), **extra}


@router.get("/")
def home(request: Request, sessions: SessionService = Depends(get_sessions)):
    return templates.TemplateResponse(request, "home.html", home_context(sessions))


@router.get("/sessions/new")
def new_session():
    """Retired: the request form *is* the home page. Kept as a redirect so an existing bookmark still
    lands somewhere useful instead of on a 404."""
    return RedirectResponse(url="/", status_code=307)
