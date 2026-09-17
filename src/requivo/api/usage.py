"""What a paid API action cost: the response's `usage` object (#425), an API-local twin of
`web/viewmodels/usage.py` rather than an import across two optional extras. Three states: priced;
unpriced (tokens exact, no cost, invariant 6); nothing to report (`"usage": null`, never a zero).
The failure path is logged from a `finally`: `test_a_failed_paid_call_still_logs_what_it_spent`.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from requivo.usage import UsageLedger, track_usage

logger = logging.getLogger("requivo.api")


@contextmanager
def track_api_usage(surface: str):
    """Scope a ledger over one paid API action and log its footprint when it ends, on failure too;
    `surface` is the string the route stamps on provenance."""
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
    """One paid action's footprint, or `None` when there is nothing to say."""
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
