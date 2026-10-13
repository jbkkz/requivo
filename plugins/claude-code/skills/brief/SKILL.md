---
name: brief
description: Produce the decision brief from a Requivo session's current understanding, using this Claude session for the judgment, and save it as a tracked document tied to the revision it was written from. Use when the questions have been worked through and the user needs something to review before estimating or committing to the scope.
allowed-tools: Bash(requivo doctor:*), Bash(requivo status:*), Bash(requivo model show:*), Bash(requivo context:*), Bash(requivo model apply:*), Bash(requivo model validate:*), Bash(requivo artifact save:*), Read
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
is not installed — follow REASONING.md's missing-CLI flow. Nothing has been applied or saved
at this point, so there is no half-written brief to find.

## 1. Check readiness, honestly
```
requivo status <slug> --json
```
Note the `revision` — call it `N`. It is the understanding this brief will rest on, and you will state it
when you save.

If high-impact topics are still unresolved, the page names each one under *Still open* (step 3) as a
question in the client's terms, never as a readiness label. A brief written on a thin understanding
must flag its assumptions — never present an inferred topic or an open decision as settled.

## 2. Load the model
```
requivo model show <slug>
requivo context --session <slug>    # exactly the cards this session was created with
```

## 3. Reason → write the one-page brief
The brief is the page the client reads (#814): **one page of prose**, one recommendation, each thing
said **once**. Reason through the whole model first: what the client stated, what was inferred, the
defaults chosen on their behalf (a `let_stand` claim, or a topic whose `evidence` opens `Client
delegated:`), the evidence and its limits, the options set aside, the riskiest assumptions and the
signal that would end the case, the premises worth contesting. Then write only the page:

1. **The call and the recommendation** — a title naming the call to make, then one short paragraph:
   the decision the client has to make and what you recommend. One recommendation, not a menu.
2. **Why** — one short paragraph, grounded in what the client said and the constraints the model states.
3. **Cost and risk** — one short paragraph: the main driver of effort and the risk that matters most.
4. **Still open, and would change this** — each open point whose answer would change the
   recommendation, as a question in the client's terms with how it would change it. Every topic
   step 1 reports as blocking belongs here; a point whose answer changes nothing does not.
5. **This recommendation assumes** — what it rests on that the client has not confirmed, each once,
   under REASONING.md's *Source tags*: a default you chose `[proposed]`, an inference `[assumed]`, a
   domain fact `[domain]`, the client's own word `[requester]`. Never promote an assumption to a fact
   because it sounds settled.

The page carries no readiness verdict or label ("Draft", "Not ready", "Ready"), no note on how it was
produced, no headings beyond the five lead-ins, no topic labels in bold before a value, and never
says one thing in two places: an assumption named under 5 is not restated under 2 or 4. A reader
finishes it in two minutes. The analysis behind it is not lost: step 4 writes it into the model,
where every later document reads it.

**Language.** Write the whole page — the title and the lead-ins included — in the session's
`docs_language` from the step-1 `status` (REASONING.md, *Every document is written in the session's
documents language*), so the page never mixes an English frame with the client's words.

**Every opportunity is a premise too** (#749): "this would be valuable". Put it through the check a
challenge gets: what would have to be true, and what if it is not? One that survives with a material
caveat carries the caveat in its text; one that does not survive becomes a challenge instead.

An option is set aside only because it loses to something the model states (a constraint, the
capacity, a decision), and a kill signal only where the model grounds a number or a concrete trigger.
Never invent either to fill a list: a forced one is worse than none.

Voice rule: no slot ids, no percentages, no confidence labels in the prose (the source tags are not
confidence labels). Say the business thing.

## 4. Fold the reasoning back into the model — do not skip this
The prose is the *view*. The **reasoning behind it is part of the model**, and every later generator
(PRD, epic, criteria, release notes) is prompted with the model, so a PRD written after this brief
should inherit the decisions and challenges you just made — not rediscover them.

Take the model from step 2, fold in the proposals the brief introduced (REASONING.md, *a document
that proposes writes the proposal back first*), and add the structured form of your reasoning:

```bash
requivo model apply <slug> - --expected-revision N --json <<'JSON'
{
  "model": { … as it was, proposals folded in … },
  "questions": [ … ],
  "summary": { … },
  "decisions":     [{"decision": "…", "why": "…", "alternative": "…", "tradeoff": "…",
                     "derived_from": ["<slot ids the decision rests on>"], "source": "requester|proposed"}],
  "challenges":    [{"headline": "…", "premise": "…", "alternative": "…", "consequence": "…",
                     "recommendation": "…", "contests": ["<slot ids whose premise this contests>"]}],
  "opportunities": [{"text": "…", "leverage": "high|medium|future", "modules": ["…"]}],
  "exclusions":    [{"option": "…", "reason": "…", "rests_on": ["<slot ids the cut rests on>"]}],
  "thresholds":    [{"condition": "<the kill signal>", "measure": "<what the cheap test reads>",
                     "action": "<stop, or what changes>", "rests_on": ["<slot ids holding the assumption>"]}]
}
JSON
```

This is the analysis behind the page, stated structurally: the options you set aside are
`exclusions`, the kill signals are `thresholds`, the premises worth contesting are `challenges`. Emit
all five lists, `[]` for an empty one: a new brief replaces the last one's items, including a
challenge a discovery turn recorded (#777) — restate the ones that still stand. A decision's `source`
says who owns it (#751): `requester` for their own choice,
`proposed` for your recommendation. `derived_from`, `contests` and `rests_on` are the dependency
edges: they are what lets Requivo tell the user *which* later change unseats *which* decision, option
set aside or kill signal. An item with no edges still records fine, but it can never be reported as
invalidated; a threshold needs at least one.

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
