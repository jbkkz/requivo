---
title: "The tree records the rule, the tracker records the story"
description: "One line at a call site, five lines in a test docstring, the issue number as the pointer; the story stays on the tracker (#554, #627)."
match: ^(src|tests|scripts)/.*\.py$
---

Prose in the Python tree states the **rule**, never the **story**: the issue and its pull request
already hold the reproduction and the review (`decision: the-tree-records-the-rule`).

- **Call site: one line.** The rule, the cost of breaking it, the test that goes red. Never a
  paragraph, never a history.
- **Docstring: what a caller needs and nothing a signature already says.** In a test, five lines at
  most and usually one: what it pins, the issue it cites.
- **A citation names a test function or a test file**, on one line, so it can be grepped.
- **A reason attached to a guard, or a MUST-FIRE note, stays.** That is the rule, not the story.
- **Tests: one file per subject, one fixture per subject, incidents parametrised.** A new test
  reverts the behaviour once to prove it goes red.

Ceilings live in `tests/lean_budget.toml` and only go down; write none in prose.
