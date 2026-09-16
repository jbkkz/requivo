# Route and ground with two calls, not one

**Slug:** `two-judgment-calls-not-one`

## Context

#601's router adds a perimeter-routing judgment ahead of a first discovery's turn, alongside #593's
own grounding judgment. The issue's own cost section named the option of riding an existing call
rather than paying for a new standalone one, "if you find a way that does not cost accuracy" --
offered, not mandated. The concrete way to do that was to merge the two: one standalone call, one
reply carrying both a perimeter verdict and a grounding verdict, paid once rather than twice.

## Decision

Keep them separate. Two standalone calls (`build_standalone_prompt`, neither carrying the shared
schema/context prefix), each validated against its own contract (`PerimeterJudgment`,
`ContextJudgment`), each independently skippable (an explicit `--perimeter`/`--context`, a single
installed perimeter, a provider implementing only one of the two protocols).

The price this sets, by path -- asserted, not described
(`test_a_first_discovery_still_reaches_the_provider_on_both_paths`,
`test_run_with_a_request_makes_the_same_call_count_as_discover`,
`test_stopping_early_keeps_the_turns_it_paid_for`,
`test_an_explicit_perimeter_flag_costs_no_routing_call`,
`test_an_ambiguous_router_verdict_refuses_before_any_model_is_reasoned_cli`):

| Path | Calls |
|---|---|
| No `--context`, no `--perimeter` (typical) | 3 — route, ground, turn |
| ...and grounding is `uncovered` (writes a missing card) | 4 |
| Explicit `--perimeter`, no `--context` | 2 — ground, turn |
| Explicit `--perimeter` and `--context` | 1 — turn |
| Ambiguous perimeter verdict | 1 — refuses (or, interactively, asks) before grounding or the turn |
| One installed perimeter, or no `PerimeterJudge` | same as an explicit `--perimeter` |

A third judgment (a third perimeter needs none of this to change; a genuinely new *question* would)
adds a row to this table rather than changing what it already says -- the reason this is a table
naming paths, not a number in prose (`CLAUDE.md`'s own rule, and #290's lesson one layer up: a total
that was right the day it was written and silently wrong the day the next request landed).

## What breaking it cost

Not yet broken — merging is an alternative that was never built rather than one that was built and
reverted, so there is no incident to report. What is on record is why it stopped being tempting: a
combined reply keeps the "installed"/"uncovered"/"none" and "fits"/"ambiguous"/"none" verdicts one
validator's job, and the moment that reply's own JSON-retry loop has to reconcile two disagreeing
corrective nudges — an unknown card name on one axis, an unknown perimeter id on another — is the
exact failure `#266` already measured once for a single-purpose reply: a drift invisible in the
offline suite, paid for at up to three calls per invocation instead of one. Two independently
validated replies cannot develop that failure into each other.

## Alternatives rejected

- **One call, two verdicts in one reply.** The alternative above. Rejected on accuracy, not cost: two
  genuinely different questions (which vocabulary fits the request vs. which installed cards ground
  it) are easier to keep sharp in two focused prompts than one prompt asked to answer both, and the
  saving is one call on a subset of paths — an explicit `--perimeter` or `--context` already skips
  the call that alternative would have folded in.
- **Judge perimeter inside the discovery turn itself (`analyze`).** Circular, the same reason
  `decision: the-engine-writes-the-missing-card` rejected it for grounding: the turn that builds the
  model needs the perimeter's schema to know what it is filling, so the turn cannot be the one call
  that decides which schema that is.
- **Route only when grounding says `uncovered`**, deferring the perimeter question behind the
  cheaper verdict. Rejected: the two questions are independent — a go-to-market request can ground
  as `none` or `installed` and still need routing away from software — so gating one behind the
  other would silently skip routing for the ordinary case it exists to catch.
