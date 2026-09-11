---
name: prd
description: Generate a PRD from a Requivo session's model — a view of the model, with unknowns kept visible and open decisions left open — and save it as a tracked artifact. Use when the user wants a Product Requirements Document from a discovered model.
allowed-tools: Bash(requivo:*), Read
---

# /requivo:prd

Generate a **PRD as a view of the model** — not new invention. **You** write it from the model; Requivo
tracks it. Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md` unless you already hold it from an earlier
`/requivo:*` in this conversation — and read it again whenever you are unsure you still do (its
opening rule says why, and which way to err).

## 0. Preflight
Run the shared **preflight** from REASONING.md before anything else: `requivo doctor --json`, checking
whether the command ran *at all* rather than what it reported. If it could not run, the `requivo` CLI
is not installed — say the four things REASONING.md lists and stop. Nothing has been saved at this
point, so there is no half-written PRD to find.

## 1. Load the model
```
requivo model show <slug>
requivo status <slug> --json
```
Note the `revision` — call it `N`. It is the model this PRD will rest on, and you will state it when
you save.

## 2. Reason → write the PRD
Turn the model into a PRD (title, summary, problem, goals, users, in/out of scope, requirements with
priorities, workflow, business rules, permissions, integrations, edge cases, acceptance criteria,
assumptions, open questions, risks). Rules that keep it faithful to the model:

- **Unknowns stay visible.** A slot that is empty or `inferred` becomes an explicit *assumption* or an
  *open question* in the PRD — never a stated requirement.
- **Open decisions stay open.** Do not silently resolve a decision the model left open.
- **Traceability.** Each requirement should be traceable to the slot(s) it comes from; keep them
  grounded in the model, not added from outside it.

## 3. Save it as a tracked artifact
Pass the PRD markdown in on stdin — no temp file:
```bash
requivo artifact save <slug> --type prd --file - --revision N --json <<'MD'
# … the PRD you just wrote …
MD
```
`--revision N` is the revision you read in step 1 — the model you actually reasoned from. Writing a
PRD takes a while, and if the session moved in between, Core compares the two and records the PRD
stale rather than filing a superseded document as current.

The PRD is now tied to the model revision it was written from, so a later change to any slot it rests
on flags it stale (`requivo status` will show it). Read `stale` back from the save output: if it is
`true`, the model moved while you were writing — say so plainly and offer to regenerate.

## 4. Point at the next step, once
Close by telling the user how to find out what this document rests on: `/requivo:impact <slug>` reads
the dependency graph and says which change would reach the PRD, so a later edit is answered rather
than guessed at. If the understanding has already moved and they want to know what else is behind,
`/requivo:status <slug>`.
