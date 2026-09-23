# The JSON retry pays full price under `reuse_system=False`

**Slug:** `retry-regression-under-reuse-system-false`

## Context

`_system_blocks` puts a `cache_control` breakpoint on the op-specific system block only when the
caller says `reuse_system=True` — a per-operation choice (#9), because a one-shot generator writing a
cache nothing reads pays a flat ~25% surcharge. But `_complete` retries malformed JSON with a
corrective nudge, re-sending the same block: a one-shot call that retries pays the write price twice
(2.0x) where caching would have cost 1.25 + 0.1 = 1.35x. Since #258 this applies to the op-specific
remainder only (~1-3k tokens); the shared ~9k-token head is always cached, so a retry reads it at
0.1x. The threshold is arithmetic, not a behaviour a test can observe.

## Decision

Keep `reuse_system=False` for single-call generators and accept the retry-path regression. With `p`
the retry probability, not caching wins while `1 + p < 1.25 + 0.1p`, i.e. `p < ~0.28`; a JSON
contract violation is far rarer than one call in four.

## What breaking it cost

Nothing shipped wrong: a forward cost analysis. If the single-call retry rate is ever measured near
~0.28 — a systematic prompt or contract problem, not noise — re-run the tradeoff on the real rate.

## Alternatives rejected

- **Cache only from the second attempt** — costs `1 + 1.25p`, dominated at every `p > 0`: the retry
  is the last send, so its write buys a read nobody makes.
- **`reuse_system=True` everywhere** — every one-shot call pays the 25% write surcharge for a read
  that almost never happens.
