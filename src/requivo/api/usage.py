"""What a paid API action cost -- the response's `usage` object (#425, slice 2).

A small, API-local reimplementation of `web/viewmodels/usage.py`'s `usage_view`, not an import of
it: the two are different surfaces behind different optional extras (`[web]` pulls in Jinja2 and the
rest of that package's `__init__`; `[api]` does not), and importing across them would trade the
dozen lines this duplicates for a dependency from one surface's package onto another's -- more
coupling than the duplication it would remove. `api/dependencies.py`'s `safe_slug` docstring makes
the identical call for the identical reason; see that module for the fuller argument.

The behaviour is deliberately identical, because the fact it states does not change with the
transport: **three states, because two of them are silences and they are not the same silence** --
a call was made and priced; a call was made and could not be priced (tokens exact, no cost --
invariant 6: a rate is stated or it is not guessed); or nothing to report (no call, or a call the
provider gave no usage figures for) -- `None`, which the routes serialize as `"usage": null` rather
than as a manufactured zero-valued object. The key is always present on a paid route's success
body, so a client can tell "this route never reports usage" from "this call had nothing to report"
without a schema in hand. Reported `cost` and `unpriced_reason` are mutually exclusive, matching the
CLI's `render_usage` and the Web's `usage_view` -- the Two vocabularies rule applied to money: the
arithmetic lives once, on `UsageLedger`, and every surface only ever relabels it.

**The failure path is the operator's, and it is logged from a `finally`.** A paid call that fails
*after* the provider answered -- a `RevisionConflictError` on the apply, a save that could not land
-- has already been billed (`record_call` fires before a clean failure surfaces), and the error
envelope `app.py` renders has no `usage` on it. `track_api_usage` writes the figure to the
`requivo.api` logger whether the action succeeded or not, the same contract `web/spend.py`'s
`track_web_usage` keeps for the browser and `cli.py`'s exit arms keep for the terminal; without it
the API was the one surface on which a billed, failed call left no trace anywhere (found in review
of #425 slice 2). Pinned by `test_a_failed_paid_call_still_logs_what_it_spent`.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from requivo.usage import UsageLedger, track_usage

logger = logging.getLogger("requivo.api")


@contextmanager
def track_api_usage(surface: str):
    """Scope a ledger over one paid API action and log its footprint when the action ends, on the
    failure path too. `surface` is the string the route stamps on the revision's provenance, so a
    line in the operator's terminal and a line in the session's history name the same operation."""
    with track_usage() as ledger:
        try:
            yield ledger
        finally:
            view = usage_view(ledger)
            if view is not None:
                cost = (f"est. ~${view['cost']}" if view["cost"] is not None
                        else view["unpriced_reason"])
                logger.info("%s spent %s tokens over %s call(s) -- %s",
                            surface, format(view["tokens"], ","), view["calls"], cost)


def usage_view(ledger: UsageLedger | None) -> dict | None:
    """One paid action's footprint, or `None` when there is nothing to say -- an offline route (or a
    ledger the provider reported no usage on) reports `"usage": null`, never a zero-valued
    object."""
    if ledger is None or not ledger.calls:
        return None
    processed = ledger.input_tokens + ledger.cache_read_tokens + ledger.cache_write_tokens
    tokens = processed + ledger.output_tokens
    if tokens == 0:
        return None
    cost = ledger.cost_usd()
    models = " · ".join(ledger.models)
    as_of = " · ".join(ledger.priced_as_of)
    return {
        "calls": len(ledger.calls),
        "tokens": tokens,
        "cached": ledger.cache_read_tokens,
        "model": models,
        "cost": None if cost is None else f"{cost:.3f}",
        "unpriced_reason": None if cost is not None else f"no price on file for {models}",
        "rates_as_of": as_of or None,
    }
