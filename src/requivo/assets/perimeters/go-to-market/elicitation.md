# The go-to-market perimeter

**Requivo's scope is the job, not the artifact type** (`decision: the-job-not-the-artifact-type`).
This perimeter serves a different decision structure than software scoping: not "what does the
system do", but "how does this reach its first users, and what would make us stop or change course".
The reasoning is the same engine, over a different slot vocabulary (#608, #609).

It exists because a go-to-market planning request was run once, scored against the software slot
set, and found wanting: four slots mapped cleanly, three partially, eight had no referent at all, and
the structure the request actually turned on -- objective, ICP, channels, capacity, decision
thresholds -- had no slot anywhere to hold it. This schema is that request's own structure, not a
whiteboard guess (`docs/decisions/0020-the-job-not-the-artifact-type.md`).

## Four pillars

The same four pillars as the software perimeter, re-read for this job:

| Pillar | Question | Slots |
|---|---|---|
| **Why** | What are we trying to achieve, and by when? | Objective · Success metric · Horizon |
| **What** | Who is this for, and what do they get? | ICP · Existing distribution · Offer |
| **How** | How does it reach them, and with what? | Channels · Unit economics · Capacity · Budget |
| **Validate** | How do we know, and when do we act on it? | Instrumentation · Decision thresholds |

## Each slot is the same small record

`completeness`, `confidence` (explicit / inferred / empty / testable), `impact`, `value`, `evidence`,
`test_plan` -- identical shape and identical meaning to the software perimeter (Core, not perimeter,
per `docs/decisions/0020-the-job-not-the-artifact-type.md`). `testable` is not an edge case here: "will
this channel convert" and "will anyone pay this price" are exactly the gaps a real test, not another
question, is what would settle.

## Capacity is the binding constraint

Of the twelve slots, `capacity` carries the highest default impact for a reason worth stating
plainly: the benchmark this perimeter is built from showed capacity being *asserted* rather than
received -- a plan sized to an ambition instead of to the hours actually available. A go-to-market
plan that does not fit its capacity is not a plan, whatever the budget or the channel list says, so
the engine treats an unconfirmed `capacity` as high-value to probe even when nothing else about the
request signals it.

## Existing distribution before a new channel

`existing_distribution` is asked before `channels` is trusted, not after: what audience, relationship
or surface already exists is usually the single biggest determinant of which channels are even
viable. A request that proposes a channel without saying what distribution already exists is
answering a question it has not yet been asked.

## What the engine produces

While discovery is open, the same per-pillar status and priority questions render as they do for the
software perimeter. This perimeter ships exactly one artifact, per #607's cost rule: `gtm_plan`
(`requivo gtm_plan <slug>`) — its equivalent of the decision brief, generated and written to
`go-to-market-plan.md`. It names a chosen set of actions rather than a ranking, states what it
deliberately excludes and the resource envelope it assumed, and carries its decision thresholds as
typed items — see `core/contracts.py`'s `GoToMarketPlan` and `render/markdown.py`'s
`gtm_plan_markdown` for the split between what it projects off the model and what it judges (#609).
