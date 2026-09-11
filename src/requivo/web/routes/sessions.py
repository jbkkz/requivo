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

# The same logger `app.py` writes its 5xx lines to, so an unreadable session lands in the terminal
# the operator started the server in rather than in a channel of its own.
logger = logging.getLogger("requivo.web")

# The provider seam fails two ways on a first analysis: a transport failure (`EngineError` — API
# unavailable, output truncated) and the JSON retry loop giving up on a reply that never holds the
# contract (`ProviderOutputError`). `app.py`'s own `_status_for` already treats both as one family —
# 502 either way, "provider transport is a family, not a code" — so the recovery path here has to
# answer for both too. Catching `EngineError` alone left `ProviderOutputError` reaching the generic
# 500/502 handler instead of `analysis_failed`: the request was still saved, but the reader was sent
# to a page that does not say so.
_PROVIDER_FAILURE = (EngineError, ProviderOutputError)


# How long a provider's own words may be when they ride back on a URL. Not a security boundary --
# Jinja escapes the value and this server is local, single-user and says so in its own footer -- but a
# redirect is not the place for an unbounded string, and a message this long has stopped being a
# message.
_MAX_NOTICE_CHARS = 300

# The exact text `completion.py`'s give-up exit puts between the failure sentence and the saved-reply
# path (`saved_note = f" — the reply that failed validation was saved to {debug_path}"`), duplicated
# here rather than imported: importing from `providers.anthropic` would cross the surface-provider
# boundary this file otherwise stays clear of, for one string. `analysis_failed()` below matches it
# via `endswith` against the whole clause -- prefix and path together -- so the notice it truncates
# never ends on a dangling "...was saved to" with the path removed and nothing left to complete it.
_SAVED_NOTE_PREFIX = " — the reply that failed validation was saved to "


