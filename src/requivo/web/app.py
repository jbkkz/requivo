"""The FastAPI application factory for Requivo Web.

`create_app()` wires the routers, mounts the local static files (and *only* those — never the
workspace, `.requivo`, `.env` or `.git`), sets conservative security headers, and turns structured
`RequivoError`s into clean pages (never a traceback). It is a factory: importing this module binds no
port and starts no server — `requivo web` calls it.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from requivo.core.errors import RequivoError
from requivo.http import http_status_for as _status_for
from requivo.security_headers import CACHE_CONTROL, DEFAULT_CSP, REFERRER_POLICY, apply_security_headers
from requivo.web.routes import artifacts, discovery, health, home, sessions
from requivo.web.security import install_cross_site_guard
from requivo.web.templating import STATIC_DIR, templates

# The error-code -> HTTP-status table, its per-row reasoning and `http_status_for` (imported above
# as `_status_for`, so the call site below reads unchanged) moved to `requivo.http` in #422 --
# framework-free by construction, so a second HTTP surface (the hosted product, a future local API
# facade) never has to fork the table or reach a private name out of this optional extra. Nothing
# in this module reads the table or the unclassified default directly any more, so neither is
# imported here; the guard test that keeps the table honest
# (`test_every_error_code_has_an_explicit_http_status`) moved with it, to
# tests/test_http_status_table.py.

# A locked-down CSP: everything is same-origin, images may be inline data URIs. No external hosts, so
# the vendored HTMX and local CSS are the only scripts/styles — nothing loads from a CDN. This is
# `requivo.security_headers.DEFAULT_CSP` (#503) — restated as `_CSP` here only so the long comment
# above, which is what a reader actually wants when they land on this line, keeps a name to attach to.
_CSP = DEFAULT_CSP

# `no-referrer` here made the app unusable in a browser, and the mechanism is worth stating in full
# because both halves were individually correct (#47).
#
# A `Referrer-Policy` governs the requests *our own pages* make. It says nothing about a request some
# other page sends to this server — that is governed by that page's policy, not ours — so this header
# has never been part of the cross-site guard's defence. It only ever constrains us.
#
# What it constrained was the `Origin` header on our own forms. Fetch's *append a request `Origin`
# header* consults the referrer policy for any request that is **not** CORS-mode and whose method is
# not `GET`/`HEAD`, and under `no-referrer` it replaces the serialized origin with the opaque value
# `null`. An ordinary HTML form submit is a navigation, not CORS — so `home.html` (create a session)
# and `sessions/detail.html` (run discovery), the two plain-form posts in this app, arrived carrying
# `Origin: null`. `security._enforce` refuses the opaque origin deliberately (#43). The result was a
# 403 on the product's entry path, from a same-origin request carrying a valid token.
#
# The HTMX posts (answers, generation) go out as XHR, which is CORS-mode, so that clause never applied
# to them and they were unaffected. That asymmetry is why the failure looked like "the form is broken"
# rather than "the app is broken".
#
# `same-origin` is the strictest value that leaves the guard something to read. It sends the full
# referrer within this app and **nothing at all** to any other origin, so the privacy intent of
# `no-referrer` is kept for the only case where it did anything: navigating away. The alternatives and
# what they cost:
#
#   * `strict-origin-when-cross-origin` (the browser default) also fixes it, and hands a third party
#     `http://localhost:8765` on any outbound navigation — a gratuitous "this machine runs Requivo Web
#     on this port" that buys nothing here.
#   * Removing the header defers to the browser's default, which is that same value on current
#     browsers and unstated on older ones. This app states its headers rather than inheriting them.
#   * `origin` / `unsafe-url` leak strictly more, for nothing.
#
# The cost of `same-origin` is that a same-origin `Referer` now carries the full URL, session slug
# included — the reader's own request name, travelling to the server already holding it.
#
# For a *same-origin* request, `no-referrer` is the only policy value that produces `Origin: null`:
# the downgrade-sensitive values (`strict-origin`, `no-referrer-when-downgrade`,
# `strict-origin-when-cross-origin`) null it only on an HTTPS→HTTP downgrade, which same-origin cannot
# be, and `same-origin` itself nulls it only when the request *is* cross-origin.
# `test_the_policy_this_app_sends_and_the_origin_guard_it_runs_agree` asserts that composition — the
# defect lived between two files, so no per-file test could see it.
#
# This is `requivo.security_headers.REFERRER_POLICY` (#503) — restated as `_REFERRER_POLICY` here
# only so the argument above, which is what a reader actually wants on arriving at this line, keeps
# a name to attach to.
_REFERRER_POLICY = REFERRER_POLICY

# Nothing this app answers with may be written to the browser's disk cache, except the assets it
# ships (#218).
#
# Every page renders the reader's own client request and the model built from it, plus the cross-site
# request token; `/sessions/{slug}/export` hands back the whole model as JSON and an artifact download
# hands back the PRD as Markdown. On a shared machine a disk copy outlives the "sessions stay on this
# machine" promise in spirit, and it also produces the two stale states this app already has to
# apologise for: a cached page carries an old `expected_revision` (409 with a reload link) and, after
# a restart, an old token (403 saying reload the page).
#
# **The rule is keyed on the path and fails closed, deliberately not on the content type.** Keying on
# `text/html` is the reflex and it is wrong here: it covers the pages and the HTMX fragments and
# misses `export` (`application/json`) and the artifact download (`text/markdown`) — the two
# responses carrying the most of the reader's material there is. So `no-store` is the default and
# what this package *ships* is the exception. A route added later is covered by nobody remembering to
# cover it; the cost of the default being wrong is one re-fetch of a stylesheet, and the cost of the
# allowlist being wrong is a session page on somebody's disk.
#
# `/static` is the mount, `/favicon.ico` is `health.py` serving one file out of the same directory.
# Both are fingerprint-free, so this deliberately says nothing about *how long* they may be cached —
# an ETag/max-age strategy is a separate question and is out of scope here.
# `test_no_response_carrying_the_readers_material_may_be_written_to_the_disk_cache` and
# `test_a_bundled_asset_stays_cacheable` are the two halves.
_BUNDLED_ASSET_PREFIXES = ("/static/",)
_BUNDLED_ASSET_PATHS = ("/favicon.ico",)
# `requivo.security_headers.CACHE_CONTROL` (#503), restated under this app's own name for the same
# reason as `_REFERRER_POLICY` above.
_CACHE_CONTROL = CACHE_CONTROL


def _is_bundled_asset(path: str) -> bool:
    """Does this path name a file shipped inside the package rather than anything of the reader's?"""
    return path in _BUNDLED_ASSET_PATHS or path.startswith(_BUNDLED_ASSET_PREFIXES)


