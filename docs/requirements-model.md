# The requirements model

> The model is the product; documents are views of it. This is what the model is made of and how
> Requivo reasons over it.

## The product vocabulary

These are the names the product uses, in Requivo Web and in the documents it writes. The rest of this
page is the same ideas in the engine's own, more precise vocabulary — which is what `--json` and the
technical docs speak.

- **What we know** — stated directly by the client.
- **What we are assuming** — inferred from context; confirm before building.
- **Open question** — not yet known, and worth asking when the answer would move the build.
- **How we know it vs how fully** — whether something was stated or inferred is separate from whether
  it has been covered in enough detail. Both have to hold before a topic stops blocking.
- **Decision and assumption to review** — a settled choice with its trade-off; a premise worth
  contesting before build.
- **What rests on what** — a decision records the topics it was derived from; a document records the
  topics it consumes. That graph is what makes "needs updating" an answer rather than a guess.
- **Are we ready?** — whether a high-impact topic is still unresolved.
- **Needs updating** — a document the understanding has moved past. `requivo impact` shows a change's
  blast radius before you make it.

| The product says | The engine says |
|---|---|
| what we know / what we are assuming | evidence (`explicit` / `inferred` / `unknown`) |
| how fully a topic is covered | coverage (completeness) |
| topic | slot |
| assumption to review | challenge |
| needs updating | stale artifact |
| decision brief | `brief` |

## Slots and pillars

The model is a set of typed **slots** — the problem, actors, business objects, business rules,
workflow, permissions, edge cases, constraints, and more — grouped into four areas:

- **Why** — the problem, current process, success criteria
- **What** — actors, business objects, business rules, workflow
- **How** — permissions, integrations, constraints, config-vs-custom
- **Validate** — acceptance, edge cases, risks

Each slot carries a value plus its **evidence** and **coverage** (below). Every artifact is a render of
this same filled model.

## The driver: information value = uncertainty × impact

Requivo does **not** ask because a slot is empty — it asks where an answer would change the solution.
Empty-but-low-impact slots are left alone; filled-but-risky ones get probed. **Impact is estimated
from the product context**, so the engine is only as sharp as the [context cards](context-cards.md)
it's given.

## Evidence vs coverage

Two independent signals, deliberately not collapsed:

- **Evidence** — *how we know* a slot: `explicit` (stated by the client), `inferred` (assumed from
  context), or `unknown`.
- **Coverage** — *how fully* a slot is covered (its completeness). A slot can be `explicit` yet thinly
  covered — stated in one word. That still blocks readiness; it reads as "partial", not "confirmed".

## Decisions, challenges, opportunities

The assessment layer, persisted into the model so every generator inherits it:

- **Decision** — a settled choice, with its *why*, the *alternative* weighed, and the *trade-off*
  accepted.
- **Challenge** — a contested premise: the assumption the request takes for granted, a concrete
  alternative, the consequence, and a recommendation. This is the differentiator — it pushes back on
  the request rather than organising it.
- **Opportunity** — a leverage point, ranked, naming the modules it reaches.

## Dependencies and staleness

The model is not a flat snapshot — its parts rest on each other:

- A decision records the slots it was **derived from**; a challenge records the slots it **contests**.
- Each buildable artifact records the slots it **consumes**.

So a change knows its blast radius. `requivo impact <slug> <slots>` shows the decisions to re-validate
and the artifacts that would go **stale**; a discovery turn that materially moves the model flags the
already-generated files that no longer match it. An unrelated (or completeness-only) change leaves an
artifact fresh — staleness follows the dependency graph, not the revision number.

Every required slot is guaranteed to reach at least one artifact's staleness check — a specific one
(prd, stories, estimate, criteria, epic, release) when it shapes that artifact's content, or the
solution assessment's judgment over the whole model when it does not. A slot reaching neither used to
be possible without anyone noticing: nothing marked the specific artifacts stale when it changed, only
the assessment. A test now catches it — see CLAUDE.md's "Adding a slot" checklist when introducing a
new one.

## Readiness

Readiness is binary: a high-impact slot must be both `explicit` **and** covered above the soft
boundary to stop blocking the build. A high-impact gap — empty, unknown, or stated-but-thin — keeps a
session out of "ready". Requivo does not invent graded "nearly ready" levels; it shows what blocks.

## The language of the outputs

A request often arrives in the client's own language — a forwarded French or Spanish email. Requivo
splits its outputs in two, because the two halves have different readers:

- **The conversation mirrors the request.** The questions Requivo asks back, and the understanding it
  renders every turn — the objective, the likely scope, the assumptions and the least-explored area —
  are written in the language the request arrived in. They are read by the person who received that
  email and who has to take the questions back to their client, so mirroring is what makes them
  usable. Requivo mirrors; it does not translate the request into English and answer in English.
- **The buildable artifacts anchor English.** The decision brief, the PRD, the stories, the
  acceptance criteria, the epic and the release notes are written in English whatever language the
  request was in. They feed dev teams and trackers — a GitHub or GitLab issue body, a backlog, a
  spec a build team reads — and those are English-speaking destinations by default. The decision
  brief sits on this side rather than with the conversation because its reasoning is folded into the
  model and every later generator is prompted with that model: a brief in the request's language
  would carry that language into all five artifacts downstream of it.

**The saved decision brief is the one artifact this split does not cleanly divide, and it is
bilingual on purpose rather than by accident.** `brief_markdown` is the only writer that receives an
`EngineOutput` as well as its contract, and half of what it emits is a *projection* of the model:
the objective, the current understanding, each slot's stated value under *What is confirmed* and the
first half of *Important assumptions* are the model's own words, copied through. Those words are on
the mirroring side, so a French request produces a `solution-assessment.md` whose judgment —
problem, solution, complexity, decisions, challenges, risks — is English and whose four projected
sections are French.

That is not a defect to render away, and since #491 it is not an open question either:
`decision: the-decision-briefs-quoted-half` settles it. **The saved brief stays on the English
anchor side, and its four projected sections are quotations** — the client's own words about their
own problem, which is the one kind of text not improved by being rendered into the document's
language. What decides it is the reader: a *saved* `solution-assessment.md` is read by the build
side and stands as the trail behind a commitment, the same audience the anchor exists for, while the
PM taking questions back to the client is served by the turn output, which mirrors.

The two alternatives are rejected in that record with their reasons. In short: translating the
projection means asking the provider to restate facts it was already given, which is exactly what
CLAUDE.md refuses (*ask the provider for judgment; read the facts off the model*) — a restatement
can drift, a projection cannot — and moving the brief to the mirroring side would put the first
document in the build chain on the opposite side from the rest of it. Either change is confined to
one function, `brief_markdown`, which is what keeps the cost of reversing this visible.

So a French request produces French questions and a French understanding, and an English PRD. That
is the intended behaviour, not a limitation to work around.

The policy is stated to the model in each prompt asset's *Output format* block — one sentence in
`engine.md` for the mirroring half, one identical sentence in the six artifact prompts for the
English half — so it is an instruction on those calls rather than something left to emerge. Requivo
does **not** detect the request's language: nothing is stored about it and nothing branches on it.
Both halves are instructions to the model about what it is writing, not a routing decision made in
Python.

**One call is deliberately outside the policy, and it is named here rather than left to be
discovered.** `estimate` is prompted with no language sentence, so its `note` and `risks` — free text
a reader sees — come back in whatever language the model settles on. When the sweep was made that
was not an oversight: `estimate` was the one generator that produced **no file** — a terminal
analysis read by the person who ran it, the same reader and the same room as the mirroring half —
where every artifact on the English side is written to disk and read downstream by a dev team or a
tracker. Since #519 the estimate *is* written to disk (`estimate.md`, beside the `stories.md` it was
reasoned from), so the reason for leaving it open has weakened; the edge stays open all the same,
because closing it is a prompt change, and a prompt change owes a golden re-capture that #519
deliberately did not make. Until it is decided, "every artifact anchors English" means the six
that carried a filename before #519, and a mixed-language `estimate.md` is a known consequence
rather than a contradiction.

Requivo Web reflects the same split. The page declares `lang="en"` for its own chrome, and the
regions the policy says mirror the request — the request itself, the understanding, the questions —
declare an empty `lang`, which is HTML's way of saying the language is unknown. Unknown is the
honest claim: nothing here knows what language the client wrote in, and a guess that is usually
right is still a guess.

**What that buys, and what it does not.** It removes a false claim: the page no longer asserts that a
French objective is English. It does **not** make a screen reader pronounce that objective correctly.
[WCAG 3.1.2](https://www.w3.org/WAI/WCAG22/Understanding/language-of-parts) asks for the actual
language of each passage to be programmatically determinable, and `lang=""` is the opposite of
determinable — a reader defaulting to English will read French with English rules exactly as before.
So this is the strongest *true* statement available without detection, and the accessibility gap
stays open behind it. Closing it needs a real BCP 47 value, which needs either detection or asking
the user, and neither has been decided.