def analysis_failed(slug: str, exc: EngineError | ProviderOutputError) -> RedirectResponse:
    """Send the reader to the session that *was* saved, carrying why the analysis was not.

    Public, and shared with `routes/discovery.py`: both doors onto a first analysis can fail the same
    way and must land the reader in the same place. A second copy is a second wording. A redirect
    rather than a rendered page, so a refresh cannot re-POST a paid call. The cause travels as a query
    parameter because it is the actionable half. `exc` is one of `_PROVIDER_FAILURE` -- both members
    are `RequivoError`s with the same `.message` shape, read only for that, never the code, so the two
    failure classes render identically on purpose (#207).

    **The saved-reply path is carried as its own untruncated query parameter, and the whole connector
    clause (`_SAVED_NOTE_PREFIX`, matched via `endswith`) is stripped from what gets truncated, not
    only the path** -- otherwise a realistic contract violation loses the path entirely past
    `_MAX_NOTICE_CHARS`, and the shortest cause ends mid-filename at a path that does not resolve
    (#283, #362). Stripping the path alone and not the clause around it was tried first and reviewed
    out: it left the notice ending "...was saved to" with nothing after it, immediately followed by
    the template's own "was saved to <path>" sentence -- a dangling half-sentence worse to read than
    the truncation this exists to fix. Best-effort: a future reword of `completion.py`'s connector
    text this constant is not updated alongside simply stops matching, reverting to the unstripped
    (but never broken) clause. Pinned by
    `test_a_failed_first_analysis_lands_on_the_session_that_was_saved` and
    `test_a_retry_exhausted_analysis_carries_the_full_saved_reply_path_on_the_web_surface`.
    """
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
    """Create a session from a request and, by default, analyse it straight away.

    `provider` is `auto` unless the reader opened Advanced settings and chose otherwise: the server
    already knows whether a provider action can run, so asking the reader to declare it was asking
    them to answer a question the application could answer itself. `create_only` remains as the
    explicit 'capture it now, analyse later' choice, and is what `auto` resolves to when no provider
    is configured — a request is still worth keeping when there is nothing to reason with yet."""
    # Bounds are refusals, not truncations: a request silently cut at 20k would be reasoned over as if
    # it were the whole thing, and the user would never learn which half the engine saw.
    text = request_text.strip()
    # Two names because there are two meanings, and sharing one was a bug. `typed_slug` is the string
    # the reader put in the box — always a string, empty when they left it alone. `chosen_slug` is the
    # argument the service takes, where `None` means *derive a slug from the request*. When one
    # variable carried both, an empty box became `None` before the empty-request arm was reached, and
    # the re-render stringified it: the reader got `value="None"` in a field they never touched, which
    # also fails the field's own `pattern` and so had to be cleared before they could resubmit. A
    # refusal built to stop costing the reader work had started adding some.
    typed_slug = slug.strip()

    def refused(status: int, code: str, message: str):
        """Hand the page back with the submission still in it, rather than sending the reader to an
        error page whose only affordance is *Back to sessions*. Every refusal on this form goes
        through here, and reads `typed_slug` (what the reader submitted), never `chosen_slug` (what
        the service was going to be told) (#30). Pinned by
        `test_an_unusable_session_name_re_renders_rather_than_navigating_away`.
        """
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
        # The session-name field's *other* refusal, and it re-renders for the same reason. Leaving one
        # of one field's two refusals throwing the reader's work away is a worse state than either
        # arm alone, because which one they hit is not something they can predict.
        try:
            validate_slug(typed_slug)
        except InvalidSlugError as exc:
            return refused(400, exc.code, exc.message)
    # An empty box means *derive a slug from the request*, which the service spells `None`. Computed
    # here as its own value rather than by overwriting `typed_slug`, so nothing below can hand the
    # service's vocabulary back to the reader as if they had typed it.
    chosen_slug = typed_slug or None
    # An unknown card is an error, not something to filter out: dropping it leaves an empty selection,
    # which every reader downstream treats as "load every card" — the opposite of narrowing.
    #
    # This one is deliberately left to raise. The card boxes are checkboxes over a set this page
    # rendered, so an unknown value did not come from a reader mistyping something they could correct
    # on a re-render — it came from a submission that did not originate in this form. Re-rendering
    # would dress a tampered request as a typo.
    picked = resolve_cards(cards)

    if not text:
        return refused(400, "empty_request",
                       "A request is required — paste the client or stakeholder email, or describe "
                       "what was asked in your own words.")

    if provider == "auto":
        provider = "anthropic" if provider_status().available else "create_only"
    if provider == "anthropic":
        # Claim, then reason — two service operations rather than `start()`, because the route needs
        # the slug in its hands *before* the paid call can fail (#207). `claim_session` is public for
        # exactly this: a surface that owns its own flow takes invariant 13's gate itself. Nothing is
        # reimplemented here; `run_discovery` is the same operation the pending page's own button
        # already posts to.
        new_slug = discovery.claim_session(text, cards=picked, slug=chosen_slug).slug
        # Logged always; carried to the following GET when there is a figure to carry (#253) --
        # `routes/discovery.py`'s copy of this call carries the same shape and the same reasoning.
        # This is the very first paid call a new user ever makes, and its result rides the redirect
        # to `/sessions/{new_slug}` server-side rather than on the URL.
        with track_web_usage("web-discover", carry_to=new_slug):
            try:
                discovery.run_discovery(new_slug, surface="web-discover")
            except _PROVIDER_FAILURE as e:
                # The request is already safely captured at revision 0, and the page we are about to
                # send them to *already* offers the retry button. Letting this propagate mapped it to
                # a 502 and `errors/500.html` — "Something went wrong… check the server logs. Back to
                # sessions." — which says nothing about the pasted email having been saved, so a
                # first-time user on a transient API error reasonably concludes the product ate their
                # request. The good outcome was one redirect away the whole time. Pinned by
                # `test_a_failed_first_analysis_lands_on_the_session_that_was_saved`.
                return analysis_failed(new_slug, e)
    else:
        new_slug = discovery.create_only(text, cards=picked, slug=chosen_slug)
    return RedirectResponse(url=f"/sessions/{new_slug}", status_code=303)


