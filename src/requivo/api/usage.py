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
provider gave no usage figures for) -- `None`, so the field is absent from the response rather than
a manufactured zero. Reported `cost` and `unpriced_reason` are mutually exclusive, matching the CLI's
`render_usage` and the Web's `usage_view` -- the Two vocabularies rule applied to money: the
arithmetic lives once, on `UsageLedger`, and every surface only ever relabels it.
"""

from __future__ import annotations

from requivo.usage import UsageLedger


def usage_view(ledger: UsageLedger | None) -> dict | None:
    """One paid action's footprint, or `None` when there is nothing to say -- an offline route (or a
    ledger the provider reported no usage on) passes through as no `usage` key at all, never a
    zero-valued one."""
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
