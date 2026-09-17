# Deferring the provider-neutral extraction out of `providers/anthropic`

**Slug:** `deferring-the-neutral-provider-layer`

## Context

`providers/anthropic` holds two things that are not the same thing. One is the transport: the SDK
client, the `APIError` mapping, the `cache_control` placement, the usage-field reads and the dated
rate tables. The other is orchestration that has nothing to do with any vendor: the per-operation
user-message builders, the `_GENERATORS` / `_OP_PROMPTS` tables, `prompt_version()`, the JSON
extraction and fence-stripping, the contract validation, the corrective-nudge retry loop and the
parse-first truncation policy. The second is packaged inside the first only because that is where it
was first written.

#273 proposed extracting the neutral half into a provider-agnostic module so a second provider would
inherit it rather than duplicate it, and measured the neutral share at roughly 350 lines.

**Re-measured on 2026-08-31**, at the commit this record lands against, because the figure had
already moved: `providers/anthropic` is 1,057 lines across six modules. `generators.py` (246) imports
nothing from the SDK at all -- its only vendor coupling is the call to `_complete`. Of
`completion.py`'s 335, the SDK-touching region is the `client.messages.create` call with its
`APIError` and `TypeError` arms, the usage-field reads and the `stop_reason` check -- about 42 lines
-- alongside `_response_text`, `_system_blocks` and `_transport_message`; the failed-reply debug
writer, the JSON extraction and the retry/validation/truncation loop around the call are neutral, and
come to roughly 160. So **about 400 lines**, not 350.

That growth is the interesting part of the measurement rather than a correction to it. The audit's
350 was taken when `completion.py` was 206 lines; it is 335 now. The neutral layer packaged under a
vendor's name grows with every generator and every hardening of the retry path, and nothing announces
that it has.

## Decision

**Defer the extraction. Correct the two sentences that overstate what the seam already buys, and
write down the trigger.**

The extraction exists to make a second provider cheap. The repository's own roadmap lists "no second
provider" among the things not to do, and that decision has a written trigger which has not been
reached. Doing the split now buys a capability against a demand the project has explicitly decided
not to serve yet -- the same speculative-generality trade this codebase refuses elsewhere. One
context card colliding with its neighbour did not justify automatic relevance routing; one plausible
drift does not justify a new guard tier (#288). A first instance does not fund the work.

**The trigger, written so it is testable rather than a feeling.** Either of these reopens #273 as an
implementation issue:

- the first concrete request for a second provider; or
- the first generator added *after* which the neutral layer's duplication cost is **measured** rather
  than estimated.

**What the deferral owes, and what was done instead of the refactor.** Two sentences told a reader
something false about the cost, not about the protocol:

- `services/discovery.py`, module prose: *"swapping in a second provider is a constructor argument"*
- `docs/providers.md`: *"`DiscoveryService` talks to the protocol and nothing else, so a second
  provider is a constructor argument"*

Both are true of the **protocol** -- `DiscoveryService` really does take a `ReasoningProvider` and
nothing else, and `tests/test_source_form.py` keeps that honest from both ends. Both are false of the
**build cost**: a contributor reads them and plans for an afternoon, then finds that a working second
provider means re-implementing or copying about 400 lines that have nothing to do with their vendor.
Both now say the protocol half and the cost half separately, and point here.

## What breaking it cost

Nothing has broken yet, and this record exists so that the deferral is a decision with a stated
trigger rather than an issue that was never picked up. Two costs are already real and are the reason
it is written down rather than left implicit:

- **The overstated sentences.** Nobody has yet abandoned a second-provider contribution over them,
  because nobody has attempted one -- which is exactly why the claim went four releases unchallenged.
  A claim about a cost is only tested by somebody paying it.
- **The neutral layer grew 50% between the audit's measurement and this record**, over two days, with
  nothing reporting it. If the split is deferred again, re-measure rather than quoting this
  paragraph; a number in prose is right on the day it is written.

## Alternatives rejected

- **Do the extraction now.** Rejected on demand, not on merit: the design in #273 is sound and its
  own acceptance criteria sanction this path explicitly. There is zero user demand today, the
  existing fake-client tests would pass with import-path changes only (which is itself the evidence
  the seam is already there and can be taken later at the same price), and a refactor of the one
  module every paid call goes through carries a risk that buys nothing until somebody wants a second
  provider.
- **Close #273 as wontfix.** Rejected: the finding is correct and the cost is real. Closing it would
  lose the measurement and the trigger, and the next audit would re-derive both from scratch -- which
  is the failure this whole directory exists to prevent.
- **Leave the two sentences alone and defer silently.** Rejected, and this is the half that made the
  deferral worth any work at all. An unstated deferral is indistinguishable from nobody having looked,
  and the sentences would go on quietly overselling a seam to exactly the contributor most likely to
  act on them.
- **Add a guard that fails when the neutral share crosses a threshold.** Considered, because the
  growth is the trigger's own evidence. Rejected under this repository's meta-guard budget: a
  line-count classifier over `providers/anthropic` needs a hand-maintained list of which regions are
  neutral, which is the same by-hand judgement moved into test code, and CLAUDE.md asks for two named
  instances of the drift before funding a guard tier. There is one.
