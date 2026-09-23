# Deferring the provider-neutral extraction out of `providers/anthropic`

**Slug:** `deferring-the-neutral-provider-layer`

## Context

`providers/anthropic` holds two things. The transport — SDK client, `APIError` mapping,
`cache_control`, usage reads, rate tables — and vendor-free orchestration: the message builders,
`_GENERATORS`/`_OP_PROMPTS`, `prompt_version()`, JSON extraction, contract validation, the retry
loop and the parse-first truncation policy. #273 proposed extracting the neutral half so a second
provider inherits it. Re-measured on 2026-08-31 it was ~400 lines (the audit said 350, taken when
`completion.py` was 206 lines rather than 335): the neutral layer grows with every generator and
every hardening of the retry path, silently.

## Decision

**Defer the extraction, correct the sentences that oversold the seam, and write down the trigger.**
The roadmap lists "no second provider"; splitting now buys a capability against demand the project
has decided not to serve. Either of these reopens #273 as an implementation issue:

- the first concrete request for a second provider; or
- the first generator added after which the duplication cost is **measured** rather than estimated.

`services/discovery.py` and `docs/providers.md` said a second provider "is a constructor argument".
True of the **protocol** (`DiscoveryService` takes a `ReasoningProvider` and nothing else, held by
`tests/test_source_form.py`); false of the **build cost**. Both now state the two halves separately.

## What breaking it cost

Nothing yet. The overstated sentences went four releases unchallenged because nobody had paid the
cost they misdescribed. If deferred again, re-measure rather than quote the figure above.

## Alternatives rejected

- **Extract now.** Sound design, zero demand; the fake-client tests would pass with import-path
  changes only, so the seam can be taken later at the same price.
- **Close #273 as wontfix.** Loses the measurement and the trigger; the next audit re-derives both.
- **Defer silently.** An unstated deferral is indistinguishable from nobody having looked, and the
  sentences would keep overselling to the contributor most likely to act on them.
- **A guard on the neutral share.** Needs a hand-kept list of neutral regions, and there is one
  instance of the drift, not the two a guard tier requires.
