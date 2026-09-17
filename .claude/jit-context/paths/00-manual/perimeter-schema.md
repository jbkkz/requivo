---
title: "A slot belongs to one perimeter, and adding one moves every prompt"
description: "The three mandatory steps when a perimeter's schema changes, and the golden re-capture that follows (#608, #269)."
match: ^src/requivo/assets/perimeters/.*
---

A slot lives in one perimeter's `model_schema.json`. Adding or changing one:

1. `model_schema.json` is the single source (`schema_slot_ids(perimeter)` reads it).
2. `elicitation.md` beside it is kept by hand and unguarded: check the pillar table by eye
   (`decision: elicitation-schema-hand-kept`).
3. `core/dependencies.py`'s `_ARTIFACT_SLOTS_RAW`: every artifact set of this perimeter the slot
   shapes, or a reason in `tests/test_dependencies.py`'s `_SLOTS_WITH_NO_SPECIFIC_ARTIFACT`;
   `test_every_required_slot_is_consumed_by_a_specific_artifact_or_is_exempted` fails otherwise.

`{{SCHEMA}}` is substituted into every prompt: a full golden re-capture follows
(`scripts/golden_run.py`, then `golden_diff.py`). `docs/extending.md` has the conditional steps.
