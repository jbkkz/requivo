# Decision records

The narrow set of things that belong here, and why the set is narrow.

This project's normal home for a bug narrative is the code, and after that the test that goes red
when the guard is removed. `CLAUDE.md` states the rule under *Where a bug narrative lives*: a
paragraph recounting a past bug must be backed by a red test, and if it is, it belongs **in that
test** with one line and the test's name left at the call site. An external review proposed moving
all of it here instead; that was rejected, because the person about to simplify a subtlety away is in
the editor and a pointer they will not follow is worse than the paragraph it replaced.

So a record here is for **what no test can reach**. In practice that is three shapes:

- **A fact about something outside the repository** — an API's behaviour, a platform's, a service's.
  Nothing here can exercise it, so nothing here can go red for it.
- **A rejected alternative.** Nothing goes red when a path is *not* taken.
- **A cost tradeoff with a threshold** — an argument, not a guard.

If you are about to write a record for anything else, the honest answer is usually a missing test.

## Shape

Four headings, in this order. Keep them; a record that argues in a different order is one nobody can
scan against its siblings.

```markdown
# <Title>

**Slug:** `<stable-kebab-slug>`

## Context
What was true, and what question came up.

## Decision
What was decided, stated so it can be disagreed with.

## What breaking it cost
The concrete failure — the one that makes this worth a file. If there is none yet, say so plainly
rather than inventing one.

## Alternatives rejected
Each with the reason. This is usually the half a reader actually needs.
```

## Tense: a record written ahead of the code

A record is usually written while the thing it decides is still being built, so most of it describes
a tree that does not exist yet. **Write that in a form a reader cannot mistake for a description of
the tree** — name the issue or slice that builds it, or mark the paragraph's status outright. The
failure is not a wrong sentence; it is an ambiguous one, where *"what it keeps"* reads equally as
*keeps, once built* and *keeps, today*, and a reader checking the record before wiring something up
is told a protection exists.

**When a record's forward half lands, correct the record in place** and say it was forward-looking
when written — `0006`'s *"carried 31 open alerts at the time this record was written — since
dismissed"* is the shape. Do not silently rewrite it into the present: the argument is the record,
and when it was made is part of it.

This is a convention and not a guard, deliberately. `tests/test_narrative_references.py` resolves a
name and has no opinion about tense, and nothing mechanical can have one —
`CLAUDE.md`'s meta-guard budget says a taste does not get a test. It is written down because the
two-instance bar this repo applies is met: **#505** (`0006` describing a traversal guard on an
unmerged branch as though it were in the tree) and **#509** (`0004` §5 describing a cross-site
posture the API did not have), one release apart, in two records.

## Referencing one

**By slug, never by path.** Paths in this repository move: the package was renamed once, a module
became a package, and a 2147-line test file became seven, all inside a fortnight. A slug is greppable
and survives every one of those. Write it as `` `decision: <slug>` `` at the line that rests on it, on
one line — a wrap makes it unfindable, which is the failure
`tests/test_narrative_references.py` exists to catch for test names.

The filename carries a number for ordering and the slug for meaning. The number is not the reference.
