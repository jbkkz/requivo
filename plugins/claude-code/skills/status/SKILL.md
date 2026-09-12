---
name: status
description: Show a Requivo session's readiness, blocking slots, current revision, and artifact freshness. Pure deterministic read — no reasoning, no API key. Use when the user asks where a session stands or what is still blocking it.
allowed-tools: Bash(requivo:*), Read
---

# /requivo:status

Report where a session stands. This is a **deterministic read** — do not re-analyse the request with
Claude; just run the command and translate the result.

## Preflight
This is often the first Requivo command a new user runs, and the `requivo` CLI is a **separate
install** from the plugin. So start with the shared **preflight** in
`${CLAUDE_PLUGIN_ROOT}/REASONING.md`: run `requivo doctor --json` and check whether the command ran
*at all* — not what it reported. If it could not run, the CLI is not installed; say the four things
REASONING.md lists and stop. This skill reads and writes nothing, so there is nothing to undo.

## Run
`$ARGUMENTS` is the session slug. Run:
```
requivo status <slug> --json
```
(Add nothing else — the numbers come from Core, not from you.)

## Relay
Translate the JSON into plain language, in the vocabulary the user reads elsewhere — *what we know*,
*what we are assuming*, *open question*, *needs updating*. Slot ids are the wire format, not something
to show:

- **Are we ready?** Two states, no invented middle ground: *ready for a first decision brief*, or
  *not ready* — and if not ready, name what is still unresolved by its `label`, never the raw id.
- **Revision**: the session's history position. Mention it only if the user asks or if it matters.
- **Documents**: for each, whether it is up to date or **needs updating**. "Needs updating" means the
  understanding has moved in a way that actually touches what that document was built from. An older
  source revision on its own does *not* mean it needs updating — that number is provenance (where it
  came from), not a verdict. Report the `stale` flag; never infer it from the revision.

Be clear about what is still unresolved. If the user wants the full per-topic checklist and every open
question, run `requivo status <slug>` without `--json` and show that view. Do not invent detail the
command did not return.

## Then point at the next step, once
This skill answers *where are we*; the user's next move follows from the answer, so name one and stop
there. Not ready, with open questions: `/requivo:run <slug>` folds their answers in. Ready, with no
decision brief yet: `/requivo:brief <slug>`. Something marked as needing an update: the *needs
updating* lines above already say what it reaches; `/requivo:run <slug>` is where the change goes
in, and it announces the blast radius before applying. One pointer, not a menu.
