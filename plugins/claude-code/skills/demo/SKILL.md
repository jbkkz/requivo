---
name: demo
description: Replay a real Requivo run from saved output, ending on what changing one answer makes stale. Offline, no API key, no session, nothing written. Use when the user wants to see what Requivo does before giving it a real request, or asks for a demo or a first thing to try.
allowed-tools: Bash(requivo doctor:*), Bash(requivo demo:*), Read
---

# /requivo:demo

Show the product's best first minute: a real run, replayed from output saved inside the package. This
is a **deterministic replay** — do not analyse anything with Claude and do not invent detail; run the
command and relay what it printed.

Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md` unless you already hold it from an earlier `/requivo:*` in
this conversation, and read it again whenever you are unsure you still do.

## Preflight
Start with the shared **preflight** in REASONING.md: run `requivo doctor --json` and check whether
the command ran *at all*, not what it reported. If it could not run, the CLI is not installed —
follow REASONING.md's missing-CLI flow. A demo is the cheapest place to meet that flow, since nothing
is at stake. This skill creates no session and writes nothing, so there is nothing to undo.

## Run
```
requivo demo
```
Add nothing: it takes no argument, needs no `ANTHROPIC_API_KEY` and no network, and creates no
session. It reads only what ships in the package and prints to the terminal.

## Relay
The output has five numbered beats. Present them in order, in plain language, in the vocabulary the
user reads elsewhere (*what we know*, *what we are assuming*, *open question*, *needs updating*) and
without slot ids:

1. **The request**: a rambling, multi-feature client email, as it arrived.
2. **What the engine made of it**: what the request states, what was assumed, what is open.
3. **The decision brief**: a judgment on the model, not a recap of the email. Give it a few lines.
4. **Change one answer**: the six-week deadline moves. This is the point of the demo, so give it the
   most room.
5. **Everything else is a view of the same model**: one line, no more.

**End on beat 4, not on the brief.** The last thing the user reads is what the changed answer
reaches: the decisions to re-validate, the premises to re-examine, and the documents that go stale,
by name. Say that it was computed from the recorded dependency graph, not generated, so the same
change gives the same answer every time. Do not close on the brief or on the command's own
"keep going" lines.

## Then point at the next step, once
The natural next move is a real request: `/requivo:run` with the user's own text. Say once that it
reasons in this Claude session with no API key. One pointer, not a menu.
