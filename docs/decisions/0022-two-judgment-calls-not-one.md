# Route and ground with two calls, not one

**Slug:** `two-judgment-calls-not-one`

## Context

#601's router adds a perimeter judgment before a first discovery, beside #593's grounding judgment.
The issue offered — not mandated — riding an existing call "if you find a way that does not cost
accuracy": concretely, one call returning both verdicts.

## Decision

Keep them separate: two standalone calls (`build_standalone_prompt`, no shared prefix), each
validated against its own contract (`PerimeterJudgment`, `ContextJudgment`) and independently
skippable (an explicit `--perimeter`/`--context`, a single installed perimeter, a provider
implementing one protocol only). The price by path, asserted by
`test_a_first_discovery_still_reaches_the_provider_on_both_paths`,
`test_run_with_a_request_makes_the_same_call_count_as_discover`,
`test_stopping_early_keeps_the_turns_it_paid_for`,
`test_an_explicit_perimeter_flag_costs_no_routing_call` and
`test_an_ambiguous_router_verdict_refuses_before_any_model_is_reasoned_cli`:

| Path | Calls |
|---|---|
| No `--context`, no `--perimeter` (typical) | 3 — route, ground, turn |
| ...and grounding is `uncovered` (writes a missing card) | 4 |
| Explicit `--perimeter`, no `--context` | 2 — ground, turn |
| Explicit `--perimeter` and `--context` | 1 — turn |
| Ambiguous perimeter verdict | 1 — refuses (or asks) before grounding or the turn |
| One installed perimeter, or no `PerimeterJudge` | as an explicit `--perimeter` |

A new judgment adds a row; the table names paths rather than a total in prose (#290).

## What breaking it cost

Never built, so no incident. A combined reply's retry loop would reconcile two disagreeing nudges —
an unknown card on one axis, an unknown perimeter on the other — the drift #266 measured once: green
offline, up to three calls per invocation. Two separately validated replies cannot do that.

## Alternatives rejected

- **One call, two verdicts** — rejected on accuracy: two different questions stay sharper in two
  focused prompts, and the saving is one call on a subset of paths.
- **Judge the perimeter inside the discovery turn** — circular: the turn needs the perimeter's schema
  to know what it fills (as `decision: the-engine-writes-the-missing-card` found for grounding).
- **Route only when grounding is `uncovered`** — the questions are independent; a go-to-market
  request can ground `none` and still need routing away from software.
