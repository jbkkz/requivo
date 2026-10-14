# The requirements model

> The model is the product; documents are views of it. This is what the model is made of and how
> Requivo reasons over it.

## The product vocabulary

These are the names the product uses, in Requivo Web and in the documents it writes. The rest of this
page is the same ideas in the engine's own, more precise vocabulary — which is what `--json` and the
technical docs speak.

- **What we know** — stated directly by the client, or, with no client, the builder's own committed
  intent (what they want, will build, will spend) — never their unconfirmed belief about the world.
- **What we are assuming** — inferred from context, proposed as a default, drawn from domain
  knowledge, or the builder's own unconfirmed belief about the world (will users want this, will they
  pay); confirm before building.
- **Open question** — not yet known, and worth asking when the answer would move the build.
- **To test** — not yet known, and **not** worth asking: only a real test would settle it. It names
  what would, and does not block readiness.
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
| what we know / what we are assuming / to test | evidence (`explicit` / `inferred` / `unknown` / `testable`) |
| how fully a topic is covered | coverage (completeness) |
| topic | slot |
| assumption to review | challenge |
| needs updating | stale artifact |
| decision brief | `brief` |
| consultant view | `consultant_view` |

## Slots and pillars

The model is a set of typed **slots** — the problem, actors, business objects, business rules,
workflow, permissions, edge cases, constraints, and more — grouped into four areas:

- **Why** — the problem, current process, success criteria
- **What** — actors, business objects, business rules, workflow
- **How** — permissions, integrations, constraints, config-vs-custom
- **Validate** — acceptance, edge cases, risks

Each slot carries a value plus its **evidence** and **coverage** (below). Every artifact is a render of
this same filled model.

## One engine, several perimeters

The scope is the job, not one artifact type (`decision: the-job-not-the-artifact-type`). A
**perimeter** owns its slot schema, its elicitation spec (`assets/perimeters/<id>/`), its discovery
guidance and, to begin with, one artifact. Software scoping is the default and carries the larger set
of artifacts below because it grew them one at a time; go-to-market is the second (#608, #609). A
session's perimeter is frozen at creation, chosen by `--perimeter` or by the router (#601,
`decision: two-judgment-calls-not-one`). What is **Core**, identical across perimeters, is the
mechanism this page describes: the driver, evidence and coverage, the reasoning items, the dependency
graph and its staleness rule, readiness. Where a section names a concrete artifact type or slot, that
is software's own content riding on the shared mechanism.

## The driver: information value = uncertainty × impact

Requivo does **not** ask because a slot is empty — it asks where an answer would change the solution.
Empty-but-low-impact slots are left alone; filled-but-risky ones get probed. **Impact is estimated
from the product context**, so the engine is only as sharp as the [context cards](context-cards.md)
it's given.