def _apply_security_headers(response, path: str):
    """State this app's header policy on one response — the single definition, read by both the
    middleware and the unhandled-exception handler (#462).

    Two callers because Starlette leaves no choice, and **one** definition because the alternative is
    #340 re-armed. `_unexpected` is registered for `Exception`, which Starlette handles inside
    `ServerErrorMiddleware` — *outside* the user middleware stack — so `security_headers` never sees
    its response, and reordering middleware cannot reach it. #340 closed that by writing the four
    headers out a second time in the handler, which fixed the four that existed and left the fifth
    header anyone adds to the middleware silently missing from the 500 page: the original defect,
    one header along. Pinned by
    `test_the_500_page_carries_every_header_an_ordinary_page_carries`, which compares the two
    responses' header sets rather than a list of names, so it fails on a header it was never told
    about.

    The actual header-setting is `requivo.security_headers.apply_security_headers` (#503) — moved
    out of this module once a second FastAPI app (`api/app.py`) needed the identical policy and
    found nothing here it could reach without importing this optional `[web]` extra. This wrapper
    stays so both call sites below read unchanged, and so the bundled-asset exemption
    (`_is_bundled_asset`, a fact about *this* app's own static mount) is supplied once, here, rather
    than at every caller.
    """
    return apply_security_headers(response, path, csp=_CSP, is_bundled_asset=_is_bundled_asset)

# Under `requivo web` this rides uvicorn's handler, so a traceback lands in the terminal the user
# started the server in — the only place a local, single-user app has to put one.
logger = logging.getLogger("requivo.web")


def _render_error(request: Request, status: int, code: str, message: str):
    """Render an error the way the request expects: a small inline fragment for an HTMX swap, a full
    page otherwise. Never leaks a traceback."""
    ctx = {"status": status, "code": code, "message": message}
    if request.headers.get("HX-Request") == "true":
        response = templates.TemplateResponse(request, "errors/_error.html", ctx, status_code=status)
        # **Retargeted, because the fragment is one line and the form's target is the whole region**
        # (#203). `app.js` now swaps 4xx/5xx instead of dropping them; without these headers that
        # opt-in would swap this notice *over* `#session-body` — deleting the textarea the reader had
        # just typed into, which is exactly the destruction #30 was filed to stop. `#flash` lives in
        # `base.html`, outside every swap target, so the message appears and nothing is taken away.
        # The routes that already return a full region with the error stated on it (the 413 answers
        # refusal) do not come through here and keep their own target.
        response.headers["HX-Retarget"] = "#flash"
        response.headers["HX-Reswap"] = "innerHTML"
        return response
    template = "errors/404.html" if status == 404 else (
        "errors/500.html" if status >= 500 else "errors/error.html")
    return templates.TemplateResponse(request, template, ctx, status_code=status)


def create_app() -> FastAPI:
    app = FastAPI(title="Requivo Web", docs_url=None, redoc_url=None, openapi_url=None)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Installed before the header middleware so it ends up *inside* it: a request the guard turns away
    # still leaves with the same CSP and nosniff headers as any other response.
    install_cross_site_guard(app, _render_error)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        return _apply_security_headers(await call_next(request), request.url.path)

    @app.exception_handler(RequivoError)
    async def _requivo_error(request: Request, exc: RequivoError):
        status = _status_for(exc)
        if status >= 500:
            # A 5xx here is our fault, not the reader's, and the page they get says almost nothing.
            # Without this the operator has no record of a condition the reader cannot act on — the
            # same argument the unexpected-exception handler below already makes.
            logger.error("%s serving %s %s: %s", exc.code, request.method, request.url.path,
                         exc.message)
        return _render_error(request, status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return _render_error(request, 400, "invalid_request", "The form submission was not valid.")

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):
        return _render_error(request, 404, "not_found", "That page does not exist.")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # No traceback to the browser — but it has to go *somewhere*, or an unexpected failure is
        # invisible: the user sees a generic page and the operator has nothing to debug from. The
        # method and path are enough to locate it; the request body is deliberately not logged, since
        # it is the user's own product request.
        logger.exception("unhandled error serving %s %s", request.method, request.url.path)
        # The policy is stated here as well as in the middleware because this handler runs outside
        # the user middleware stack, so `security_headers` never sees this response (#340). One
        # definition, two callers — the argument is on `_apply_security_headers` (#462).
        return _apply_security_headers(
            _render_error(request, 500, "internal_error", "Something went wrong on the server."),
            request.url.path)

    app.include_router(health.router)
    app.include_router(home.router)
    app.include_router(sessions.router)
    app.include_router(discovery.router)
    app.include_router(artifacts.router)
    return app
