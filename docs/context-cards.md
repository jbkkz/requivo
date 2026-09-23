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

By default every card is loaded for every request, so cards dilute one another, and it is the most
expensive default (#257). Together the bundled cards are **the larger part of every call's system
prompt**, so each one you add costs every all-cards session. The weight is measured live rather than
written down here: `requivo discover` (when `--context` is not given) and the Web home page state
the average card size (`core.context.average_card_byte_size`, which ignores a Windows checkout's
CRLF).

Each added card weighs on every all-cards session and can blunt a neighbour's sharpest question:
adding `financial-reporting` cost `doc-reapproval` its sharpest question (3/3 golden runs down to
1/3). `requivo discover` names the cards it is about to reason over, with this note, whenever
`--context` is not given — before the paid call.

Scope a session to the cards that matter:

```bash
requivo run --context b2b-platform,financial-reporting "…"
```

The selection is held constant across the session's turns (so the cached system prompt survives) and
reused by the generators.

Every name is checked, on every turn, against the cards on disk. A misspelled card is refused rather
than dropped (an empty selection means *every* card), and so is an empty token:

```console
$ requivo discover --context b2b-platform, "…"
empty context card selector at position 1 — an empty token matches everything, so it would widen the
selection instead of narrowing it. Remove it (a stray comma is the usual cause), or pass no selector
at all to select everything deliberately.
```

A session scoped to a card in `REQUIVO_CONTEXT_DIR` carries that name in its `session.json`; on
another machine, or after a rename, it no longer resolves, and Requivo stops rather than reasoning
without it:

```console
$ requivo answer my-session "…"
unknown context card(s): acme-crm. Available: b2b-platform, document-management, event-ops, financial-reporting
```

**To recover, put the card back** — restore the file, or point `REQUIVO_CONTEXT_DIR` at wherever it now
lives — and the turn runs. Or re-scope away from it:
[`session rescope`](#re-scoping-an-existing-sessions-cards) records a new selection instead.

You do not have to wait for a paid turn to find out. Three offline checks answer it:

```bash
requivo doctor                     # lists every session whose saved cards no longer resolve
requivo session verify <slug>      # reports it for one session, and exits non-zero
requivo context --session <slug>   # prints the cards that session is actually reasoning with
```

These are environment findings, not integrity problems — see
[cli.md](cli.md#context-cards-a-session-can-no-longer-find).

## An install with no cards at all

Not *the card you named is missing*, not *we could not look*, but **we looked everywhere and there
is nothing** — a wheel or container layer that lost `assets/context/`:

```console
$ requivo discover --context b2b-platform "…"
no context cards are installed, so there is no product context to reason from — impact estimation is
the product's central idea and it runs on these cards. Looked in: … and …. This install is
incomplete: reinstall requivo, or point REQUIVO_CONTEXT_DIR at a directory holding your cards.
```

It is refused when the selection is validated, at session creation (#41), and in Requivo Web it is a
500 rather than a 400 — nothing you sent caused it
([compatibility.md](compatibility.md#http-statuses-in-requivo-web)). With no `--context`, it is caught
when the cards are loaded.

A card directory that exists but **cannot be read** is `context_unreadable`, never a missing card:
the remedies are opposite (fix permissions, not restore a file).

## A fourth state nothing detects: cards about the wrong product

`ok`, `empty` and `unreadable` are decidable from the filesystem. **Present, readable, and about a
different product** is not, and renders identically to `ok`: a session grounded on
`financial-reporting` while reasoning about a CI matrix still reaches *ready*, asking duller questions
(#489). **There is no `context.status: mismatched`** (#492): relevance needs either a paid, fallible
call inside the free deterministic preflight, or a keyword heuristic that is wrong silently.

So **every surface hands the fact to a human**: `discover` names the cards before it spends,
`status` and `session show` name what a session was grounded on, the session page states it on the
primary screen, and the Claude Code skill names it back. `tests/test_grounding_contract.py` asserts
that property. At a **first** discovery, the engine now also judges whether an installed card covers
the request's domain (`decision: the-engine-writes-the-missing-card`) — a paid call, shown to you, not
a preflight verdict. Narrowing afterwards is [`requivo session rescope`](#re-scoping-an-existing-sessions-cards).

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

- **It records a new revision once the session has a model** — same model and hash, `surface:
  session-rescope`; at revision 0 it only updates the metadata.
- **Nothing already produced is marked stale**: context is not a dependency edge
  ([session-format.md](session-format.md#artifacts-and-freshness)). Only the *next* turn changes.
- **`--context` is required**; pass `--context ""` to reset to every card deliberately.

Hand-editing `context_cards` in `session.json` still works (the layout is a published contract), but
`session rescope` validates and leaves provenance. A card name carrying a control character is refused
as `unsafe_selector_token` ([cli.md](cli.md#a-card-name-cannot-write-a-line-of-the-receipt), #40).
