"""What a run spent: a provider-neutral ledger of API calls, tokens, latency and cost, beside
`paths.py` and `streams.py` (#167). Cost is arithmetic here and nowhere else: a `CallRecord` carries
the rate it was billed at, so the estimate is right across a price change. Three states: priced,
unpriced (`rate_per_mtok is None`, `cost_usd()` refuses to guess), and priced with no provenance
(`priced_as_of is None`). `SpendPolicy` (#427) is the one non-renderer consumer of `cost_usd()`.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field

from requivo.core.errors import SpendCeilingReachedError


@dataclass
class CallRecord:
    """One provider call's usage, summed across its retry attempts. `rate_per_mtok` and
    `priced_as_of` are provenance stamped by the provider; both absent means unpriced (invariant 6)."""
    model: str
    input_tokens: int = 0        # uncached, full-price input
    output_tokens: int = 0
    cache_read_tokens: int = 0   # served from cache (~0.1x input price)
    cache_write_tokens: int = 0  # written to cache (~1.25x input price)
    latency_ms: int = 0
    attempts: int = 1
    rate_per_mtok: tuple[float, float] | None = None
    priced_as_of: str | None = None
    # Which verb spent this call, in `_OP_PROMPTS`'s vocabulary (#435); `None` for a record that
    # predates the field (`test_call_record_operation_defaults_to_none`).
    operation: str | None = None


@dataclass
class UsageLedger:
    """Accumulates the API usage of one `requivo` command; presentation-free, and the cost estimate
    is pure arithmetic over records that brought their own rate."""
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, rec: CallRecord) -> None:
        self.calls.append(rec)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def cache_read_tokens(self) -> int:
        return sum(c.cache_read_tokens for c in self.calls)

    @property
    def cache_write_tokens(self) -> int:
        return sum(c.cache_write_tokens for c in self.calls)

    @property
    def latency_ms(self) -> int:
        return sum(c.latency_ms for c in self.calls)

    @property
    def models(self) -> list[str]:
        seen: list[str] = []
        for c in self.calls:
            if c.model not in seen:
                seen.append(c.model)
        return seen

    @property
    def priced_as_of(self) -> list[str]:
        """The distinct rate-table dates behind this ledger's cost, in order first seen; empty when nothing was priced."""
        seen: list[str] = []
        for c in self.calls:
            if c.priced_as_of is not None and c.priced_as_of not in seen:
                seen.append(c.priced_as_of)
        return seen

    def cost_usd(self) -> float | None:
        """Estimated USD across all calls, or None if any call went unpriced. Cache reads bill ~0.1x
        the input rate, cache writes ~1.25x; the rates come off the records."""
        total = 0.0
        for c in self.calls:
            if c.rate_per_mtok is None:
                return None
            in_rate, out_rate = c.rate_per_mtok
            total += (c.input_tokens * in_rate
                      + c.cache_read_tokens * in_rate * 0.1
                      + c.cache_write_tokens * in_rate * 1.25
                      + c.output_tokens * out_rate) / 1_000_000
        return total


@dataclass
class SpendPolicy:
    """An optional ceiling on what one operation may spend, in estimated USD (#427), consulted by
    `DiscoveryService` before every provider call; no policy is a no-op
    (`test_default_no_policy_is_byte_identical_to_before_this_existed`). USD over tokens, since a
    token ceiling needs one number per model; reuses `cost_usd()`, refusal to guess included."""

    ceiling_usd: float

    def check(self, ledger: UsageLedger | None) -> None:
        """Raise `SpendCeilingReachedError` if `ledger`'s estimate is at or past the ceiling. `None`
        (no `track_usage()` scope open) proceeds uncounted: a budget cannot be enforced unmeasured."""
        if ledger is None:
            return
        cost = ledger.cost_usd()
        if cost is None:
            raise SpendCeilingReachedError(
                f"Spend ceiling of ${self.ceiling_usd:,.2f} cannot be verified: this operation's "
                "ledger already holds a call with no rate on file, so its true cost is unknown "
                "rather than zero. Refusing rather than letting an unpriced call spend past a "
                "ceiling nobody can see.",
                details={"ceiling_usd": self.ceiling_usd, "spent_usd": None,
                         "calls": len(ledger.calls), "reason": "unpriced_call"})
        if cost >= self.ceiling_usd:
            raise SpendCeilingReachedError(
                f"Spend ceiling of ${self.ceiling_usd:,.2f} reached (est. ${cost:,.2f} already "
                f"spent over {len(ledger.calls)} call(s)); refusing before this call is made.",
                details={"ceiling_usd": self.ceiling_usd, "spent_usd": cost,
                         "calls": len(ledger.calls), "reason": "ceiling_reached"})


# Session-scoped ledger: a ContextVar, isolated per call stack; a provider records only if one is active.
_LEDGER: contextvars.ContextVar[UsageLedger | None] = contextvars.ContextVar("usage_ledger", default=None)


@contextmanager
def track_usage():
    """Scope a UsageLedger over a block; with none active, `record_call` is a no-op."""
    ledger = UsageLedger()
    token = _LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _LEDGER.reset(token)


def record_call(rec: CallRecord) -> None:
    """File one call against the active ledger, if there is one. A provider records the spend before
    it surfaces a failure: `test_a_failed_call_is_still_recorded_on_every_exit`."""
    ledger = _LEDGER.get()
    if ledger is not None:
        ledger.record(rec)


def current_ledger() -> UsageLedger | None:
    """The ledger active on this call stack, or `None` when no `track_usage()` scope is open (#292):
    "nothing to report", never "spent nothing" (invariant 6,
    `test_a_provider_call_made_with_no_active_ledger_still_leaves_usage_absent`)."""
    return _LEDGER.get()
