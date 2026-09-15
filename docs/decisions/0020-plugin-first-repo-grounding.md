# Ground the first run in the plugin first, and gate a CLI scanner on proof

**Slug:** `plugin-first-repo-grounding`

## Context

Nothing in Requivo read the checkout it was run in (#591): a session's entire knowledge of the
user's world was the sentence they typed, even when it started inside a repository that already
answered half the questions about to be asked — what exists, what it is built with, what the
request is going to touch. #594 closes that for the **first** run, by handing the user a perimeter
recap before the first question.

Two surfaces could ground a session in the repository around it, and they are not the same size of
problem:

- **The Claude Code plugin** already holds file tools (`Read`, `Glob`, `Grep`) in the same session
  that does the reasoning, spends no API call to use them, and needs nothing installed beyond what
  ships with the plugin.
- **The CLI's `discover`** has no equivalent: giving it the same grounding means building a real
  subsystem first — what to read, a token budget, `.gitignore`, secrets, binaries, vendored trees,
  monorepos — none of which the plugin's own file tools need, because Claude already draws those
  lines when it reads a repository directly.

## Decision

**Ship in the plugin first, and only there.** `plugins/claude-code/skills/run/SKILL.md` gets a
bounded grounding step — a manifest, a root README/CLAUDE.md, the top level, one narrow search —
folded into the same turn that already reasons the model, at no extra provider call. The CLI gets no
scanner in this change. That is a scoping decision, not an omission: a CLI-side scanner is real
infrastructure, and it should be funded by the plugin version proving the grounding is worth what it
costs, not by this issue assuming it.

**The graduation trigger, so this is not re-argued from a fresh impression:** the CLI gets a scanner
when the plugin's recap has **measurably** changed the questions on a request whose answers were
sitting in the repository — a golden fixture of that shape, built the way the golden harness's own
`fixtures/golden/requests.md` builds one (a request run with and without the grounding step, showing
a real shift, not an anecdote). Until a fixture like that exists, the CLI's `discover` stays exactly
as it is today: no repository read, nothing implied by its absence.

## What breaking it cost

No incident is on record — the plugin-side grounding step this decision ships is itself new, so
inventing a cost here would be dishonest. What is on record is the cost of the state before it: a
session's first turn interrogating a user about facts already sitting in their own checkout, which
is the friction #591 was filed from (a solo builder's first-run walkthrough, three separate
frictions in one sitting, this being one of them).

## Alternatives rejected

- **Build the CLI scanner in the same change.** Rejected as premature scope: a token budget,
  `.gitignore` handling, secret redaction, binary/vendored-tree exclusion and monorepo layout are
  each a real design question, and answering all of them before anything has shown the grounding is
  worth the read is exactly the ordering this decision avoids.
- **A keyword heuristic in the CLI's free deterministic preflight**, so at least something narrows
  the CLI's blind spot immediately. Rejected for the same reason `render_grounding`'s docstring
  (#492) already refuses the neighbouring idea for context cards: right often enough to be trusted,
  wrong silently, and it plants a fallible verdict in the one path whose whole value is that it is
  decidable from the filesystem alone.
- **Judge "worth it" from an impression rather than a fixture.** Rejected because it is exactly the
  failure mode `CLAUDE.md`'s own meta-guard rule names for a new guard: one plausible instance is a
  taste, not a trigger — the bar here is a measured, reproducible shift in the questions asked, the
  same standard the golden harness already holds every other prompt-behaviour change to.
