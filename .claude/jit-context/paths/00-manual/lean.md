---
title: "The tree records the rule, the tracker records the story"
description: "One line at a call site, five lines in a test docstring, the issue number as the pointer; the story stays on the tracker (#554)."
match: ^(src|tests)/.*\.py$
---

Editing under `src/` or `tests/`: prose here states the **rule**, never the **story** — the issue
and its pull request already hold the reproduction and the review (`decision: the-tree-records-the-rule`).

- **Call site: one line** — the rule, the cost of breaking it, the test that goes red. A second
  line only when the rule is not obvious from the code beside it. Never a paragraph.
- **Test docstring: five lines at most** — what the test pins, and the issue it cites.
- **A decision record is for a decision** a reader could reopen, never for an incident.
- **Keep the issue number.** It is the one pointer that survives every rename. A test is cited by
  its name, on one line.
- **A reason attached to a guard, or a MUST-FIRE note, stays** — that is the rule, not the story.

Ceilings live in `tests/lean_budget.toml` (#553) and only go down; write none in prose.
