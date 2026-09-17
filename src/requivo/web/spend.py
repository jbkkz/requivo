"""Opening a usage ledger around a paid web request and recording what it cost (#253): the reader
sees the figure on the response, or stashed server-side across a `303` for the next GET to pop; the
operator sees it always, in the terminal, from a `finally`, so a failed call is recorded too
(`test_a_failed_paid_call_still_records_what_it_spent`). Under `--reload` uvicorn's worker never
passes through the entry point and the line is still dropped there.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from requivo.usage import track_usage
from requivo.web.viewmodels.usage import usage_view

# The same logger `app.py` uses, reached by name so this module stays free of the app factory.
logger = logging.getLogger("requivo.web")

# ── carrying a figure across the one hop that has no body of its own (#253) ────
# A query parameter is a forgeable cost claim, so: an in-memory, read-once store keyed by slug,
# per-process. Two tabs on one slug race like any flash store, harmlessly.
# `test_a_first_analysis_lands_on_a_page_showing_what_it_spent`,
# `test_reloading_the_landing_page_does_not_repeat_the_spend_line`.
_lock = threading.Lock()
_pending: dict[str, dict] = {}


def stash_web_usage(slug: str, view: dict | None) -> None:
    """Remember one action's footprint against a slug for the page its redirect lands on; a no-op when there is nothing."""
    if view is None:
        return
    with _lock:
        _pending[slug] = view


def pop_web_usage(slug: str) -> dict | None:
    """The figure stashed for this slug, removed as it is read, so a reload repeats nothing."""
    with _lock:
        return _pending.pop(slug, None)


@contextmanager
def track_web_usage(surface: str, *, carry_to: str | None = None):
    """Scope a ledger over one paid web action, log its footprint when it ends, and stash the figure
    for `carry_to`'s next GET. `surface` is the string the route stamps on provenance; `carry_to` is
    always a slug the caller owns, never read off the request."""
    with track_usage() as ledger:
        try:
            yield ledger
        finally:
            view = usage_view(ledger)
            if view is not None:
                cost = (f"est. ~${view['cost']}" if view["cost"] is not None
                        else view["unpriced_reason"])
                logger.info("%s spent %s tokens over %s call(s) — %s",
                            surface, format(view["tokens"], ","), view["calls"], cost)
                if carry_to is not None:
                    stash_web_usage(carry_to, view)
