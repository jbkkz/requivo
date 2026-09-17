---
title: "A decision record is a decision, in four headings, under a ceiling"
description: "Shape and size of a docs/decisions/ record, and what does not belong there (#627)."
match: ^docs/decisions/.*\.md$
---

A record is for a **decision**: a choice between alternatives a reader could reopen, a fact about
something outside the repository, or a cost tradeoff with a threshold. Never an incident: that is a
line in the test that pins it, and the issue holds the story.

- Four headings, in order: Context, Decision, What breaking it cost, Alternatives rejected.
- A `**Slug:**` line; referenced by `` `decision: <slug>` `` on one line, never by path.
- `tests/lean_budget.toml` caps a record's length. State the decision so it can be disagreed with,
  each alternative with its reason, and stop. Measurements go in the issue.
- A record written ahead of the code says so; when the code lands, correct it in place.
