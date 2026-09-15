# Confidence stays one axis

**Slug:** `confidence-stays-one-axis`

## Context

#610 and #611 both come from the same shift: Requivo's target user is now as often a builder with a
half-formed idea as a PM relaying someone else's request. `Confidence` (`explicit` / `inferred` /
`empty`) was built for the second case, and it breaks in two distinct ways for the first:

- **#610.** `empty` conflates two unknowns with opposite remedies — *nobody has told me, and someone
  knows* (ask; blocks readiness, correctly) and *nobody knows until something is tried* (test; must
  not block readiness). The second is not an edge case for a builder's idea; it is the default shape
  of one — a go-to-market bet, whether anyone wants the feature. Collapsing it into `empty` means a
  session for this persona either never reaches `ready`, or reaches it because someone graded a guess
  `inferred` to force it through.
- **#611.** `explicit` means "the client said so" — a third party committed to the fact. With no
  client, the engine's only available reading is "the requester said so", which grades everything the
  user types `explicit` and turns readiness into a measure of how much they typed rather than what is
  actually settled.

Both issues asked the same open question, on purpose: is this one axis (`Confidence`, widened) or two
(*where a fact came from*, crossed with *whether it is obtainable at all*)? Answering them apart was
flagged as the way a three-value enum grows to six values and no thesis.

## Decision

**One axis. `Confidence` gains a fourth value, `testable`, and `explicit` is redefined rather than
joined by a second field.**

`testable` means: unknown, and not answerable by asking — only a real test would settle it. A slot
graded `testable` must carry `test_plan` (what would settle it) or it is refused, the same rule a
`Challenge`'s five required parts already enforce. It does not block readiness, because "precise
enough to build from" is compatible with a named, deliberately deferred test in a way an unasked
question is not. Settling one is an ordinary confidence change and propagates through `diff_models` /
`propagate()` like any other — no new machinery was needed for that half of #610, because a value
change on an existing field is already the thing those functions watch for.

`explicit` keeps its name and its place in the enum, but its meaning tightens: it requires an
*authority* for the fact, not just confidence in stating it. A third party's word always qualifies. A
solo builder's own **intent** — what they want, will build, will spend — is also an authority on
itself, so it can still be `explicit`. A solo builder's **belief about the world** — will users want
this, will they pay — is not, however plainly it is stated; it is `inferred` (an assumption to
confirm) or `testable` (below), the same way a fact read out of a repository is `inferred`, never
`explicit`, because it is the artifact speaking and not the client.

**Why this collapses to one axis rather than two.** `testable` is not a second, orthogonal dimension
bolted onto the existing three — it is a fourth *way of knowing*, sitting in the same question the
other three already answer ("how do we know this, or how will we"): stated by an authority
(`explicit`), deduced (`inferred`), unknown and askable (`empty`), or unknown and only settleable by a
test (`testable`). And the #611 half needs no new value or field at all: it is a grading *rule* over
the existing three values — who counts as an authority for a given fact — stated once, in
`framework/model_schema.json`'s `confidence` block (part of `{{SCHEMA}}` on every call) and restated in
`engine.md`, so the two cannot silently drift apart. Reusing `testable` as the correct landing zone for
an untested world-belief is what makes the two issues one change rather than two: a self-asserted
belief about the world that is worth a real test is exactly `testable`'s shape, named and deferred
rather than either mis-graded `explicit` or quietly dropped.

`PersistedSlot.confidence` is widened to `Confidence | str` (a value this build does not define
survives a round trip instead of raising, and is never treated as `explicit`) — invariant 8 applied to
an enum, not only to unknown keys.

## What breaking it cost

Neither half has shipped before this change, so there is no incident on record — inventing one would
be dishonest. What is worth naming, because it is the shape a future failure would take: **the #611
half is enforced by a prompt rule, backed by nothing in `core/` that can independently verify it.**
Requivo's architecture reasons in one LLM call per turn, whose intelligence lives in prompt assets, not
in Python (`CLAUDE.md`) — there is no way for `core/` to know, from a slot's text alone, whether a
stated fact is the requester's own intent or their guess about the world. What the type system *does*
guarantee, regardless of how that judgment goes: neither `inferred` nor `testable` is ever read as
`explicit`, so a model built entirely of correctly-graded self-assertions cannot reach "ready" on them
alone (pinned by `test_readiness_is_not_reached_on_self_asserted_beliefs_about_the_world_alone`) — but
if the engine grades a world-belief `explicit` regardless of the prompt, nothing here catches it. That
is the concrete gap a future measurement (the golden harness, run against a first-person request) is
for, not this change.

## Alternatives rejected

- **A separate field for obtainability, beside `Confidence`.** The literal reading of #610's own open
  question. Rejected because every existing reader (`state_of`, `readiness_blockers`, `diff_models`,
  `understanding_view`, the persisted mirror) already switches on `confidence`; a second field would
  have to be threaded through every one of them for no payoff `testable`-as-a-value doesn't already
  give, and it invites exactly the six-values-two-fields sprawl the coupling was meant to avoid.
- **A boolean flag (e.g. `self_asserted`) marking an `explicit` slot as resting only on the requester's
  own word about the world.** This was the most seriously considered alternative: it would let `core/`
  itself refuse to count such a slot as confirmed, closing the exact gap named above under "What
  breaking it cost". Rejected for now because it duplicates what `inferred`/`testable` already express
  once the engine grades correctly, and a field whose only job is to catch the engine grading
  *incorrectly* is a patch over a prompt-discipline problem, not a fix for it — the golden harness,
  not a second flag, is the honest way to find out whether the grading rule actually holds. Revisit if
  a measured instance shows the engine mis-grading in practice; that is a real-instance bar this
  repository already applies elsewhere (`CLAUDE.md`'s "two real instances of the drift" rule for a new
  guard).
- **Per-slot classification in the schema (`intent` vs `world`), instead of a per-instance judgment.**
  #611's own option 1. Rejected as too coarse: `business_rules` can hold a requester's own policy
  decision in one session and a guess about a regulator's requirement in another: the distinction is a
  property of what was *said*, not of which slot it landed in.
- **Leave the vocabulary alone and fix the prompt only (#611's option 3).** Rejected explicitly by both
  issues: "a rule enforced in a prompt and nowhere else is not enforced." Landing the grading rule in
  `framework/model_schema.json` as well as `engine.md` is the minimum this change could do to answer
  that — both files ship inside `{{SCHEMA}}`/the prompt, so it is still prompt-enforced in the end, but
  it is no longer a single hand-typed sentence nobody re-checks against its sibling
  (`test_the_confidence_grading_in_the_schema_and_the_prompt_agree` pins the two staying in sync).
