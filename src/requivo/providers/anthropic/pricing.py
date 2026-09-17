"""Anthropic's published rates and the one function that stamps them onto a call: the vendor half
of the ledger (`requivo.usage` is the neutral half), a file edited on a calendar.
"""

from __future__ import annotations

from datetime import date

from requivo.usage import CallRecord

# USD per 1M tokens (input, output), read from https://platform.claude.com/docs/en/about-claude/pricing
# on the date below: an estimate, never a bill. Whoever edits it next states where and when the rate was read.
PRICING_AS_OF = "2026-08-29"
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.00, 25.00),
    # The standard rate since the scheduled increase was cancelled (#254); it was carried as 3.00/15.00 for a release.
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Launch pricing that lapses on a known date: model → (input, output, last day inclusive). Empty
# today and kept, so the next intro rate is a one-line edit; `test_launch_pricing_applies_until_it_lapses`
# pins the expiry against a fixture model.
_LAUNCH_PRICE_PER_MTOK: dict[str, tuple[float, float, str]] = {}


def price_per_mtok(model: str, on: date | None = None) -> tuple[float, float] | None:
    """The (input, output) USD rate per million tokens for `model` on `on` (today by default), or
    None when unknown: never guess a price."""
    launch = _LAUNCH_PRICE_PER_MTOK.get(model)
    if launch is not None:
        in_rate, out_rate, until = launch
        if (on or date.today()) <= date.fromisoformat(until):
            return in_rate, out_rate
    return _PRICE_PER_MTOK.get(model)


def price_call(rec: CallRecord, on: date | None = None) -> CallRecord:
    """Stamp the rate this call was billed at onto the record, resolved when the call is filed, not
    when the total is rendered (#167, `test_a_call_is_priced_at_the_rate_in_force_when_it_was_made`).
    Both fields set together or neither (invariant 6). Re-stamps unconditionally: the one caller
    runs once per record."""
    rate = price_per_mtok(rec.model, on)
    if rate is not None:
        rec.rate_per_mtok = rate
        rec.priced_as_of = PRICING_AS_OF
    return rec
