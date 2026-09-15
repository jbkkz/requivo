# The job, not the artifact type

**Slug:** `the-job-not-the-artifact-type`

> **Not yet built.** This record settles the argument #607 asks for before its machinery lands. The
> product today still implements exactly one decision structure — software scoping — and every
> mention below of a perimeter mechanism, a plural schema, or a router describes what #608, #609 and
> #601 are for, not what runs today. Correct this note in place once they land.

## Context

Requivo models one decision structure today: the slots of `framework/model_schema.json`, all shaped
around scoping a piece of software — actors, business objects, business rules, workflow, permissions,
integrations, edge cases. The README's audience line is already wider than that — solutions
engineers, consultants, technical PMs, agency leads — but the model only ever served one of the
things that audience does.

A measurement forced the question. A go-to-market planning request was run twice — once through a
strong conversational baseline, once through Requivo — and scored by hand against the software slot
set. Four mapped cleanly, three partially, **eight had no referent at all**, and the structure the
request actually turned on — objective, ICP, channels, capacity, decision thresholds — had no slot
anywhere to hold it. That is not "the model is imperfect at this"; it is "the model does not model
this job", for a request from inside the audience the README already names.

That measurement is reported in the issues this record answers (#605, #607) and not in this
repository: the audit it was taken from is not committed here, and nothing above should be read as a
number a reader can re-derive from the tree. It is reported evidence, which is enough to reopen a
position and not enough to close an argument on its own.

The framing that follows is worth preserving in the words it arrived in, because it is sharper than
any paraphrase of it:

> I have a half-formed idea in the PM / solutions-engineer / builder space. What must I not forget
> before diving in headfirst?

Three things follow from that sentence, and each changes something concrete about what the product is
for:

- **The trigger is an urge, not a client request.** The engine's whole framing today — "paste a
  client or stakeholder request" — assumes a text someone else wrote. The job starts before that text
  exists.
- **The promise is recall, not documentation.** *What you will regret not having asked* is literally
  what `information_value = uncertainty × impact` already computes; the product has just never said
  so in those words.
- **The value lands before commitment, not at handover to a dev team.** A brief for a dev team is one
  shape this can take, not the only one.

## Decision

**Requivo's scope is the job, not the artifact type.** It serves the decisions that job is made of,
through several **perimeters** over one engine, on the claim:

> The decision structure varies. The reasoning about decisions does not.

The boundary is the whole argument, and it is what stops this from becoming three unrelated products
sharing a repository:

**A perimeter owns**, and nothing outside it may assume:
- its slot schema
- its elicitation spec
- discovery guidance specific to it — a software heuristic such as `engine.md`'s "primary objects
  first — a Job, an Invoice, a Mission" must never reach a session running a different perimeter
- one artifact to begin with, its equivalent of the decision brief — a second is added when a user
  asks for one, never because the software perimeter happens to carry more

**The Core keeps**, identical across every perimeter that will ever exist:
- the driver, `information_value = uncertainty × impact`
- `Slot`, `Confidence`, `Impact`, evidence, revisions and provenance
- `DesignDecision`, `Challenge`, `Opportunity`, and whatever is added beside them
- `propagate()`, `diff_models()`, staleness and `requivo impact` — the dependency graph reasons over
  slot ids and does not care which vocabulary they came from
- the session store, the lock, the atomic claim, integrity

If that split is wrong anywhere, it will show up as a reasoning item that behaves differently under a
different vocabulary than the claim above predicts — and finding that out is exactly what building
the second and third perimeter (#609, #612) is for.

**The set of perimeters stays finite by one rule, applied the same way it already is elsewhere in
this repository: a new perimeter needs a real recorded instance, not a plausible one.** Go-to-market
has one, the measurement above. Data / analytics does not yet, and #612 says so in its own body: its
first job is to get one before its slot set is designed from a whiteboard.

**The cost accepted, per perimeter, for as long as it ships:** a slot schema, an elicitation spec,
one artifact, and a golden baseline — plus the maintenance of all four. The baseline is the item
most easily skipped and the least optional: behaviour in this product is tuned by editing assets and
measured through the harness, never judged from a single run, so a perimeter without one cannot be
improved, only guessed at. A perimeter that ships without a baseline has not shipped.

Targeting, surface choice, pricing and the hosted service are deliberately not decided here. Those
belong to the private cloud repository — this repository is published, and who a product is sold to
is not an engine concern.

## What breaking it cost

No incident is on record for taking this decision, because no perimeter beyond software scoping has
shipped yet — inventing a failure would be dishonest. What is on record is the measurement that made
the narrower answer costly enough to reopen a position already written down once: the audit that
proposed declaring a request outside software scoping out of scope is the same audit whose own
numbers — most of the slot set with no referent, and a request structure with no slot anywhere —
are the reason this record exists at all. Staying narrow does not avoid a cost; it spends it on every
request from the exact audience the README already claims to serve.

If this decision is wrong, the shape of the failure is predictable and worth naming here when it
happens: a second or third perimeter that reproduces the software perimeter's structure instead of
one measured from a real request, or a perimeter that turns out to cost about what building it
standalone would — see the reopening condition folded into the alternatives below.

## Alternatives rejected

- **Declare a request outside software scoping out of scope, and say so at the boundary.** This is
  what the audit itself recommended, and what this issue originally proposed before being rewritten.
  Cheap, honest, and it forecloses the product: a scope drawn at "software" is narrower than the
  audience line the README already states, and a micro-scope does not sell — the thing being sold is
  help with a job, not help producing one document type.
- **One maximally generic schema flexible enough for every job, instead of several perimeters.**
  Rejected because it dissolves the boundary this record depends on: a software-specific heuristic
  like "primary objects first" has nowhere to live except inside every slot's description, a router
  (#601) has no clean edge to test against, and a slot vocabulary wide enough to fit an unrelated job
  is a vocabulary vague enough to fit none of them well.
- **Add a perimeter whenever one seems plausible, from a whiteboard.** Rejected by the real-instance
  bar stated above, which this repository already applies in kind — to the golden harness's
  relevance routing, to a new source-scanning test tier, to #594's CLI scanner. What those share is
  the *kind* of evidence, not the count: a new test tier owes two recorded instances of the drift it
  would have caught, relevance routing is waiting on a third, and a perimeter owes one recorded
  request it demonstrably cannot model. Go-to-market clears its bar with the measurement above; data
  / analytics (#612) does not yet, which is why it is filed with that gap stated in its own body
  rather than built ahead of it.
- **Build a third perimeter on schedule regardless of what the second one showed.** Rejected in
  favour of a measurable checkpoint, stated in #612: if the third perimeter costs about what the
  second one did, the mechanism generalised. If it costs about what building it standalone would
  have, the mechanism did not, and the right answer may be fewer perimeters done properly rather than
  a framework applied a third time.
