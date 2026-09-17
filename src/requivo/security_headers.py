"""The security-header policy shared by every FastAPI surface (#503): defined once here rather than
in `web/app.py`, since `api/app.py` shipped with none. Framework-free (`setdefault` on any
Starlette response), reachable from a base install with neither extra.
"""

from __future__ import annotations

from collections.abc import Callable

# `same-origin`, never `no-referrer`, which nulls the `Origin` on the app's own form posts (#47).
REFERRER_POLICY = "same-origin"

# Nothing dynamic may be disk-cached by default (#218); a surface carves out its bundled assets via `is_bundled_asset`.
CACHE_CONTROL = "no-store"

# The strict baseline: same-origin only, inline data-URI images, no CDN. `api/app.py` widens one
# directive at its own call site, never here.
DEFAULT_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def apply_security_headers(response, path: str, *, csp: str = DEFAULT_CSP,
                            is_bundled_asset: Callable[[str], bool] = lambda _path: False):
    """State one surface's header policy on one response: the single definition its middleware and
    unhandled-exception handler both call. `setdefault`, so a route's own value stays; `csp` and
    `is_bundled_asset` are per-surface, documented at the call site."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", REFERRER_POLICY)
    response.headers.setdefault("Content-Security-Policy", csp)
    if not is_bundled_asset(path):
        response.headers.setdefault("Cache-Control", CACHE_CONTROL)
    return response
