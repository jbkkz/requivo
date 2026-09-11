"""Anthropic's published rates, and the one function that stamps them onto a call.

The vendor half of what #74 called `usage.py`. The neutral half — the ledger, the records, the
arithmetic — is `requivo.usage`; what stayed here is the only part that is genuinely a fact about
Anthropic: a dated table, its expiry-aware launch prices, and the lookup over them.

Kept as a module of its own rather than folded into `client.py` for the reason #74 gives for cutting
it out of the provider in the first place: it is the part edited on a *calendar*, on a schedule that
has nothing to do with the engine. A file whose whole content is two tables and a date is one a
maintainer can open, correct and close without reading a call loop.
"""

from __future__ import annotations

from datetime import date

from requivo.usage import CallRecord

# USD per 1M tokens (input, output). Read from Anthropic's published rates at
# https://platform.claude.com/docs/en/about-claude/pricing on 2026-08-29. This yields an *estimate*,
# never a bill: prices drift and intro rates lapse, so the renderer stamps this date and labels the
# number an estimate. Tokens are ground truth from the API; cost is the only thing here that can go
# stale — keep this table updateable and honest, not authoritative. Whoever edits it next: state
# where the rate was read and when, because the previous edit did not and the omission is what made
# the entry below unfalsifiable for a release.
PRICING_AS_OF = "2026-08-29"
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.00, 25.00),
    # 2.00/10.00 is the *standard* rate, not an intro one. It was carried here as 3.00/15.00 — Sonnet
    # 4.6's rate, inherited from the previous Sonnet generation rather than confirmed — behind a
    # launch row that expired 2026-08-31, so every cost line this product printed from September
    # would have over-reported by exactly 50%. Anthropic's pricing page now states the introductory
    # $2/$10 "is now the standard price" and that the scheduled increase "will not occur" (#254).
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Launch pricing that lapses on a known date: model → (input, output, last day inclusive). A dated
# table with no notion of expiry is wrong twice — it over-reports while an intro rate is live, then
# under-reports the day someone edits the rate in and forgets the lapse. Encoding the end date lets
# the estimate be right on both sides of it without another edit.
#
# Empty today, and deliberately kept: no model is on a published intro rate, and the mechanism is
# what makes the next one a one-line edit instead of a calendar reminder. Its guard does not depend
# on this table being populated — `test_launch_pricing_applies_until_it_lapses` pins the expiry
# semantics against a fixture model, because a mechanism test written against the live rate is a
# test the calendar can retire, which is how this table came to be believed.
_LAUNCH_PRICE_PER_MTOK: dict[str, tuple[float, float, str]] = {}


def price_per_mtok(model: str, on: date | None = None) -> tuple[float, float] | None:
    """The (input, output) USD rate per million tokens for `model` on a given day, or None when the
    model's price is unknown — never guess a price. `on` defaults to today, so a running estimate
    follows a launch rate over its expiry without a code change."""
    launch = _LAUNCH_PRICE_PER_MTOK.get(model)
    if launch is not None:
        in_rate, out_rate, until = launch
        if (on or date.today()) <= date.fromisoformat(until):
            return in_rate, out_rate
    return _PRICE_PER_MTOK.get(model)


def price_call(rec: CallRecord, on: date | None = None) -> CallRecord:
    """Stamp the rate this call was billed at onto the record, and return it.

    The rate is resolved *when the call is filed*, not when the total is rendered -- the ledger
    holds arithmetic over rates it was given rather than reaching back into the table above, so a
    second provider needs no registry, and an estimate spanning a price change is right on both sides
    of it (#167). Pinned by `test_a_call_is_priced_at_the_rate_in_force_when_it_was_made`.

    Both fields are set together or neither is, which is invariant 6 applied to a price: a record
    carrying a rate with no table date would print an estimate that reads exactly like a dated one.
    An unknown model leaves both None and `cost_usd()` returns None rather than guessing.

    **It re-stamps unconditionally, and that is deliberate rather than an oversight.** Called twice
    on one record, the second call wins. A first-write-wins guard was considered and rejected: the
    only caller is `_record` in `completion.py`, which runs exactly once per `CallRecord` because
    `_complete` builds one record and reaches one exit with it, so the guard could never fire and a
    guard that provably cannot fire is worse than none -- it reads as protection against a case
    nobody has. What this function means is *price this call at Anthropic's rates*, and that is a
    question with one answer, not an answer that depends on whether somebody asked before.
    """
    rate = price_per_mtok(rec.model, on)
    if rate is not None:
        rec.rate_per_mtok = rate
        rec.priced_as_of = PRICING_AS_OF
    return rec
