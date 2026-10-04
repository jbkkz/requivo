# The engine judges whether a domain needs a context card, and writes the missing one

**Slug:** `the-engine-writes-the-missing-card`

> **Landed** with #593 (the judgment, the re-claim) and #598 (the card), each correcting it in place.

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

**Not what #492 refused** — silent ranking inside a free preflight. This is a paid call, shown before
it influences anything; `doctor`, `session verify` and `render_grounding` are unchanged.

**Claim, then judge, then discover.** `claim_session` runs first on the user's cards, so invariant 13
holds: a repeat discovery is refused before anything is billed. The judgment is its own small call
without `SHARED_PROMPT_HEAD`, in hand before the first turn, the one that builds the model. A selected
card changes identity (invariant 11) after the claim, so when the verdict narrows the empty session is
**deleted and re-claimed** — only if the caller named no cards, this call created the session, and it
is still at revision 0 re-read under the lock (invariant 9). `DiscoveryService.claim_and_ground` owns
the sequence (invariant 14).

**The card is fields from the same call.** An `uncovered` verdict carries a `GeneratedCard`, and
`GeneratedCard.markdown()` is the one writer, in `_template.md`'s sections.

**Written into the workspace, kept only on consent.** `write_generated_card` puts it in
`.requivo/cards/`, a third directory of the one card lookup, and the session is re-claimed selecting
it alone under the four conditions above: a written card is selection, not provenance. That directory
answers a named selection only — never the every-card default, never a judgment — so every verb that
loads a session's cards resolves it unchanged, and nothing else sees it. Keeping it is the user's act —
asked at a terminal, default No, or `--save-card` — and copies it to `user_context_dir()` under the same
stem: its sessions keep their identity, a later judgment is offered it, and the shadowed draft stays so
no reader mid-turn loses it. A taken stem moves to `-2`…`-9`; an identical unsaved card is reused only
while no installed card owns its stem. Costs left: unsaved cards read the ambient workspace, as
`decision: debug-dump-ambient-root` does, so a repository rooted elsewhere does not see them.

**The trust boundary widens**: a card authored from an untrusted request lands in the system block of
every later call. What holds it: the engine fills fields, never Markdown; every value is one line (C0,
C1, DEL and U+2028/9 refused), each line, list and the whole card capped — **refused, not trimmed**
(invariant 3), through the retry loop; the stem is a lowercase-hyphen pattern checked before
any filesystem call, and one a card or `none` answers to is never shadowed — refused in the retry
loop, moved to a free suffix in core, and published by a no-clobber link; the card is printed in full,
through `display_text`, before the turn it grounds, under the head's untrusted-data sentence. The
prompt asks for the domain, never the client, because the card outlives the request. A refused or
failed write keeps every card and says why; it never costs the discovery.

## What breaking it cost

No incident for synthesis. The cost of the state it replaces is the dilution measurement and a gap
written down twice (#492, #489) and left open. The failure to watch: a confidently wrong card, read
past by a user who cannot tell, sharpening questions in the wrong direction — and, once kept,
offered again to the next request in that domain until someone edits or deletes the file.

## Alternatives rejected

- **The human as detector** — fails at the one moment that matters; the default loads every card.
- **A keyword heuristic or `context.status: mismatched`** — right often enough to be trusted, wrong
  silently, on the one path whose value is being decidable.
- **Rank installed cards and pick the best** — what #492 refused; an unrelated card must not be
  offered merely because it exists.
- **A card inside the session directory** — this record's first answer. `load_context` sees a
  selection, never a session: `answer`, the generators, `status`, `doctor`, `session verify` and
  `rescope` would each need a second card lookup, the second implementation the architecture forbids.
- **Write straight into `user_context_dir()`** — #598's first build: a run wrote outside the workspace
  unasked, and every card it wrote diluted every later unscoped session.
- **Fall back to every card when the user declines** — the dilution this record exists to end.
- **Hold the card in the prompt only** — nothing for the user to read or correct.
- **A second call that writes the card** — the judgment already reads the request; a field is free.
- **Judge inside the first discovery call** — the card would arrive with the model it should inform.
- **Judge before `claim_session`** — a repeat discovery would pay before being refused (#133).
- **Report the narrowing and ask for a re-run with `--context`** — the first working version; it
  strands a claimed session at revision 0 and makes the user retype what the engine knew.
