---
title: "A prompt or card edit is measured, never judged from one run"
description: "What holds when editing assets/prompts or assets/context: the shared head, the contract, the golden harness (#258, #266)."
match: ^src/requivo/assets/(prompts|context)/.*\.md$
---

- **Every template opens with the shared head**, byte-identical across templates: that block is the
  cross-operation cache prefix, and `build_system_prompt` refuses a template that does not open
  with it. Edit the head in `SHARED_PROMPT_HEAD`, not in one file.
- **The "Output format" example must validate against the contract** the generator parses replies
  with (`tests/test_prompt_contracts.py`); a drifted example costs up to three paid calls per op.
- **User-facing text carries the Voice rule**: no slot ids, percentages or confidence labels.
- **A card dilutes its neighbours**: every card is in every prompt by default.
- **Then measure**: `scripts/golden_run.py` → `scripts/golden_diff.py --questions`; commit the new
  baseline only when the change was intended (`docs/evaluations.md`).
