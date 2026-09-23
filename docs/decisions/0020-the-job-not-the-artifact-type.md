# The job, not the artifact type

**Slug:** `the-job-not-the-artifact-type`

> Written ahead of its machinery (#607). Since landed: the perimeter mechanism with go-to-market as
> its second instance (#608), go-to-market's one artifact (#609), and the router that picks a
> perimeter (#601). Not built: a data/analytics perimeter (#612), which still lacks its instance.

## Context

Requivo modelled one decision structure — slots for scoping software: actors, business objects,
rules, workflow, permissions, integrations. The README's audience (solutions engineers, consultants,
technical PMs, agency leads) was already wider. A go-to-market request scored by hand against the
software slots: four mapped, three partially, **eight had no referent**, and what the request turned
on — objective, ICP, channels, capacity, decision thresholds — had no slot at all. That measurement
is reported in #605 and #607, not committed here: evidence enough to reopen a position. The framing
it arrived in: *"I have a half-formed idea… what must I not forget before diving in headfirst?"* —
the trigger is an urge rather than a client's text, the promise is recall (what
`uncertainty × impact` already computes), and the value lands before commitment.

## Decision

**Requivo's scope is the job, not the artifact type**: several **perimeters** over one engine, on
the claim *the decision structure varies; the reasoning about decisions does not.*

- **A perimeter owns** its slot schema, its elicitation spec, discovery guidance specific to it (a
  software heuristic like "primary objects first" must never reach another perimeter), and one
  artifact to begin with — a second only when a user asks.
- **The Core keeps**, identical for every perimeter: the driver; `Slot`, confidence, impact,
  evidence, revisions, provenance; the reasoning items; `propagate()`, `diff_models()`, staleness,
  `impact`; the store, lock, atomic claim and integrity.
- **The set stays finite**: a new perimeter needs one real recorded request it demonstrably cannot
  model. Go-to-market has one; data/analytics (#612) does not yet.
- **The accepted cost per perimeter**: a schema, a spec, one artifact and a golden baseline. A
  perimeter without a baseline has not shipped — it can only be guessed at.

Targeting, pricing and the hosted service belong to the private repository, not here.

## What breaking it cost

No incident: no second perimeter had shipped. Staying narrow spends its cost on every request from
the audience the README claims. The failure to watch: a perimeter that copies software's structure
instead of one measured from a real request, or one that costs what a standalone build would.

## Alternatives rejected

- **Declare non-software requests out of scope** (the audit's own recommendation) — narrower than
  the stated audience; the product sells help with a job, not one document type.
- **One generic schema for every job** — dissolves the boundary: nowhere for perimeter-specific
  heuristics, no clean edge for the router (#601), a vocabulary vague enough to fit nothing well.
- **Perimeters from a whiteboard** — the real-instance bar, applied as it is to guard tiers and
  relevance routing.
- **A third perimeter on schedule** — #612 is a checkpoint instead: if it costs what the second did,
  the mechanism generalised; if it costs a standalone build, fewer perimeters done properly.
