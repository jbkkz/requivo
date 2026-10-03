# Claims carry provenance

**Slug:** `claims-carry-provenance`

Amends `decision: confidence-stays-one-axis` (0021).

## Context

A slot carries one `confidence`, and real slots mix statements of different provenance: a limit the
requester stated beside obligations the engine assumed (#747), forum evidence beside the builder's
own observation (#744), an as-is read from code and then confirmed (#716), answers an agent gave
from documents (#724). Graded `explicit`, such a slot overclaims; graded `inferred`, a high-impact
one blocks whatever the requester confirms. 0021 kept confidence one axis and rejected per-slot
classification because the mix belongs to the session, not to the slot id, and
`decision: the-expert-proposes-and-labels` could only derive its document tags from slot evidence
clauses. The design is #751.

## Decision

**Statements carry provenance; the slot keeps the readiness axis — in 3.x.**

- A slot may hold up to eight `claims`, each one statement with a `source` (`requester`, `artifact`,
  `evidence`, `proposed`, `domain`, `assumed`) that never changes under its content-derived id, and a
  `confirmation` (`open`, `let_stand`, `confirmed`, `to_test`) that is the only thing an answer moves.
  A ninth claim, a `to_test` with no `test_plan`, a confirmation with no `answered_by`, a claim rated
  above its slot: refused, never repaired (invariant 3).
- **In 3.x claims are informational.** They are recorded, validated, persisted, diffed and shown;
  readiness, `state_of`, `soft_slots` and `veto_defaults` still read the slot's `confidence`, and 0021's
  grading rule stands unchanged. A slot with one kind of statement needs no claims, and no claims is
  absent on disk, so a claimless session is written exactly as an older 3.x wrote it.
- **A claim rated below its slot's impact is counted, never capped.** `status` names the count; the
  golden harness watches the rate. A cap would encode a judgment that is sometimes right.
- `DesignDecision` gains an informational `source` (`requester` | `proposed`): who owns the choice.
- **What moves in 4.0 (#751, stage 2, not built):** readiness over claims — a high-impact claim must
  be `confirmed` by someone other than an agent; `let_stand` never counts at high impact; a bulk "yes"
  confirms only a list that was shown — with `format_version` 2 and the slot's four value fields
  removed. Until then this record describes a vocabulary, not a readiness rule.

## What breaking it cost

No incident yet. The named gap: in 3.x a slot can read `inferred` while every claim in it is
confirmed, and still block (#747's dilemma stays open until stage 2), and nothing in `core/` checks
that a claim's `source` matches where the statement came from — that is prompt discipline, for the
golden harness to measure.

## Alternatives rejected

- **Split mixed slots** (`constraints` into stated limits and assumed obligations) — fixes #747 only,
  costs every perimeter schema and DAG edge, and repeats the per-slot classification 0021 rejected.
- **A top-level side table of claims** — two sources of truth that drift, and a tri-state side list
  would keep the claims of a slot a turn rewrote, so provenance outlives its value (invariant 6).
- **Straight to 4.0** — a readiness change measured on nothing. Stage 1 buys the token cost and the
  source mix from real runs first, and a 3.x reader loses nothing by ignoring the field.
- **A second slot axis beside `confidence`** — the field 0021 rejected, threaded through every reader
  for a property that belongs to a statement, not to a topic.
