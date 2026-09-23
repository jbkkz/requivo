# The engine judges whether a domain needs a context card, and writes the missing one

**Slug:** `the-engine-writes-the-missing-card`

> **Partly landed.** The judgment and the ordering are in the tree; writing the card is not (#593).
> The ordering was corrected in place once building it revealed the re-claim step below.

## Context

*Impact* is estimated against the context cards, so they decide which questions get asked. With no
`--context` every installed card loads, and dilution is measured: `financial-reporting` cost
`doc-reapproval` its sharpest question (3/3 → 1/3). Relevance is deliberately not computed:
`render_grounding` (#492) and the plugin's `run` skill (#489) both appoint **the human as the
detector**. Right for a free deterministic preflight; wrong at a **first** run, when that human has
never heard of cards, and a request from outside the bundled domains reaches `ready` silently.

## Decision

At the **first** discovery only, the engine judges the request's domain and takes one path: **no card
warranted** (one line, no menu); **an installed card covers it** (named, selected, reason given); or
**none covers a domain that needs one** — write one from the request, in `_template.md`'s sections.
Warranting signals are those that can change the solution: heavy legislation, a licensed
profession, safety- or money-critical obligations, unsettled frontier tech, a niche vocabulary. A
written card is **session-scoped, shown in full, and frozen** for the session (like `only`, #258);
promotion to `user_context_dir()` is a separate explicit act that names a stem collision first.

**Not what #492 refused.** That was *selection* — silently ranking installed cards inside a free,
decidable preflight. This is a paid call at discovery, shown to the user before it influences
anything, and ranks nothing; `doctor`, `session verify` and `render_grounding` are unchanged.

**Claim, then judge, then discover.** `claim_session` runs first on the user's cards, so invariant 13
holds: a repeat discovery is refused before anything is billed. The judgment is its own small call
without `SHARED_PROMPT_HEAD`, and its card is in hand before the first turn, the one that builds the
model. A **written** card is provenance on the revision (invariant 6), not identity (invariant 11).
A **selected** card does change identity after the claim, so when the verdict narrows the empty
session is **deleted and re-claimed** — only if the verdict narrows, the caller named no cards, this
call created the session, and it is still at revision 0 re-read under the lock (invariant 9).
`DiscoveryService.claim_and_ground` owns the sequence (invariant 14).

**The trust boundary widens**: a card authored from an untrusted request lands in the system block.
#593 owes three holds: rendered to the user before a later turn; the template's shape or **refused,
not trimmed** (invariant 3); neutralised at every interpretation site, its name validated
(`normalize_tokens`).

## What breaking it cost

No incident for synthesis. The cost of the state it replaces is the dilution measurement and a gap
written down twice (#492, #489) and left open. The failure to watch: a confidently wrong card, read
past by a user who cannot tell, sharpening questions in the wrong direction.

## Alternatives rejected

- **The human as detector** — fails at the one moment that matters; the default loads every card.
- **A keyword heuristic or `context.status: mismatched`** — right often enough to be trusted, wrong
  silently, on the one path whose value is being decidable.
- **Rank installed cards and pick the best** — what #492 refused; an unrelated card must not be
  offered merely because it exists.
- **Write into `user_context_dir()`** — a user card wins a stem clash silently, for every session.
- **Hold the card in the prompt only** — nothing for the user to read or correct.
- **Judge inside the first discovery call** — the card would arrive with the model it should inform.
- **Judge before `claim_session`** — a repeat discovery would pay before being refused (#133).
- **Report the narrowing and ask for a re-run with `--context`** — the first working version; it
  strands a claimed session at revision 0 and makes the user retype what the engine knew.