Readiness reads the same rule the other way (#815): it waits only on what would still change the
solution. A high-impact slot blocks until it is confirmed, deferred to a test, or delegated by the
client — or until the engine stops asking about the default it holds. A gap below high impact, or a
default nobody asks about any more, is an assumption offered for veto, never a blocker; a blank one
nobody asks about gets the probe from the schema as its question, so "not ready" always comes with
something to ask. A turn asks every question above the bar at once, and none below it.

## Evidence vs coverage

Two independent signals, deliberately not collapsed:

- **Evidence** — *how we know* a slot: `explicit`, `inferred`, `unknown`, or `testable`.
- **Coverage** — *how fully* a slot is covered (its completeness). A slot can be `explicit` yet thinly
  covered — stated in one word. That still blocks readiness; it reads as "partial", not "confirmed".

**`explicit` needs an authority** (`decision: confidence-stays-one-axis`, #611): a client's word
always qualifies; with no client, only the builder's own *intent* (what they want, will build, will
spend). Their unconfirmed *belief about the world* — will users want or pay for this — is `inferred`
or `testable`, however plainly stated, just as a fact read out of a repository is `inferred`. Without
this, readiness would measure how much a solo builder typed. One thing a solo builder can vouch for is
their own system: an as-is read from it, restated to them and confirmed, is `explicit` (#716).

**`testable` is a gap no amount of asking closes** — a go-to-market bet, whether anyone wants the
feature — where `unknown` is one an answer would close. A `testable` slot must name what would settle
it or it is refused; named, it does not block readiness. Settling one is an ordinary model change.

**What the reasoner brings is `inferred`, and says where it came from**
(`decision: the-expert-proposes-and-labels`). The requester's word is never invented, but the
reasoner is expected to contribute a concrete default where the model leaves room (evidence
`proposed: <rationale>`) and the regulatory, technical or market facts the request bears on
(`domain: <fact>`). Both enter the model before any document states them, so they reach the "what I
will assume unless you object" list and can be vetoed; documents stay views of the model, and tag
each claim by its source. The keyless plugin does this; the API path's generator prompts still carry
the older rule.

**Claims label a mixed slot** (`decision: claims-carry-provenance`, #751). A slot may list the
statements it holds, each with where it came from (`requester`, `artifact`, `evidence`, `proposed`,
`domain`, `assumed`) and what an answer did with it (`open`, `let_stand`, `confirmed`, `to_test`). In
3.x claims are informational — `status` labels them, `model apply` names the one that moved — and
readiness still reads the slot as a whole.

## Decisions, challenges, opportunities

The assessment layer, persisted into the model so every generator inherits it:

- **Decision** — a settled choice, with its *why*, the *alternative* weighed, and the *trade-off*
  accepted.
- **Challenge** — a contested premise: the assumption the request takes for granted, a concrete
  alternative, the consequence, and a recommendation. This is the differentiator — it pushes back on
  the request rather than organising it. The decision brief proposes them, and so does the
  go-to-market plan (#728).
- **Opportunity** — a leverage point, ranked, naming the modules it reaches.

Exclusions (what is deliberately out of scope, #599) and decision thresholds (#604) are reasoning items
of the same kind, with content-derived ids.

## Dependencies and staleness

The model is not a flat snapshot — its parts rest on each other:

- A decision records the slots it was **derived from**; a challenge records the slots it **contests**.
- Each buildable artifact records the slots it **consumes**.

So a change knows its blast radius. `requivo impact <slug> <slots>` shows the decisions to re-validate
and the artifacts that would go **stale**; a discovery turn that materially moves the model flags the
already-generated files that no longer match it. An unrelated (or completeness-only) change leaves an
artifact fresh — staleness follows the dependency graph, not the revision number.

The same edge answers the other direction of time: a decision **derived from thinner evidence than
exists now** — a `derived_from` slot that was `empty` or `inferred` when it was recorded and is
`explicit` now (#493) — is named *worth re-reading* by `requivo impact` and the Web, never
*contradicted*: that judgment costs a call.

Every required slot reaches at least one artifact's staleness check — a specific artifact when it
shapes its content, or the assessment's judgment over the whole model — and a test catches one that
reaches neither (CLAUDE.md's "Adding a slot").

## Readiness

Readiness is binary: a high-impact slot must be both `explicit` **and** covered above the soft
boundary to stop blocking the build. A high-impact gap — empty, unknown, or stated-but-thin — keeps a
session out of "ready". Requivo does not invent graded "nearly ready" levels; it shows what blocks.

A `testable` slot does **not** block — it already names what would settle it, and "ready" means
precise enough to build from, not nothing left unproven. A high-impact slot that is merely `inferred`
still blocks, which is what stops a model of untested guesses from reading as settled.

## The language of the outputs

Requivo splits its outputs by reader, and lets the user choose the second
(`decision: documents-speak-the-chosen-language`, #790):

- **The conversation mirrors the request.** The questions and the understanding rendered each turn
  are in the request's language: they are read by the person who has to take them back to the client.
  Requivo mirrors; it does not translate.
- **The documents are written in the language the user chose** — the decision brief, the go-to-market
  plan, the PRD, stories, estimate, acceptance criteria, epic and release notes. A first `discover`
  or `run` at a terminal asks for it, offering the request's language; `--docs-language CODE` answers
  ahead (on `session init` too, which the keyless run skill passes after asking at its first
  checkpoint); any other run takes the request's language, or English when it cannot be told.

**The default costs no call.** The request's language is guessed by counting function words of the
languages Requivo knows by name (English, French, Spanish, German): at least two, and more than any
other language's. Anything else — another script, a one-word request, a tie — is English, and the user
can still name any BCP 47 code.

**The choice is recorded** as `docs_language` on `session.json` and read back from the session's
snapshot by every generation; a session from before #790 has none and reads as English
([session-format.md](session-format.md)). Each document generator is told it in its user message, not
its system prompt, so the cached prompt stays one per operation; the prompts say only to write the prose
in the language named there, and to leave ids, slot ids, enum values, JSON keys and code as they are.
The keyless skills read the same field from `status --json`.

**The saved documents no longer mix languages.** The brief's and the plan's projected sections
quote the model's own words, which are in the request's language; with the default those match the
judgment the provider writes. Every saved document's fixed words — title, headings, labels, verdicts,
the estimate's computed lines and their numbers (#823) — come from `render/phrases.py` in French,
Spanish and German, and stay English for any other code; the terminal views stay English. Two edges
remain: a slot's label (in *rests on*, *traces to* and the envelope) and a schema probe standing in
for a question nobody asked are the schema's English.

Requivo Web declares `lang="en"` for its chrome and an empty `lang` on the regions that mirror the
request — the honest claim, since the request's own language is still not recorded. That removes a
false claim but does not satisfy [WCAG 3.1.2](https://www.w3.org/WAI/WCAG22/Understanding/language-of-parts),
which needs a real BCP 47 value; a saved document could now declare `docs_language`, which is not done yet.
