---
name: brief
description: Produce the decision brief from a Requivo session's current understanding, using this Claude session for the judgment, and save it as a tracked document tied to the revision it was written from. Use when the questions have been worked through and the user needs something to review before estimating or committing to the scope.
allowed-tools: Bash(requivo:*), Read
---

# /requivo:brief

Write the **decision brief** — what someone needs to review with a client, a product lead or an
engineering lead *before* estimating this request. A judgment, not a recap, and not a PRD. **You** do
the analysis; Requivo tracks the document. Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md` unless you already hold it from an earlier
`/requivo:*` in this conversation — and read it again whenever you are unsure you still do (its
opening rule says why, and which way to err).

## 0. Preflight
Run the shared **preflight** from REASONING.md before anything else: `requivo doctor --json`, checking
whether the command ran *at all* rather than what it reported. If it could not run, the `requivo` CLI
is not installed — say the four things REASONING.md lists and stop. Nothing has been applied or saved
at this point, so there is no half-written brief to find.

## 1. Check readiness, honestly
```
requivo status <slug> --json
```
Note the `revision` — call it `N`. It is the understanding this brief will rest on, and you will state it
when you save.

If high-impact topics are still unresolved, **say so up front**. A brief written on a thin
understanding must flag its assumptions — never present an inferred topic or an open decision as
settled.

## 2. Load the model
```
requivo model show <slug>
requivo context --session <slug>    # exactly the cards this session was created with
```

## 3. Reason → write the brief
Produce it in PM language, in the order a scope review is run:

1. **Request and objective** — the underlying problem, what is being built, complexity and its cost driver.
2. **Current understanding** — the scope in a short paragraph.
3. **What is confirmed** — the topics the client actually stated, each with its value.
4. **Important assumptions** — the topics that were *inferred*, each marked as needing confirmation.
5. **Decisions made** — settled choices, with the alternative weighed and the trade-off accepted.
6. **Scope implications** — what this genuinely introduces into the system.
7. **Assumptions worth contesting** — premises worth challenging *before* build. This is the differentiator.
8. **Main risks**, **unresolved questions**, **opportunities**.
9. **Are we ready?** — ready, or the topics that are still unconfirmed and can move the solution.
10. **Recommended next steps.**

Sections 3 and 4 are read off the model, not invented: a topic whose evidence is `explicit` is
confirmed, one that is `inferred` is an assumption. Do not promote an assumption to a fact because it
sounds settled — that distinction is the most useful thing on the page.

Keep it shorter than a PRD. The question it answers is "what do I need to review before estimating
this?", not "what should be built".

Voice rule: no slot ids, no percentages, no confidence labels in the prose. Say the business thing.

## 4. Fold the reasoning back into the model — do not skip this
The prose is the *view*. The **reasoning behind it is part of the model**, and every later generator
(PRD, epic, criteria, release notes) is prompted with the model, so a PRD written after this brief
should inherit the decisions and challenges you just made — not rediscover them.

Take the model from step 2 unchanged and add the structured form of your reasoning:

```bash
requivo model apply <slug> - --expected-revision N --json <<'JSON'
{
  "model": { … exactly as it was … },
  "questions": [ … ],
  "summary": { … },
  "decisions":     [{"decision": "…", "why": "…", "alternative": "…", "tradeoff": "…",
                     "derived_from": ["<slot ids the decision rests on>"]}],
  "challenges":    [{"headline": "…", "premise": "…", "alternative": "…", "consequence": "…",
                     "recommendation": "…", "contests": ["<slot ids whose premise this contests>"]}],
  "opportunities": [{"text": "…", "leverage": "high|medium|future", "modules": ["…"]}]
}
JSON
```

These are the same items as in your prose, stated structurally. `derived_from` and `contests` are the
dependency edges: they are what lets Requivo tell the user *which* later change unseats *which*
decision. A decision with no edges still records fine, but it can never be reported as invalidated.

Note the revision the apply returns — call it `M`. Slots may not have moved at all; the reasoning is
the change, and Requivo tracks it as one.

## 5. Save the document as a tracked artifact
```bash
requivo artifact save <slug> --type brief --file - --revision M --json <<'MD'
# … the decision brief you wrote in step 3 …
MD
```
`M` is the revision the apply just created — the understanding the brief actually describes, reasoning
included. (If you skipped step 4, use `N` from step 1: the honest revision is whichever one you truly
reasoned from, never simply the latest.)

The brief is a judgment over the whole understanding, so any later material change to it flags the saved
copy stale. Read `stale` back from the save output: if it is `true`, the model moved while you were
writing — tell the user plainly that the brief is already behind and offer to redo it.

## 6. Point at the next step, once
The brief is what a scope review is run from, so once it has been reviewed and the scope is agreed,
`/requivo:prd <slug>` renders the same understanding for build — no second discovery, the same model
seen from a different angle. If the review reopened something instead, `/requivo:run <slug>` is
where that goes.
