# Confidence stays one axis

**Slug:** `confidence-stays-one-axis`

## Context

The target user is now as often a builder with a half-formed idea as a PM relaying a request, and
`Confidence` (`explicit`/`inferred`/`empty`) breaks two ways for the builder. **#610**: `empty`
conflates *someone knows, ask* (blocks readiness, correctly) with *nobody knows until something is
tried* (must not block) — the default shape of a builder's bet, so a session either never reaches
`ready` or reaches it on a guess graded `inferred`. **#611**: `explicit` means "the client said so";
with no client, everything the user types reads `explicit`, and readiness measures typing. Both
issues asked whether this is one axis widened or two crossed — answered apart, a three-value enum
grows to six values and no thesis.

## Decision

**One axis: `Confidence` gains `testable`, and `explicit` is redefined rather than joined by a field.**

- **`testable`**: unknown, and settleable only by a real test. It must carry `test_plan` or is
  refused (as a `Challenge`'s required parts are), and does not block readiness. Settling one is an
  ordinary confidence change that `diff_models`/`propagate()` already watch.
- **`explicit` requires an authority.** A third party's word always qualifies; a builder's own
  **intent** (what they want, will build, will spend) is an authority on itself; their **belief
  about the world** (will users want or pay for this) is not — it is `inferred` or `testable`.
- `testable` is a fourth *way of knowing* in the same question the other three answer; #611 needs no
  new value, only a grading rule stated in the perimeter's `model_schema.json` `confidence` block and
  in `engine.md` (`test_the_confidence_grading_in_the_schema_and_the_prompt_agree`).
- `PersistedSlot.confidence` is `Confidence | str`: an unknown value round-trips and is never read
  as `explicit` (invariant 8 applied to an enum).

## What breaking it cost

No incident. The named gap: the #611 half is a prompt rule `core/` cannot verify — nothing tells a
stated intent from a stated belief. The type system guarantees only that `inferred` and `testable`
never count as `explicit`
(`test_readiness_is_not_reached_on_self_asserted_beliefs_about_the_world_alone`); an engine grading a
belief `explicit` anyway is for the golden harness to find.

## Alternatives rejected

- **A separate obtainability field** — every reader switches on `confidence`; a second field
  threads through all of them for nothing `testable` does not give.
- **A `self_asserted` flag on `explicit`** — the closest call: it would let `core/` refuse such a
  slot. Rejected as a patch over prompt discipline; revisit on a measured mis-grading.
- **Per-slot `intent`/`world` classification** (#611 option 1) — too coarse: the same slot holds a
  policy decision in one session and a guess about a regulator in another.
- **Prompt-only fix** (#611 option 3) — "a rule enforced in a prompt and nowhere else is not
  enforced"; the schema and prompt now state it together, pinned in agreement.
