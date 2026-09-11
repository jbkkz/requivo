"""What a paid web action cost, translated for the screen it appears on.

A projection over `requivo.usage.UsageLedger` and nothing more. It relabels and formats; it does not
compute — the cost arithmetic lives on the ledger, and asking for a number a second way here is how
two surfaces come to disagree about one call (#253). The CLI prints the same figures through
`render_usage`, and the split between what is exact and what is an estimate is identical on both.

**Three states, because two of them are silences and they are not the same silence.** A call made
and priced states tokens and a labelled cost; a call made and unpriceable states tokens exact with
*no number at all* rather than guessing (invariant 6); nothing to report renders as `None`, never as
"0 tokens, est. $0.000" -- a value nobody could read must not print as a value read and found to be
zero. Pinned by `test_an_unpriced_call_says_so_rather_than_guessing` and
`test_a_call_the_provider_reported_no_usage_for_says_nothing_rather_than_zero`.
"""

from __future__ import annotations

from requivo.usage import UsageLedger


def usage_view(ledger: UsageLedger | None) -> dict | None:
    """One paid action's footprint, or `None` when there is nothing to say.

    The caller passes the ledger it opened around the action; an offline route passes nothing and
    gets nothing, which is what keeps the line off pages that made no call.

    Pinned by `test_an_answers_turn_says_what_it_spent`, `test_an_offline_page_reports_no_spend`,
    `test_a_call_the_provider_reported_no_usage_for_says_nothing_rather_than_zero` and
    `test_an_unpriced_call_says_so_rather_than_guessing`.
    """
    if ledger is None or not ledger.calls:
        return None
    processed = ledger.input_tokens + ledger.cache_read_tokens + ledger.cache_write_tokens
    tokens = processed + ledger.output_tokens
    if tokens == 0:
        # A call the provider reported no usage for. `render_usage` takes the same early return, and
        # for the same reason: this is "we could not measure", not "it was free".
        return None

    cost = ledger.cost_usd()
    models = " · ".join(ledger.models)
    # The rate date comes off the ledger rather than a vendor constant, so this module names no
    # provider — the leak #167 closed. Empty is the third state within the priced one: an estimate
    # printed with no rate date must not read like one printed with a current date.
    as_of = " · ".join(ledger.priced_as_of)
    return {
        "calls": len(ledger.calls),
        "tokens": tokens,
        "cached": ledger.cache_read_tokens,
        "model": models,
        # Formatted to the same three places the CLI uses, so the two surfaces state one number.
        "cost": None if cost is None else f"{cost:.3f}",
        "unpriced_reason": None if cost is not None else f"no price on file for {models}",
        "rates_as_of": as_of or None,
    }