@router.post("/sessions/example")
def create_example(sessions: SessionService = Depends(get_sessions),
                   artifacts: ArtifactService = Depends(get_artifacts)):
    """Materialise the bundled example and go to it -- the keyless activation path. A POST rather
    than a link, because it writes: it creates a session in the reader's workspace, so it carries the
    cross-site token every other write on this app carries, and a refresh cannot silently re-run it
    (#226). Every decision this makes lives in `web/example.py`, not here -- including, since #429,
    that the decision brief is seeded alongside the model. This is a redirect around one service
    call, which is what lets a second surface reuse the operation without reimplementing the policy.
    Pinned by `test_seeding_is_refused_without_the_cross_site_token`,
    `test_one_click_yields_a_browsable_session_with_no_key_and_no_call` and
    `test_one_click_also_seeds_the_decision_brief_no_key_needed`.
    """
    return RedirectResponse(url=f"/sessions/{seed_example(sessions, artifacts)}", status_code=303)


def _unreadable_session(request: Request, slug: str, exc: BaseException):
    """The session page for a session nobody could read (#240).

    `home.html` has promised since #7 that "the session screen is where the full error is stated,
    and that is the one place a reader can act on it". It was not: every read on this route raised,
    and the reader got `errors/error.html` or `errors/500.html` — a generic page naming neither the
    session nor anything to run. So the row humanised by #240 pointed at a page that knew less than
    the row did.

    **The status does not move.** Whatever the exception would have been reported as, it still is:
    409 for a session written by a newer Requivo, 500 for a store that could not answer. A page
    that explains a failure does not turn it into a success, and `session_page`'s status is part of
    a public surface. `http_status_for` used to be `app.py`'s own `_status_for`, imported inside the
    function because `app.py` imports this module — the cycle was real, and by the time a request was
    served `app` was fully imported. #422 moved the classification to `requivo.http`, which this
    module has no cycle with, so the import now sits at module level like every other one here. Pinned
    by `test_opening_an_unreadable_session_answers_with_the_status_it_always_did`.

    Logged as well as rendered. The page is written for the reader; the operator needs the same
    fact in the terminal they started the server in, which is where the app's own 5xx arm already
    puts one.
    """
    status = http_status_for(exc) if isinstance(exc, RequivoError) else 500
    code = exc.code if isinstance(exc, RequivoError) else "session_unreadable"
    # **The traceback rides the non-`RequivoError` arm and only that arm**, because the catch above
    # is deliberately open and therefore also catches what nobody anticipated. A `RequivoError` is a
    # state this build has a code and a sentence for, and a stack under it is noise; anything else
    # may be a defect in this codebase, and without `exc_info` the operator's log renders "the store
    # is broken" and "we have a bug" as one identical line carrying `str(exc)` and nothing else.
    # `app.py`'s sibling handler for the same unanticipated family already uses `logger.exception`
    # for this reason; this is that decision, kept rather than quietly dropped one route along.
    logger.error("session '%s' could not be read (%s): %s", slug, code, exc,
                 exc_info=None if isinstance(exc, RequivoError) else exc)
    return templates.TemplateResponse(request, "sessions/unreadable.html", {
        "slug": slug, "status": status, "code": code, "detail": str(exc),
    }, status_code=status)


@router.get("/sessions/{slug}")
def session_page(request: Request, slug: str = Depends(safe_slug),
                 sessions: SessionService = Depends(get_sessions)):
    """One session, or the page for one nobody could read (#240).

    **The guard wraps the reads and stops there.** `_session_view` returns a template name and a
    context and renders nothing; the render happens below, outside the `try`. That split is the
    whole of this guard's correctness, because Starlette renders a `Jinja2Templates.TemplateResponse`
    *eagerly*, inside `_TemplateResponse.__init__` — so a `try` that spans the render catches
    Jinja's own `UndefinedError` from a context key a route forgot to pass, and reports a defect in
    this codebase to the reader as *your session is corrupt on disk* and to the operator as the
    same. An absence produced by the guard, dressed as an absence in the world.

    **The existence check is inside the `try` and that is not tidiness.** `sessions.exists` reaches
    `core.persistence._probe`, which raises `SessionUnreadableError` for any `OSError` that is not
    "no such thing" — an `EACCES` on a parent directory, say. That is precisely a session that is
    there and could not be read, and outside the guard it fell to the app-wide handler and the
    generic page this route exists to replace. None of the three break modes in
    `tests/web/test_degraded_listing.py` reach it, which is how it survived the first draft.
    """
    try:
        template, context = _session_view(request, slug, sessions)
    except SessionNotFoundError:
        # Re-raised deliberately: "there is no such session" is a 404 the app already renders well,
        # and it is not the third state. Only a session that *is* there and could not be read gets
        # the page below.
        raise
    except Exception as exc:  # noqa: BLE001 - the set of ways a session can be broken is open
        # The same argument `SessionService.list_entries` makes for its own bare catch, at the one
        # route that opens a single named session: a truncated `model.json` (pydantic), a
        # `request.md` replaced by a directory (`OSError`), a `format_version` from a newer build
        # (`RequivoError`). Naming a family here is how the guard ends up nominally on and
        # effectively off for the next failure mode.
        return _unreadable_session(request, slug, exc)
    return templates.TemplateResponse(request, template, context)


