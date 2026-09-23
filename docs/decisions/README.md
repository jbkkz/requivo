# Decision records

A bug's *story* lives on the tracker — the issue and the pull request that closed it; the tree keeps
the *rule* (`decision: the-tree-records-the-rule`). So a record here is for a **decision** — a choice
between alternatives a reader could reopen — and never for an incident. In practice, three shapes:

- **A fact about something outside the repository** — an API's, a platform's, a service's behaviour.
  Nothing here can exercise it, so nothing here can go red for it.
- **A rejected alternative.** Nothing goes red when a path is *not* taken.
- **A cost tradeoff with a threshold** — an argument, not a guard.

For anything else, the honest answer is usually a missing test.

## Shape

Four headings, in this order, so records can be scanned against each other. `tests/lean_budget.toml`
caps a record's length: state the decision so it can be disagreed with, each alternative with its
reason, and stop — measurements and narrative go in the issue.

```markdown
# <Title>

**Slug:** `<stable-kebab-slug>`

## Context
What was true, and what question came up.

## Decision
What was decided, stated so it can be disagreed with.

## What breaking it cost
The concrete failure. If there is none yet, say so plainly rather than inventing one.

## Alternatives rejected
Each with the reason.
```

## Tense

A record is usually written while its subject is still being built. **Write the unbuilt half so it
cannot be mistaken for a description of the tree** — name the issue that builds it, or mark its
status outright. When it lands, **correct the record in place** and say it was forward-looking. A
convention, not a guard; written down because #505 (`0006`) and #509 (`0004`) each described a
protection the tree did not yet have.

## Referencing one

**By slug, never by path**: `` `decision: <slug>` `` on one line at the line that rests on it.
`tests/test_source_form.py`'s reference guard checks that it resolves. The filename's number is for
ordering, not reference.
