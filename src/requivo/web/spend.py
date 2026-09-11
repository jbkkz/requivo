"""Opening a usage ledger around a paid web request, and recording what it cost (#253).

`requivo.usage` records into whatever ledger is active on the current call stack and is an explicit
no-op when there is none. `cli.py` opened one around every command; nothing opened one for the Web,
so a provider call made through a browser was billed by Anthropic and recorded nowhere — not shown,
not logged, not recoverable after the fact. On a product whose own ordering says "Web is the product
experience, the CLI is infrastructure", cost observability existed only on the infrastructure.

Two outcomes per action, deliberately, because they answer different people:

* the **reader** who just spent the money sees the figure — directly on the response, for the
  answers turn and every document generation, or carried across the one hop that has no body of its
  own: `create_session` and `run_discovery` both answer `303`, and the small `_pending` store below
  (#253) stashes the figure server-side, keyed by slug, for the GET the redirect lands on to pop.
  Neither path puts the number on the URL, where it could be forged.
* the **operator** sees it always, in the terminal they started the server in, regardless of whether
  a reader's browser ever follows the redirect through to pick it up. That is also the only channel
  left when the call *fails* — the fragment (or the stash, on a failed first analysis) still carries
  it where there is a page to land on, but the log is what survives every case, including one this
  app cannot render to: a script that posts and never issues the follow-up GET.

**"Always" is load-bearing, and it was false for two releases.** This line is written at `INFO`,
`logging.lastResort` is fixed at `WARNING`, and nothing configured a handler for `requivo.web` — so
the operator's channel was not merely unformatted, it was empty: *no handler* printed exactly the
same as *nothing was spent*, which is the absence this project is careful about everywhere else.
`requivo web` attaches that handler now (`web/logging_setup.py`, #291). One mode is still exempt and
is named rather than glossed: under `--reload`, uvicorn's worker is a separate process that
re-imports the app without passing through the entry point, so this line is still dropped there.

The log line is written from a `finally`, which is what makes the second bullet true on the failure
path. It is the same rule `record_call` states one layer down: a failed call is still billed, so the
spend is recorded before the failure surfaces.

Pinned by `test_a_failed_paid_call_still_records_what_it_spent`.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from requivo.usage import track_usage
from requivo.web.viewmodels.usage import usage_view

# The same logger `app.py` uses, reached by name rather than by import so this module stays free of
# the app factory — the argument `security.py` makes for taking `render_error` as an argument.
logger = logging.getLogger("requivo.web")

# ── carrying a figure across the one hop that has no body of its own (#253) ────
#
# `create_session`/`run_discovery` answer `303`, which has no body for a figure -- a query parameter
# was rejected as a *forgeable* cost claim. Instead: a small in-memory, read-once-and-clear store
# keyed by slug, written from the `finally` below (even on a call that failed after spending) and
# popped by the GET the redirect lands on. Per-process, so a restart between the two loses it; the
# log line above is what survives that and every other case. Pinned by
# `test_a_first_analysis_lands_on_a_page_showing_what_it_spent` and
# `test_reloading_the_landing_page_does_not_repeat_the_spend_line`.
_lock = threading.Lock()
_pending: dict[str, dict] = {}


def stash_web_usage(slug: str, view: dict | None) -> None:
    """Remember one action's footprint against a slug, for the page its redirect lands on. A no-op
    when there is nothing to report — same silence `usage_view` itself returns for that case."""
    if view is None:
        return
    with _lock:
        _pending[slug] = view


def pop_web_usage(slug: str) -> dict | None:
    """The figure stashed for this slug, if any — removed in the same step it is read, so a later
    view of the same session (a reload, a later visit) reports nothing rather than repeating it."""
    with _lock:
        return _pending.pop(slug, None)


@contextmanager
def track_web_usage(surface: str, *, carry_to: str | None = None):
    """Scope a ledger over one paid web action, log its footprint when the action ends, and — when
    `carry_to` names a slug — stash the same figure for that slug's next GET to pick up.

    `surface` is the same string the route stamps on the revision's provenance, so a line in the
    terminal and a line in the session's history name the same operation. `carry_to` is only ever a
    slug the caller already owns (the session the redirect is about to send the reader to), never
    anything read off the request — the number is the server's own, so it stays trustworthy.
    """
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
