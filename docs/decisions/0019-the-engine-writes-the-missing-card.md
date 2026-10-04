# The engine judges whether a domain needs a context card, and writes the missing one

**Slug:** `the-engine-writes-the-missing-card`

> **Landed.** The judgment and the ordering shipped with #593, writing the card with #598. Both
> builds corrected this record in place: the re-claim step (#593), and where the card lives (#598).

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
**none covers a domain that needs one** — write one. Warranting signals are those that can change the
solution: heavy legislation, a licensed profession, safety- or money-critical obligations, unsettled
frontier tech, a niche vocabulary.

**Not what #492 refused.** That was *selection* — silently ranking installed cards inside a free,
decidable preflight. This is a paid call at discovery, shown to the user before it influences
anything, and ranks nothing; `doctor`, `session verify` and `render_grounding` are unchanged.

**Claim, then judge, then discover.** `claim_session` runs first on the user's cards, so invariant 13
holds: a repeat discovery is refused before anything is billed. The judgment is its own small call
without `SHARED_PROMPT_HEAD`, in hand before the first turn, the one that builds the model. A selected
card changes identity (invariant 11) after the claim, so when the verdict narrows the empty session is
**deleted and re-claimed** — only if the caller named no cards, this call created the session, and it
is still at revision 0 re-read under the lock (invariant 9). `DiscoveryService.claim_and_ground` owns
the sequence (invariant 14).

**The card is fields from the same call.** An `uncovered` verdict carries a `GeneratedCard` — stem,
title, domain, users, entities, concepts, regulation, traps — and `GeneratedCard.markdown()` is the
one writer, in `_template.md`'s sections. No second call: the judgment already reads the request.

**It is written where every card is found, and that is its promotion.** `write_generated_card` puts it
in `user_context_dir()`, and the session is re-claimed selecting it alone, under the four conditions
above; a written card is then selection, not provenance. Every verb that loads a session's cards
resolves it unchanged. A later first discovery is offered it by the domain line its writer wrote, so a
card earned once is reusable without a second act. The costs: the run writes outside the workspace,
and the card joins the every-card default of later unscoped sessions. Both are shown with the path.

**The trust boundary widens**: a card authored from an untrusted request lands in the system block of
every later call. What holds it: the engine fills fields, never Markdown; every value is one line (C0,
C1, DEL and U+2028/9 refused), each line, list and the whole card capped — **refused, not trimmed**
(invariant 3), through the retry loop; the stem is a lowercase-hyphen pattern checked before
any filesystem call, and one an installed card or `none` answers to is refused, never shadowed —
refused in the retry loop, again in core, and by a no-clobber link; the card is printed in full,
through `display_text`, before the turn it grounds, under the head's untrusted-data sentence. The
prompt asks for the domain, never the client, because the card outlives the request. A refused or
failed write keeps every card and says why; it never costs the discovery.

## What breaking it cost

No incident for synthesis. The cost of the state it replaces is the dilution measurement and a gap
written down twice (#492, #489) and left open. The failure to watch: a confidently wrong card, read
past by a user who cannot tell, sharpening questions in the wrong direction — and, since #598,
offered again to the next request in that domain until someone edits or deletes the file.

## Alternatives rejected

- **The human as detector** — fails at the one moment that matters; the default loads every card.
- **A keyword heuristic or `context.status: mismatched`** — right often enough to be trusted, wrong
  silently, on the one path whose value is being decidable.
- **Rank installed cards and pick the best** — what #492 refused; an unrelated card must not be
  offered merely because it exists.
- **A session-scoped card, promoted later** — this record's first answer. Nothing finds a card in a
  session directory: `answer`, the generators, `status`, `doctor`, `session verify` and `rescope` would
  each need a second card lookup, the second implementation the architecture forbids. Its argument
  against the user root, a silent stem clash, is held instead by refusing the clash.
- **Hold the card in the prompt only** — nothing for the user to read or correct.
- **A second call that writes the card** — the judgment already has the request in front of it; a
  second paid call buys nothing a field does not.
- **Judge inside the first discovery call** — the card would arrive with the model it should inform.
- **Judge before `claim_session`** — a repeat discovery would pay before being refused (#133).
- **Report the narrowing and ask for a re-run with `--context`** — the first working version; it
  strands a claimed session at revision 0 and makes the user retype what the engine knew.
