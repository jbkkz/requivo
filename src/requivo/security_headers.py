"""The security-header policy shared by every FastAPI surface this project ships (#503).

`Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy` and `Cache-Control` used to
be defined once -- in `web/app.py` -- and consumed by exactly one surface. `api/app.py` (#425)
added a second FastAPI app three weeks later and installed no middleware at all: `web/app.py`'s own
`_apply_security_headers` was correct about the surface it consolidated, and the new module was a
clean addition on its own, so neither pull request's diff contained the defect. It existed only in
their composition -- "stated once" was a property of that module's definition, not of the codebase
having one. See #503 for the incident, and `CLAUDE.md`'s argument, made first about a neutral
concept trapped in `providers/`, for the general shape: move the concept out of the module that
happens to own it today, rather than writing every future consumer a second copy or an allowlist
entry.

Framework-free, on the same argument `requivo/http.py` makes for itself: `apply_security_headers`
only ever calls `response.headers.setdefault(...)`, an interface every Starlette/FastAPI response
already satisfies, so this module needs no import from fastapi or starlette. `[api]` does not
depend on `[web]` (see `pyproject.toml`), and this module is reachable from a base install with
neither extra -- `tests/test_boundaries.py` scans `api/` and `web/` as surfaces, not this module,
because it is neutral by construction rather than by allowlist entry.
"""

from __future__ import annotations

from collections.abc import Callable

# A page served by one of this project's own apps governs the requests *it* makes with this value;
# it says nothing about a request some other page sends here. `same-origin` sends the full referrer
# within an app and nothing at all to any other origin -- see `web/app.py`'s own long-form
# discussion (#47) of why `no-referrer` looked safer and broke the app's own same-origin form posts.
REFERRER_POLICY = "same-origin"

# Nothing dynamic may be written to the browser's disk cache by default (#218); a caller carves out
# its own bundled, fingerprint-free assets through `is_bundled_asset` below, never by weakening this.
CACHE_CONTROL = "no-store"

# The strict baseline every surface starts from: same-origin only, images may be inline data URIs
# (both apps' CSS use them for small icons), no CDN, ever. `web/app.py` uses this unchanged.
# `api/app.py` widens exactly one directive for the reason stated at that call site -- never here,
# so a widening is never invisible to whoever reads the shared default.
DEFAULT_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def apply_security_headers(response, path: str, *, csp: str = DEFAULT_CSP,
                            is_bundled_asset: Callable[[str], bool] = lambda _path: False):
    """State one surface's header policy on one response -- the single definition every surface's
    middleware and unhandled-exception handler both call, so adding a header here reaches every
    caller without a second edit (the acceptance criterion #503 was filed to get).

    `setdefault`, not assignment, so a route that deliberately set its own value keeps it. Two
    parameters are per-surface on purpose rather than folded into this function: `is_bundled_asset`
    is a fact about a surface's own static mount (which paths carry nothing of the reader's and may
    stay cacheable), and `csp` lets a surface widen the baseline where its own served content needs
    it -- documented at that surface's own call site, never here, so the widening is never silent.
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", REFERRER_POLICY)
    response.headers.setdefault("Content-Security-Policy", csp)
    if not is_bundled_asset(path):
        response.headers.setdefault("Cache-Control", CACHE_CONTROL)
    return response
