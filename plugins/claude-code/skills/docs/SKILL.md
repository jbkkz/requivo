---
name: docs
description: Show a menu of the seven documents a Requivo session's model can produce — decision brief, PRD, user stories, estimate, acceptance criteria, delivery epic, release notes — each with a one-line purpose and whether it is up to date, needs updating, or has not been generated. Generate the ones the user picks, one or several, reasoning in this Claude session. Use when the user wants a document but does not know its name, or wants to see what is fresh.
allowed-tools: Bash(requivo:*), Read
---

# /requivo:docs

Show what the model can produce and its freshness, then generate the picks — reasoning in *this*
Claude session, the same way `/requivo:brief` and the other six generators do on their own. This
skill does not reason a document itself; it resolves the session, shows the menu, and runs the
matching generator skill's own steps for each pick. Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md`
unless you already hold it from an earlier `/requivo:*` in this conversation — and read it again
whenever you are unsure you still do.

## 1. Preflight
Run the shared **preflight** from REASONING.md before anything else: `requivo doctor --json`,
checking whether the command ran *at all* rather than what it reported. If it could not run, the
CLI is not installed — say the four things REASONING.md lists and stop. This skill reads and writes
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

With a slug in hand, run `requivo status <slug> --json`. If `revision` is `0`, the session has no
model yet: say so and point at `/requivo:run <slug>` instead of showing a menu, and stop.

## 3. The seven document types, in the order the user meets them
`brief`, `prd`, `stories`, `estimate`, `criteria`, `epic`, `release` — the same names the CLI's own
verbs use, and the same order every time. A document token from `$ARGUMENTS` is matched by name
(case-insensitive) or, from the menu below, by its row number. Refuse an unrecognised token —
name it back to the user and list the seven valid names — **before running anything**; an unknown
pick is not silently dropped.

## 4. No document type given: show the menu
Run `requivo artifact list <slug> --json` alongside the `status --json` from step 2. For each of
the seven types, in the order above, print one row: a number, its label, one sentence on what it is
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

State per row: **not generated** (the type is absent from the artifact list payload), **up to date
(rev N)** (present, `stale: false`), or **needs updating (from rev N)** (present, `stale: true`).

Ask which to generate — one, several (numbers or names), or `all`. **Never ask the user to type a
slug or a revision**; you already hold both.

## 5. Generate the picks
In the canonical order above, for each type the user picked (from `$ARGUMENTS` or the menu):

- **Read that type's own skill file** — `${CLAUDE_PLUGIN_ROOT}/skills/<type>/SKILL.md` — and follow
  its steps as if `/requivo:<type> <slug>` had been invoked directly, with the slug and revision you
  already resolved. This skill never repeats those rules; the referenced skill is the source, the
  same way `/requivo:estimate` already defers to `/requivo:stories` for its own reasoning rules.
- **`estimate` absorbs `stories`.** If both are picked, generate `estimate` only — its own step 2
  reasons and saves the stories first, against the same revision as the estimate (invariant 6), so a
  separate `stories` run would write the same file twice against two different revisions. Drop
  `stories` from the pick list before generating and say once that it is included.

Each type still saves through the CLI exactly as `/requivo:<type>` does — this skill grows no second
save path.

## 6. Close with what was written, where, and one pointer
Name each document written and where `requivo artifact save`'s own output said it landed. Then:
**`/requivo:status` for where things stand.**
