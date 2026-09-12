# The JSON retry pays full price under `reuse_system=False`

**Slug:** `retry-regression-under-reuse-system-false`

## Context

`_system_blocks` places a `cache_control` breakpoint on the system prompt only when the caller says
`reuse_system=True` — a per-*operation* decision (see the module docstring and #9), because a
one-shot generator writing a cache nothing reads pays a flat ~25% surcharge on the largest part of
its input for nothing.

Most generators are genuinely one-shot and correctly default to `reuse_system=False`. But `_complete`
retries a malformed or non-conformant JSON reply with a corrective nudge, re-sending the identical
system prompt a second time — and a one-shot generator that hits that retry path pays the write price
(1.25x) twice (2.0x total) instead of the write-then-read price caching would have bought it
(1.25 + 0.1 = 1.35x). That is a real, measurable regression on the retry path, and it is accepted
rather than fixed by flipping the default.

Nothing in `tests/` can go red for a probability threshold — it is arithmetic about a tradeoff, not a
behaviour the suite observes, which is `docs/decisions/README.md`'s third shape.

**Scope since #258.** The system prompt is two blocks now, and the arithmetic above applies to the
op-specific remainder only (~1-3k tokens): the shared schema + product-context block that opens
every prompt (~9k tokens with the bundled cards) carries a breakpoint on every call, so on a retry it
is a 0.1x cache read whatever `reuse_system` says. The threshold below is unchanged — it is a ratio,
and the block it is taken over is simply smaller — but the absolute cost of the regression is now a
fraction of what this record first described, which is the number to hold in mind when reading
"pays the write price twice".

## Decision

Keep `reuse_system=False` as the default for single-call generators, and accept the retry-path
regression. With `p` the probability that a given call retries, not caching wins over caching
whenever `1 + p < 1.25 + 0.1p`, i.e. `p < ~0.28`. A JSON contract violation from these generators —
the only thing that triggers this retry — is far rarer than roughly one call in four, so the expected
cost of never caching a one-shot call is lower than the expected cost of always caching it.

## What breaking it cost

Nothing shipped wrong — this is a forward cost analysis fixed at design time, not a regression found
in production. The number worth keeping is the threshold itself: if the retry rate on single-call
generators is ever measured near ~0.28 (a systematic prompt or contract problem, not occasional model
noise), this default stops being the right one and the tradeoff should be re-run against the real
rate rather than the illustrative one.

## Alternatives rejected

- **Cache only from the second attempt** (a plain first send, a cache write on the retry, if any).
  Expected cost `1 + 1.25p` against never-caching's `1 + p`: it is dominated at every `p > 0`, since
  the write on the retry buys a read nothing ever makes — the retry is the last send. It adds a
  second code path for no range of `p` where it is the best of the three options.
- **Default `reuse_system=True` everywhere.** Rejected as the regression this whole design avoids:
  every one-shot generator would pay the 25% write surcharge on every call, for a read that never
  happens on the overwhelming majority of them.
