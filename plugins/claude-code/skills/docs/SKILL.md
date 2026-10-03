---
name: docs
description: Show a menu of the documents a Requivo session's model can produce — for a software session the seven (decision brief, PRD, user stories, estimate, acceptance criteria, delivery epic, release notes), for a go-to-market session its go-to-market plan — each with a one-line purpose and whether it is up to date, needs updating, or has not been generated. Generate the ones the user picks, one or several, reasoning in this Claude session. Use when the user wants a document but does not know its name, or wants to see what is fresh.
allowed-tools: Bash(requivo doctor:*), Bash(requivo session list:*), Bash(requivo status:*), Bash(requivo artifact list:*), Bash(requivo model show:*), Bash(requivo context:*), Bash(requivo schema:*), Bash(requivo model apply:*), Bash(requivo model validate:*), Bash(requivo artifact save:*), Read
---

# /requivo:docs

Show what the model can produce and its freshness, then generate the picks — reasoning in *this*
Claude session, the same way `/requivo:brief` and the other generators do on their own. This
skill does not reason a document itself; it resolves the session, shows the menu, and runs the
matching generator skill's own steps for each pick. Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md`
unless you already hold it from an earlier `/requivo:*` in this conversation — and read it again
whenever you are unsure you still do.

## 1. Preflight
Run the shared **preflight** from REASONING.md before anything else: `requivo doctor --json`,
checking whether the command ran *at all* rather than what it reported. If it could not run, the
CLI is not installed — follow REASONING.md's missing-CLI flow. This skill reads and writes
nothing of its own, so there is no half-written document to find.

## 2. Split `$ARGUMENTS`: a session, document types, both, or neither
`$ARGUMENTS` may hold a session slug, one or more document names (or `all`), both, or nothing.
Split it on whitespace and commas. Run `requivo session list --json` once — the same resolution
`/requivo:status` and `/requivo:run` use:

- **The first token matches a `slug` among the rows.** That is the session; every remaining token
  is a document type (or `all`). If that row is `readable: false`, say so and point at
  `requivo session verify <slug>` — do not fall through to treating the slug as a document type.
- **No token matches a slug.** Resolve the default the same way `/requivo:status` does: exactly one
  `readable: true` row → that one; more than one → list them (slug, revision, `updated_at`) and ask
  which, by number — **never ask the user to type a slug**; none at all → say there is no session
  yet and point at `/requivo:run` instead, and stop. Every token in `$ARGUMENTS` is then a document
  type (or `all`).

With a slug in hand, read its row: if `revision` is `0`, the session has no model yet, and `status`
refuses it rather than reporting `0`. Say so and point at `/requivo:run <slug>` instead of showing a
menu, and stop. Otherwise run `requivo status <slug> --json` and note its `perimeter` (absent reads
as `software`): it decides which documents the session can produce.

## 3. The session's document types, in the order the user meets them
A perimeter produces its own documents and no other's (#719), the same split the CLI's
`requivo docs` and `artifact save` hold. The session's `perimeter` picks one row:

| Perimeter | Document types |
|---|---|
| `software` | `brief`, `prd`, `stories`, `estimate`, `criteria`, `epic`, `release` |
| `go-to-market` | `gtm_plan` |

The names are the CLI's own artifact types, in the same order every time. A perimeter this table
does not name has no documents in this plugin build: say so, point at `requivo docs <slug>`, and stop.
A document token from `$ARGUMENTS` is matched by name (case-insensitive, `gtm-plan` reads as
`gtm_plan`) or, from the menu below, by its row number. Refuse an unrecognised token, or one the
session's perimeter does not produce — name it back to the user with the session's perimeter and
list its valid names — **before running anything**; an unknown pick is not silently dropped.

## 4. No document type given: show the menu
Run `requivo artifact list <slug> --json` alongside the `status --json` from step 2. For each of
the session's types, in the order above, print one row: a number, its label, one sentence on what it is
for, and its state — read `stale` off the artifact list payload, **never compare revision numbers**
(a document reasoned from an older revision can still be exactly current; invariant 1):

| Type | Label | What it is for |
|---|---|---|
| `brief` | Decision brief | The judgment call to review before estimating or committing to scope |
| `prd` | PRD | The requirements document a dev team builds from |
| `stories` | User stories | The backlog, broken into shippable user stories |
| `estimate` | Estimate | Day-range estimates per story, reasoned from the stories above |
| `criteria` | Acceptance criteria | Given/When/Then acceptance criteria a client can sign off on |
| `epic` | Delivery epic | The delivery epic, ready for a tracker |
| `release` | Release notes | Client-facing release notes |
| `gtm_plan` | Go-to-market plan | The go-to-market plan to review before committing capacity to it |

State per row: **not generated** (the type is absent from the artifact list payload), **up to date
(rev N)** (present, `stale: false`), or **needs updating (from rev N)** (present, `stale: true`).

Ask which to generate — one, several (numbers or names), or `all`. **Never ask the user to type a
slug or a revision**; you already hold both.

## 5. Generate the picks
In the canonical order above, for each type the user picked (from `$ARGUMENTS` or the menu):

- **Read that type's own skill file** — `${CLAUDE_PLUGIN_ROOT}/skills/<type>/SKILL.md`, with `_`
  written `-` (`gtm_plan` is `skills/gtm-plan/SKILL.md`) — and follow its steps as if
  `/requivo:<type> <slug>` had been invoked directly, with the slug and revision you
  already resolved. This skill never repeats those rules; the referenced skill is the source, the
  same way `/requivo:estimate` already defers to `/requivo:stories` for its own reasoning rules.
- **A type that applies moves the revision.** `brief`, `prd` and `gtm_plan` write their reasoning or
  proposals into the model before saving; after one, re-read `revision` with
  `requivo status <slug> --json` and give the next type that one, so later documents build on it.
- **`estimate` absorbs `stories`.** If both are picked, generate `estimate` only — its own step 2
  reasons and saves the stories first, against the same revision as the estimate (invariant 6), so a
  separate `stories` run would write the same file twice against two different revisions. Drop
  `stories` from the pick list before generating and say once that it is included.

Each type still saves through the CLI exactly as `/requivo:<type>` does — this skill grows no second
save path.

## 6. Close with what was written, where, and one pointer
Name each document written and where `requivo artifact save`'s own output said it landed. Then:
**`/requivo:status` for where things stand.**
