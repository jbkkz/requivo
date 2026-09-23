# The tree records the rule; the tracker records the story

**Slug:** `the-tree-records-the-rule`

## Context

The bug-narrative rule (#75, #483, #286) said *where* a story lives — in the test that goes red, with
one line at the call site — and never *how long*. Applied three times it moved paragraphs instead of
shrinking them: into test docstrings (100 over fifteen lines on `a016cc5`), out of CLAUDE.md, into
fifteen decision records. Prose in the Python tree went from 13,000 to 22,000 lines in two weeks
(#548), and every paragraph cited an issue that already held the same story with its reproduction
and review.

## Decision

**The tree records the rule; the tracker records the story.**

- **A call site: one line** — rule, cost, the test that goes red; a second only when the rule is not
  obvious from the code beside it.
- **A test docstring: five lines at most** — what it pins and the issue it cites.
- **A decision record is for a decision** — a choice a reader could reopen — never an incident (#549).
- **CLAUDE.md states rules and the map**: one line per invariant naming its test, under a ceiling in
  `tests/lean_budget.toml` (#627). A count in prose no test can falsify comes out.
- **The issue number is never lost** — the one pointer that survives every rename. The reference
  guard in `tests/test_source_form.py` checks test names and slugs resolve; nothing checks issue
  numbers, since #483 found no second instance.
- **A citation may name a test file, not only a function** — corrected in place by #627, after #555
  measured that exact-name citations pinned ~40% of test names and blocked parametrise-merges.
  `tests/test_<subject>.py` resolves like `test_<name>`; a function name stays the better pointer
  when one test holds the rule.

Not a licence to delete a reason attached to a guard or a MUST-FIRE note: those are the rule. The
rule is engraved in three places, each for a different moment: CLAUDE.md (reading); the jit-context
path rule `.claude/jit-context/paths/00-manual/lean.md` (writing); the lean ratchet (#553),
`scripts/prose_measure.py` with `tests/lean_budget.toml` (merging) — where the ceilings live, and in
no prose.

## What breaking it cost

The measurement above: 45% of `src/` was prose, tests were 2.86× the product code, and the meta-guard
estate was 10,500 lines against a CLAUDE.md that said 5,118. An archive nobody can read in full is
one nobody reads.

## Alternatives rejected

- **Move the narrative to `docs/decisions/` wholesale** — rejected in #75 and again here: the person
  about to simplify a subtlety away is in the editor, and a pointer they will not follow is worse
  than the line it replaced.
- **The full story in the test docstring** — #75's answer, which produced the 22,000 lines.
- **A guard that every issue number resolves** — no real dangling instance yet.
- **A fourth engraving** (CONTRIBUTING, a PR template) — every copy is one more to keep in agreement.
