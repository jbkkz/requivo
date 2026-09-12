# Context cards

> The engine is domain-agnostic; the context makes it smart. Better context → sharper impact estimates
> → better questions. Measure the effect with the [golden harness](evaluations.md).

A context card is a Markdown file describing a product, its entities, and its recurring traps. Impact —
the driver behind which questions get asked — is estimated from these cards.

## Add a card (from a clone)

The built-in cards live in the package at `src/requivo/assets/context/`. Working from a clone (or an
editable `pip install -e .`), drop a card there:

```text
src/requivo/assets/context/
  hris.md        ← HR / people platforms
  crm.md         ← sales & pipeline tools
  erp.md         ← finance & operations suites
  my-product.md  ← yours
```

Files prefixed with `_` (e.g. `_template.md`) are ignored. Copy `_template.md` to start.

## Add a card (pip install, no checkout)

Drop cards in a user directory — no need to touch the package:

```bash
export REQUIVO_CONTEXT_DIR=~/.config/requivo/context   # also the default location
mkdir -p "$REQUIVO_CONTEXT_DIR" && $EDITOR "$REQUIVO_CONTEXT_DIR/my-product.md"
```

User cards merge with the built-ins; a user card whose name matches a built-in **overrides** it, so you
can tweak a bundled card without editing the package.

## Scoping a session to relevant cards

