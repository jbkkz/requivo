# The tree records the rule; the tracker records the story

**Slug:** `the-tree-records-the-rule`

## Context

The bug-narrative rule (#75, #483, #286) answered *where* a story lives — in the test that goes red
when the guard is removed, with one line and the test's name left at the call site — and never *how
long it may be*. Applied three times, it moved paragraphs rather than shrinking them: from `src/`
into test docstrings (100 over fifteen lines, 12 over thirty, measured on `a016cc5`), from CLAUDE.md
into the same, and into fifteen decision records. Prose in the Python tree went from 13,000 to
22,000 lines in two weeks (#548). The tree became an archive of its own incidents — and that
archive already exists, in full, on the tracker: every one of those paragraphs cites an issue whose
body carries the same story, its reproduction and the review that closed it.

## Decision

**The tree records the rule; the tracker records the story.** Concretely:

- **At a call site: one line** — the rule, the cost of breaking it, the test that goes red. Never a
  paragraph. A second line is allowed only when the rule is not obvious from the code beside it.
- **In a test: five lines at most.** The docstring says what the test pins and cites the issue. The
  reproduction, the review discussion and the alternatives are in the issue and the pull request,
  which are the archive; the docstring points there rather than restating them.
- **A decision record exists for a *decision*** — a choice between alternatives a reader could
  reopen — never for an incident. The records this repository already holds are read against that
  bar in #549, and the ones that are incident reports become one line in the test they belong to.
- **CLAUDE.md states rules and the map.** Its invariants are one paragraph each. A count in prose
  that no test can falsify comes out — that part of the older rule stands.
- **The issue number is never lost.** It is the only pointer that survives every rename, and it is
  what makes the tracker the archive. `tests/test_narrative_references.py` keeps checking that test
  names resolve; nothing new checks issue numbers — the second-instance bar (#483) was not met.

This is not a licence to delete a *reason attached to a guard* or a MUST-FIRE note. Those are the
rule, not the story, and they stay on the line.

The rule is engraved in three places, each reaching a different moment, and nowhere else — a fourth
copy is the archive this record exists to stop:

1. **CLAUDE.md**, auto-loaded into every agent session — reading.
2. **A jit-context path rule**, `.claude/jit-context/paths/00-manual/lean.md`, firing on an edit
   under `src/` or `tests/` — writing, long after the session stopped holding CLAUDE.md in mind.
3. **The lean ratchet** (#553), `scripts/prose_measure.py` + `tests/lean_budget.toml`, ceilings that
   only go down — merging. The ceilings live there and in no prose.

## What breaking it cost

Nothing has yet gone red for this — it is a rule about length, and length is what the ratchet
measures once #553 lands. The cost that motivated it is the measurement above: 45% of `src/` is
prose, the test suite is 2.86× the product code, and the meta-guard estate is at 10,500 lines against
a CLAUDE.md that still said 5,118. An archive nobody can read in full is one nobody reads.

## Alternatives rejected

- **Move the narrative to `docs/decisions/` wholesale.** The external review's remedy, rejected in
  #75 and rejected again here: the person about to simplify a subtlety away is in the editor, and a
  pointer they will not follow is worse than the line it replaced. The line stays; only its length
  changes.
- **Keep the full story in the test docstring.** That was #75's answer and it produced the 22,000
  lines. A story lives once, and the tracker already holds it with the reproduction and the review
  attached, which a docstring never has.
- **A guard that checks every issue number resolves.** One real instance of a dangling issue number
  would fund it; #483 found none. Until a second is named, the pointer is kept by hand.
- **A fourth engraving point** — a `CONTRIBUTING.md` section, a PR template line. Every copy is a
  copy to keep in agreement, and the rule's own diagnosis is that copies accrete.
