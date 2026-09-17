"""What a paid web action cost, for the screen: a projection over `UsageLedger` that computes
nothing (#253). Three states: priced; unpriced, tokens exact and no number (invariant 6); nothing
to report, `None`, never "0 tokens". `test_an_unpriced_call_says_so_rather_than_guessing`,
`test_a_call_the_provider_reported_no_usage_for_says_nothing_rather_than_zero`.
"""

from __future__ import annotations

from requivo.usage import UsageLedger


def usage_view(ledger: UsageLedger | None) -> dict | None:
    """One paid action's footprint, or `None` when there is nothing to say; an offline route passes nothing."""
    if ledger is None or not ledger.calls:
        return None
    processed = ledger.input_tokens + ledger.cache_read_tokens + ledger.cache_write_tokens
    tokens = processed + ledger.output_tokens
    if tokens == 0:
        # "We could not measure", not "it was free"; `render_usage` takes the same early return.
        return None

    cost = ledger.cost_usd()
    models = " · ".join(ledger.models)
    # The rate date comes off the ledger, never a vendor constant (#167); empty is the third state.
    as_of = " · ".join(ledger.priced_as_of)
    return {
        "calls": len(ledger.calls),
        "tokens": tokens,
        "cached": ledger.cache_read_tokens,
        "model": models,
        # The same three places the CLI uses.
        "cost": None if cost is None else f"{cost:.3f}",
        "unpriced_reason": None if cost is not None else f"no price on file for {models}",
        "rates_as_of": as_of or None,
    }