By default every card is loaded for every request, so cards can dilute one another — and it is also
the most expensive default (#257). Measured against the 4 cards this repository bundles today
(`core.context.card_byte_size` on `src/requivo/assets/context/*.md`, excluding `_template.md` — not
`wc -c`, which counts the CRLF line endings a Windows checkout has and the loader never sees):
23,262 bytes, ~5.8k tokens at 4 bytes/token, split roughly evenly across the four
(3,842 / 5,576 / 6,995 / 6,849 bytes). Folded
into an assembled system prompt (`build_prompt()`, offline, no API call), that is **64–78% of every
call's system prompt**, across the eight generator prompts measured on 2026-09-12 — lowest for
`brief` (36,069 bytes total, cards are 64.5%), highest for `estimate`/`release` (~29.8–30.0k bytes
total, cards are ~78%). Across a full pipeline of roughly nine calls, cards alone account for tens of
thousands of input tokens per session that never opted into `--context`.

A card added later only makes this worse: it adds weight to every future all-cards session and can
measurably blunt a neighbour's sharpest question. The one measured instance: adding
`financial-reporting` to the bundled set cost `doc-reapproval` its sharpest question (3/3 golden runs
down to 1/3), displaced by that card's audit-trail emphasis — see the "Known limit" note in the
repository's `CLAUDE.md`. `requivo discover` prints which cards it is about to reason over, and this
same dilution note, whenever `--context` is not given — before the paid call, not after.

Scope a session to the cards that matter:

```bash
requivo discover --context b2b-platform,financial-reporting "…"
```

The selection is held constant across the session's turns (so the cached system prompt survives) and
reused by the generators.

Every name is checked, on every turn, against the cards actually on disk — not only when the session is
created. A misspelled card is refused rather than dropped, because dropping it leaves an empty
selection and an empty selection means *every* card; a selection that resolves to nothing is refused
for the mirror-image reason, because an empty context is not visibly different from a good one:

```console
$ requivo discover --context b2b-platform, "…"
empty context card selector at position 1 — an empty token matches everything, so it would widen the
selection instead of narrowing it. Remove it (a stray comma is the usual cause), or pass no selector
at all to select everything deliberately.
```

The case that costs the most is the one you did not type. A session scoped to a card that lives in
`REQUIVO_CONTEXT_DIR` carries that name in its `session.json`; open the same session on another
machine, or rename the card, and the name no longer resolves. Requivo says so and stops:

```console
$ requivo answer my-session "…"
unknown context card(s): acme-crm. Available: b2b-platform, document-management, event-ops, financial-reporting
```

That is deliberately a refusal rather than a quiet fallback to no context at all. Impact estimation is
what decides which questions get asked, it is estimated from these cards, and a turn that runs without
them produces a plausible answer for a reason nothing on screen would have shown you.

**To recover, put the card back** — restore the file, or point `REQUIVO_CONTEXT_DIR` at wherever it now
lives — and the turn runs. Or re-scope away from it:
[`session rescope`](#re-scoping-an-existing-sessions-cards) records a new selection instead.

You do not have to wait for a paid turn to find out. Three offline checks answer it:

```bash
requivo doctor                     # lists every session whose saved cards no longer resolve
requivo session verify <slug>      # reports it for one session, and exits non-zero
requivo context --session <slug>   # prints the cards that session is actually reasoning with
```

`doctor` reports it under `sessions.unresolved_cards`, and `session verify` in a `context_cards`
block beside the session's integrity problems — beside, and not among, because a card lives outside
the session directory: the same session is fine on a machine that has the card. See
[cli.md](cli.md#context-cards-a-session-can-no-longer-find) for why that distinction is load-bearing
for `session import`.

## An install with no cards at all

The third state, and the one worth telling apart from the other two: not *the card you named is
missing* and not *we could not look*, but **we looked, at every root, and there is nothing**. A wheel
or container layer that ships `assets/` and loses `assets/context/` produces it.

```console
$ requivo discover --context b2b-platform "…"
no context cards are installed, so there is no product context to reason from — impact estimation is
the product's central idea and it runs on these cards. Looked in: … and …. This install is
incomplete: reinstall requivo, or point REQUIVO_CONTEXT_DIR at a directory holding your cards.
```

That refusal now arrives when the selection is **validated**, at session creation, and not only when
the cards are first read a turn later (#41). It used to answer *unknown context card: b2b-platform*
at creation — technically true, since with nothing installed every name is unknown, and the wrong
remedy: it sent you to check a name you had typed correctly. In Requivo Web the same shift moves the
condition from a 400 to a 500, which is the honest side of that line — nothing you sent caused the
install to arrive without cards. See [compatibility.md](compatibility.md#http-statuses-in-requivo-web).

Passing no `--context` at all is unaffected: that is not a selection, so there is nothing to
validate, and the install is caught at the point the cards are actually loaded.

A card directory that exists but **cannot be read** is reported separately, as
`context_unreadable`, and never as a missing card. The two have opposite remedies — restore the file
versus fix the permissions — and they used to be indistinguishable, because `Path.glob` yields
nothing rather than raising when a directory is denied: the card vocabulary came back quietly short,
so a card sitting in that directory was reported as unknown and you were told to put back a file that
was already there.

## A fourth state nothing detects: cards about the wrong product

`ok`, `empty` and `unreadable` are the three the preflight tells apart. There is a fourth —
**present, readable, and about a different product entirely** — and it renders identically to `ok`.

It matters more than it sounds. `information_value = uncertainty × impact` is the whole driver, and
the cards are what the impact half is read against, so a session grounded on `financial-reporting`
while reasoning about a CI matrix produces a model, reaches *ready*, and asks duller questions for a
reason nothing on screen names. An external validation report ran exactly that experiment (#489):
the engine transferred well and the grounding said nothing about itself.

**There is no `context.status: mismatched`, and this is a decision rather than a gap (#492).** Every
other value of that vocabulary is decidable from the filesystem. Relevance is not. Automating it
needs either a model call inside the free deterministic preflight — putting a paid, fallible
judgment in front of the one path whose whole value is that it is decidable — or a keyword
heuristic, which is the combination that is right often enough to be trusted and wrong silently.

So **the human is the detector, and every surface hands them the fact.** `requivo discover` names
the cards it is loading before it spends anything, `requivo status` and `requivo session show` name
what the session was grounded on, the session page states it on the primary screen rather than only
under *Traceability details*, and the Claude Code skill names them back at step 7.
`tests/test_grounding_contract.py` is the guard, and it asserts a property rather than a wording: a
surface that renders a session's state names what that state was reasoned against, and says
something different when the grounding was never narrowed.

**What would change this:** a third *measured* instance of a card diluting its neighbour. Two are on
record — the `financial-reporting` / `doc-reapproval` measurement in the golden harness's own
known-limit note, and #489 — and the third is what funds automatic relevance routing, per the bar
`CLAUDE.md` applies to itself. Until then, narrowing is a human act and
[`requivo session rescope`](#re-scoping-an-existing-sessions-cards) is how it is done.

## Re-scoping an existing session's cards

`requivo session rescope <slug> --context <cards>` changes an existing session's selection. It
validates the selection the same way `session init` does — an unknown name is refused, not recorded
— and it is the recovery for a card that only exists on one machine: re-scope away from it instead of
restoring the file.

```console
$ requivo session rescope leave-approval --context b2b-platform
✅ Re-scoped 'leave-approval' → revision 3
  previous  acme-crm
  now       b2b-platform
  Turns already reasoned were reasoned under the previous selection and are untouched; the next
  turn reasons against the new one.
```

Three things follow from what a re-scope is, and are not obvious from the command alone:

- **It records a new revision, once the session has a model** — the model carries forward unchanged
  (same content, same hash), and the revision's `surface` reads `session-rescope` rather than a
  reasoning turn, so `session.json`'s own history shows exactly where the selection changed. Before
  any model exists there is nothing yet whose provenance the old cards could describe, so re-scoping
  a brand-new session only updates the metadata — no revision spent on content that was never there.
- **Nothing already produced is touched.** Existing artifacts are not marked stale: they still
  faithfully describe the model they were generated from, and context is not one of the dependency
  edges that invalidate an artifact (see [session-format.md](session-format.md#artifacts-and-freshness)).
  Only the *next* turn reasons against the new selection.
- **`--context` is required.** Unlike `session init`, where leaving it off is the ordinary default
  (every card), here the whole point of the command is a deliberate new selection — pass `--context
  ""` to reset one explicitly back to every card.

Hand-editing the `context_cards` key in `session.json` is no longer the documented path — the layout
is still a published contract (see [session-format.md](session-format.md)), so it keeps working, but
`session rescope` validates the selection for you and leaves a provenance record hand-editing does
not. A card name may not contain a control character — a newline, a tab, an escape sequence — and one
that does is refused as `unsafe_selector_token` rather than displayed, because a name rendered into a
health receipt can otherwise end the line and write its own
([cli.md](cli.md#a-card-name-cannot-write-a-line-of-the-receipt), #40). Card stems are filenames, so
no name you would actually type is affected — the case worth knowing about is a `session.json` edited
directly, since `session rescope` can never produce one.
