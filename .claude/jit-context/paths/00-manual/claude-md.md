---
title: "CLAUDE.md is the rules and the map, under a ceiling"
description: "What may be added to CLAUDE.md and where everything else goes (#627)."
match: ^CLAUDE\.md$
---

`CLAUDE.md` is loaded into every session, and `tests/lean_budget.toml` caps its length. Before
adding a line, ask where it belongs:

- **A rule** the whole tree obeys, with the test that goes red: here, one line.
- **A story** (what went wrong, how it was found, what was tried): the tracker, under the issue.
- **A how-to** or a checklist: `docs/` (`docs/extending.md` for registries and slots), plus a
  jit-context path rule so it fires when the file it concerns is edited.
- **A number** (lines, tests, checks, versions): nowhere in prose; a ceiling in the budget or a
  measurement at the moment it is needed.

Never a paragraph per invariant, never a count no test can falsify, never a fourth copy of a rule
that already lives in a decision record, a jit rule and the ratchet.
