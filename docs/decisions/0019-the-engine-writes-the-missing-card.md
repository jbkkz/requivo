# The engine judges whether a domain needs a context card, and writes the missing one

**Slug:** `the-engine-writes-the-missing-card`

> **Forward-looking as written.** This record argues the decision; #593 builds it. Nothing below
> describes the tree as it stands except the Context section, which describes it as of 3.3.0.

## Context

`information_value = uncertainty × impact` is the engine's whole driver, and the *impact* half is
estimated against the product context cards. The cards are therefore not decoration: they decide
which questions get asked and which gaps are left as assumptions.

Three things are true of them today, and they compose badly at a first run:

- **The default is every card.** With no `--context`, `load_context()` concatenates every installed
  card. The four bundled ones describe B2B enterprise domains.
- **Dilution is measured, not theoretical.** Adding `financial-reporting` cost `doc-reapproval` its
  sharpest question — 3 runs in 3 down to 1 in 3, displaced by that card's audit-trail emphasis.
  A second instance is on record. `--context` lets a session opt into a subset; nothing routes
  automatically.
- **Relevance is deliberately not computed, and the repo says so twice.** `render_grounding`'s
  docstring (#492) refuses a `context.status: mismatched` and names the trade it refused: a
  relevance judgment needs either a model call in the free deterministic preflight — putting a paid,
  fallible verdict in front of the one path whose value is that it is decidable — or a keyword
  heuristic, "right often enough to be trusted and wrong silently". The plugin's `run` skill says
  the same from the other side (#489): *"`ok` means present and readable. It does not mean relevant,
  and there is no status that does."* Both conclude the same way: **the human is the detector.**

That conclusion is correct for a free, deterministic preflight, and it is the wrong outcome for a
**first** run. The detector it appoints has, at that exact moment, never heard of context cards, does
not know what the installed ones describe, and has no way to know that their relevance is what is
quietly choosing the questions. A request from outside the bundled domains is scored against a
product it has nothing to do with, produces a model, reaches `ready`, and says nothing on screen
about any of it.

`render_grounding` also writes down the revisit trigger: *a third measured instance of a card
diluting its neighbour funds automatic relevance routing. Two are on record.* This record does not
claim that third instance, and does not need to — see **Decision**, which is a different feature from
the one that trigger gates.

## Decision

At the **first** discovery, and only there, the engine judges the request's domain and takes exactly
one of three paths:

1. **No card warranted.** The common case. One line, no menu, no prompt, no installed card offered.
2. **An installed card covers it.** Name it, select it, say what it was chosen for.
3. **The domain warrants a card and none covers it.** Write one, from the request, following
   `assets/context/_template.md`'s sections.

The signals that warrant a card are those that carry constraints capable of *changing the solution*:
jurisdictional or heavy legislation, an accredited or licensed profession, a safety- or
money-critical obligation, frontier tech with unsettled conventions, a niche vertical with its own
object vocabulary. That is the same test the engine already applies to everything else.

A written card is **session-scoped, shown, and frozen**: written into the session, rendered to the
user in full, applied to every subsequent turn of that session, and never re-derived mid-session —
the same rule `converse()` already holds for `only`, for the same reason (#258). Promotion to
`user_context_dir()` is a separate, explicit act, offered afterwards, which names a stem collision
before causing one.

### Why this survives #492, which refused the neighbouring thing

The thing refused was **selection**: deciding whether installed card X applies to request Y, cheaply
and silently, inside a preflight whose value is that it is decidable from the filesystem. This is
**synthesis**, and it differs on all three axes that refusal turned on.

- It is a **paid call at discovery time**, not a free deterministic preflight. `doctor`, `session
  verify` and `render_grounding` keep answering exactly what they answer today, and stay decidable.
- Its output is **shown to the user before it influences anything**. #492's real objection was a
  verdict that is wrong *silently*; a card the user reads is a verdict that cannot be.
- It **does not rank installed cards against each other.** Path 1 is "say one line and move on" and
  path 3 is "write one" — neither is the silent ranking that was refused.

### The ordering, which is the part with invariants on it

**Claim, then judge, then discover.** In that order, and the order is the decision:

- `claim_session(request, cards=...)` runs **first**, on the user-supplied cards, free and
  deterministic. Invariant 13 is untouched: the revision-0 claim still precedes every paid call, so a
  repeat discovery is still refused before anything is billed — including before the judgment.
- The judgment is a **small call of its own**, carrying the request and the installed cards' own
  descriptions. It does **not** carry `SHARED_PROMPT_HEAD`, so it is a few hundred tokens rather than
  a share of the ~9k prefix, and it does not disturb the cache breakpoint the discovery turn depends
  on.
- The card it produces is in hand **before the first discovery turn**, so that turn — the one that
  builds the model — is the one it grounds. A card that only applied from turn 2 would miss the turn
  it exists for.

**A generated card is provenance, not identity.** Invariant 11 says a session's identity is the
request *and* its card selection, claimed atomically. That stays exactly as written: identity answers
*is this the same discovery someone already started?*, and that is decided by what the user **asked
for** — the request and the cards they chose — never by what the engine decided to write. A generated
card is an *output* of the discovery, like the model itself, and outputs do not belong in identity.
It is recorded on the revision instead (invariant 6), where a provenance field that is populated is
the rule.

### The trust boundary this widens, and what holds it

Today every context card is a file an operator installed. A generated one is authored from an
**untrusted client request** and then lands in the **system** block. That is a real widening and is
named here rather than discovered later. Three things hold it, and #593 owes all three:

- the card is rendered to the user before it influences a subsequent turn;
- its shape is the template's sections, not free prose — a reply that does not parse as the template
  is **refused, not trimmed** (invariant 3);
- it is neutralised at every interpretation site the way a question already is (`display_text`), and
  its name never reaches a filesystem call unvalidated (invariant 14, `normalize_tokens`).

## What breaking it cost

**No incident is on record for the synthesis half, and inventing one would be dishonest.** What is on
record is the cost of the state this replaces, which is why the decision is worth a file:

- The dilution measurement above — a card from an unrelated domain displacing another request's
  sharpest question, 3/3 to 1/3 — is the concrete form of "impact scored against the wrong product".
- The repo has written down twice, in two surfaces (#492, #489), that a session can be grounded on
  the wrong product, reach `ready`, and say nothing about it — and left the gap open both times,
  appointing a human detector who does not exist yet at a first run.

If this decision is wrong, the shape of the failure is predictable and should be named here when it
happens: a generated card that is confidently wrong about a domain, read past by a user who has no
way to tell, sharpening the questions in the wrong direction more effectively than no card at all.
That is the risk the "shown before it influences anything" rule is spending its complexity on, and it
is the one to watch.

## Alternatives rejected

- **Keep the human as the detector (the status quo).** Correct where it was decided — a free,
  deterministic readout — and it fails at the only moment that matters, because at a first run the
  appointed detector does not yet know cards exist. The status quo is not neutral: the default loads
  every card, so doing nothing actively scores the request against four unrelated domains.
- **A keyword heuristic in the free deterministic preflight.** #492's own words, and still right:
  right often enough to be trusted, wrong silently. It also puts a fallible verdict on the one path
  whose entire value is that it is decidable from the filesystem.
- **A `context.status: mismatched` value.** Same objection one field along, and worse: every other
  value of that vocabulary is decidable from disk, so adding one that is not makes the whole
  vocabulary untrustworthy rather than just the new member.
- **Rank the installed cards and pick the best.** This is the thing #492 actually refused, and it is
  also the behaviour the request that prompted this record was most specific about not wanting: an
  unrelated card must not be offered merely because one exists. Path 2 above selects a card only when
  the judgment says it *covers* the domain, which is a different question from which of four is
  least bad.
- **Write the card straight into `user_context_dir()`.** A run would write outside the workspace, and
  `_card_paths()` lets a user card win a stem clash silently — so a generated `financial-reporting.md`
  would shadow the bundled one for every later session, invisibly. Promotion stays an explicit second
  act for exactly that reason.
- **Never persist the synthesised context — hold it in the turn's prompt only.** Cheapest, and it
  removes the one property that makes this admissible at all: there would be nothing for the user to
  read, correct or disagree with, which is precisely the silent-verdict failure #492 refused.
- **Judge inside the first discovery call, to save a call.** Circular: the card would arrive in the
  same reply as the model it was supposed to inform, so the first turn — the one that builds the
  model — would be the one turn it could not ground. The saving is also smaller than it looks, since
  the judgment call carries no shared prefix.
- **Judge before `claim_session`, so a generated card can join identity.** Rejected on invariant 13,
  which is the more expensive of the two to bend: #133's lesson was nine paid calls thrown away by a
  correct refusal in the wrong place, and this ordering would make a repeat discovery pay for a
  judgment before being refused. Identity excluding an engine-authored output is defensible on its
  own terms anyway, as argued above.
