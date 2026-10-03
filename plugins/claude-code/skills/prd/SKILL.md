---
name: prd
description: Generate a PRD from a Requivo session's model — the model plus what a senior PM would propose, each claim tagged by its source, unknowns kept visible and open decisions left open — write its proposals back into the model, and save it as a tracked artifact. Use when the user wants a Product Requirements Document from a discovered model.
allowed-tools: Bash(requivo doctor:*), Bash(requivo model show:*), Bash(requivo status:*), Bash(requivo model apply:*), Bash(requivo artifact save:*), Read
---

# /requivo:prd

Write a **PRD the way an expert would**: from the model, proposing where it leaves room, and labelling
every claim so a proposal is never read as the requester's word
(`decision: the-expert-proposes-and-labels`). **You** write it; Requivo tracks it. Read
`${CLAUDE_PLUGIN_ROOT}/REASONING.md` unless you already hold it from an earlier `/requivo:*` in this
conversation — and read it again whenever you are unsure you still do (its opening rule says why, and
which way to err).

## 0. Preflight
Run the shared **preflight** from REASONING.md before anything else: `requivo doctor --json`, checking
whether the command ran *at all* rather than what it reported. If it could not run, the `requivo` CLI
is not installed — follow REASONING.md's missing-CLI flow. Nothing has been applied or saved at this
point, so there is no half-written PRD to find.

## 1. Load the model
```
requivo model show <slug>
requivo status <slug> --json
```
Note the `revision` — call it `N`. It is the model this PRD reasons from.

## 2. Reason → write the PRD
Turn the model into a PRD (title, summary, problem, goals, users, in/out of scope, requirements with
priorities, workflow, business rules, permissions, integrations, data protection, edge cases,
acceptance criteria, assumptions, open questions, risks). The rules:

- **Propose where a senior PM would.** Where the model leaves room — how the core feature works,
  where data is hosted and who can read it, what happens to past results when a rule changes, what is
  recorded — state a concrete requirement or design tagged `[proposed]` with a one-line rationale.
  A regulatory or technical fact the request bears on is tagged `[domain]`.
- **Label every claim** from REASONING.md's *Source tags*, one legend line under the title. Untagged
  is never a way to pass an inference off as stated.
- **Unknowns stay visible.** An `empty` slot is an open question. An `inferred` one may carry a
  requirement only under its tag, never untagged as settled.
- **Open decisions stay open.** A proposal is not a silent resolution: an open decision or an
  unresolved challenge stays open, with your recommendation beside it. Never contradict a value the
  requester stated.
- **Traceability.** Each requirement names the topic(s) it rests on, by label, or "proposed" for one
  this PRD introduces — which step 3 then puts on a topic too.

## 3. Write the proposals back into the model
Follow REASONING.md, *a document that proposes writes the proposal back first*: every `[proposed]` or
`[domain]` claim this PRD introduces lands on its slot as `inferred` with a `proposed:` or `domain:`
evidence clause, so it shows up in the defaults list for veto and the PRD stays a view of the model:
```bash
requivo model apply <slug> - --expected-revision N --json <<'JSON'
{ "model": { … every slot, proposals folded in … }, "questions": [ … ], "summary": { … } }
JSON
```
Leave the reasoning keys out: the PRD does not speak to them. On a refusal, fix and apply again (a
refused apply wrote nothing); on `revision_conflict`, re-read and redo. Call the new revision `M`. No
new proposal, no apply: `M` is `N`.

## 4. Save it as a tracked artifact
Pass the PRD markdown in on stdin — no temp file:
```bash
requivo artifact save <slug> --type prd --file - --revision M --json <<'MD'
# … the PRD you just wrote …
MD
```
`--revision M` is the model the PRD describes, its proposals included. Read `stale` back: if it is
`true`, the model moved while you were writing — say so plainly and offer to regenerate. Relay the
apply's `stale_artifacts` too: a document resting on a slot a proposal filled is now behind.

## 5. Point at the next step, once
Close by telling the user what this document rests on, and that each `[proposed]` line is theirs to
overturn: `/requivo:run <slug>` folds an objection back in and announces what it reaches before
applying it, and `/requivo:status <slug>` names what needs updating from the dependency graph.
