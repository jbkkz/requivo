# `elicitation.md` and `model_schema.json` stay hand-kept

**Slug:** `elicitation-schema-hand-kept`

## Context

Each perimeter's `elicitation.md` is the human-readable spec; its `model_schema.json` is the machine
version fed to the model. CLAUDE.md asks whoever adds a slot to keep them consistent, by hand. #278
asked whether that needs a guard. Measured:

- `elicitation.md` names one slot by literal id (`config_vs_custom`); the pillar table uses short
  labels of its own ("Current process", not "Current process (as-is)"). The two were never equal
  strings — the difference is editorial, not drift.
- The same literal id appears in four context cards and `prompts/brief.md`, none guarded either.
- No incident: `git log` shows no disagreement between the two that went unnoticed.
- The only reader is `requivo schema --framework`, which prints it. Drift confuses one subcommand's
  prose; it corrupts no model, answer or artifact.

## Decision

No guard. The consistency stays hand-kept, and CLAUDE.md's Extending section says so. This is a cost
tradeoff with a threshold: one literal id, paraphrased prose, no incident, a one-screen blast radius.

## What breaking it cost

Nothing yet — which is the basis of the decision. A desync that misleads a reader of
`requivo schema --framework` is the cost that reopens it; name it here when it happens.

## Alternatives rejected

- **A containment test** (#278's proposal): fails on today's correct files, and passing it needs a
  hand-written label→id table — the same by-hand consistency moved into test code, red on a
  readability-only label edit.
- **A test scoped to `config_vs_custom`**: closes the smallest slice of a wider unguarded gap, with
  no incident behind it; the meta-guard budget asks for two.
- **Generate `elicitation.md` from the schema**: out of scope in #278 — a prose document meant to
  read naturally for a first-time human.