def _session_view(request: Request, slug: str, sessions: SessionService) -> tuple[str, dict]:
    """Everything this route *reads*, and nothing it renders — (template name, context).

    Its guard therefore sits above all of the reads rather than around the first one: wrapping only
    `sessions.meta(slug)` would leave `session_detail`'s own reads — the model, the status, the
    request — outside it, which is the shape of #7 one route along. And it stops short of the
    render, for the reason `session_page` states.

    `request` is read for its query parameters only; nothing here renders with it.
    """
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    meta = sessions.meta(slug)
    # Popped unconditionally: `None` on every ordinary page view (nothing was ever stashed for this
    # slug), and the one figure a create/discover redirect just stashed on the landing view right
    # after it (#253). Read-once by construction -- a reload of this same page pops nothing a second
    # time, which is deliberate (see `spend.py`).
    usage = pop_web_usage(slug)
    if meta.current_revision == 0:
        # Read once and used twice on this branch; the branch below gets it from `session_detail`,
        # which reads it for the same two purposes there.
        request_text = sessions.request_text(slug)
        # 'Create session only' with no discovery yet — offer to run it. `usage` is set here only when
        # a first analysis spent tokens and then failed: the model never advanced past revision 0, but
        # the spend is real and already logged, so the retry page states it rather than the silence a
        # bare "your request was saved" would otherwise leave.
        return "sessions/detail.html", {
            "pending": True, "slug": slug,
            "request_text": request_text, "context_cards": meta.context_cards,
            "provider": provider_status(),
            # A seeded example whose apply did not land is still the example, and still says so
            # (#226). Ordinarily unreachable — `seed_example` applies the model in the same call —
            # but a crash between the two leaves exactly this page.
            "is_example": is_example(request_text),
            # Set only by `analysis_failed`'s redirect. Anyone can put it in a URL by hand, which on a
            # local single-user server with no authentication is not a boundary worth defending -- and
            # Jinja escapes it either way.
            "analysis_failed": request.query_params.get("analysis_failed"),
            # The saved-reply path (#283), carried outside the truncated notice above and rendered in
            # full (#362) -- absent whenever the failure was an `EngineError` or the debug write
            # itself failed, in which case `analysis_failed`'s own sentence already covers the cause
            # without a path to attach.
            "analysis_failed_path": request.query_params.get("analysis_failed_path"),
            "usage": usage,
        }
    detail = session_detail(sessions, slug)
    return "sessions/detail.html", {
        "pending": False, "s": detail,
        # Lifted to the top level so `detail.html` can announce the sample once, above the branch
        # that splits on `pending` (#226). Both branches answer the same question and a template
        # asking it two ways is a template that will answer it two ways.
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
    """The explicit confirmation step the issue asks for (#238) — a GET that only ever renders a
    page; nothing is removed until the reader submits the form on it, which is the POST below and
    carries the CSRF token like every other write on this app."""
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    return templates.TemplateResponse(request, "sessions/delete_confirm.html", {"slug": slug})


@router.post("/sessions/{slug}/delete")
def delete_session(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)):
    """Irreversibly remove a session and send the reader home, where it no longer appears (#238).
    Refuses a slug nothing occupies with the same 404 every other route on a missing session answers
    with -- reached before the delete itself, which is `SessionService.delete_session`'s own refusal
    otherwise (see `Store.delete_session`'s docstring for the lock/removal ordering)."""
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    sessions.delete_session(slug)
    return RedirectResponse(url="/", status_code=303)
