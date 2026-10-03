# Repository grounding starts in the plugin; the CLI scanner waits for a measurement

**Slug:** `repository-grounding-starts-in-the-plugin`

## Context

A session's whole knowledge of the user's world was the sentence they typed, even when it was started
inside a repository that already answers half of what its first turn asks (#594, part of #591).
Reading that repository is cheap where the file tools already exist — a Claude Code session, with no
paid call and no key in the path. Anywhere else it is a subsystem: what to read, a token budget,
`.gitignore`, secrets, binaries, vendored trees, monorepos.

## Decision

**The plugin grounds first, and the CLI gets no scanner yet.** `/requivo:run` reads the working
directory before it reasons a new session, and hands the user a perimeter recap before the first
question: what the codebase appears to be, what the request touches, the request restated, the cards,
what was assumed, and what was not read. A slot filled from the repository is `inferred` with
`repo: <path>` evidence, and repository text sits inside the plugin's trust boundary (`REASONING.md`).
No prompt asset moved.

**The trigger.** The CLI gets a scanner when the plugin's recap has **measurably changed the
questions** on a request whose answers were in the repository — a golden fixture of that shape, not
an impression. The harness drives the engine rather than a Claude Code session, so that fixture (a
request paired with a checkout, its questions captured with and without the grounding) is the first
piece of work the trigger asks for. The CLI recap without the repository half shipped in the same
release (#709): a first `requivo run`/`discover` opens with the cards and the restated request.

## What breaking it cost

Nothing yet for the deferral. What the plugin half replaces is one of the three frictions in #591's
origin walkthrough: a first run with no grounding in the code already there.

## Alternatives rejected

- **A CLI scanner now** — funds a subsystem on the assumption the trigger exists to test.
- **A CLI recap renderer in this change** — deferred, not refused: its CLI-side half (the cards, the
  restated request, the assumptions) needs no repository, and shipped as its own change (#709).
- **Grade repository facts `explicit`** — the model reaches `ready` on facts nobody confirmed.
- **Re-read the repository on every turn** — the first run's evidence already carries what was read,
  and a second read is a second view of the user's world beside the session's one snapshot.
