# The elicitation framework

The framework is the core of Requivo. It's domain-agnostic: it encodes how an experienced
product person turns a vague request into a model precise enough to build from. The context cards in
`context/` ground that method in a specific product.

## Four pillars

Every requirement belongs to one of four pillars — a simple way to navigate the model and to explain
it to someone new.

| Pillar | Question | Slots |
|---|---|---|
| **Why** | Why are we doing this? | Real problem · Current process · Success criteria |
| **What** | What is the system? | Actors & roles · Business objects · Business rules · Workflow |
| **How** | How does it work? | Integrations · Permissions · Config vs custom · Constraints |
| **Validate** | How do we know it's right? | Edge cases · Reporting · Acceptance · Risks & rollout |

Pillars are for navigation. The unit that actually gets filled and tracked is the slot.

## Each slot is a small record

A slot records not just what we know, but how well we know it and where it came from:

```
completeness   0–100                                    how fully the slot is known
confidence     explicit | inferred | empty | testable    where that knowledge came from
impact         low | medium | high                      how much it shapes the solution
value          what we currently know
evidence       what it's based on
test_plan      what would settle it — only when confidence is testable
```

`confidence` is what keeps assumptions visible. An `inferred` slot is something the engine deduced
but hasn't confirmed, so it flows straight into the "Assumptions made" section of the summary —
nothing is quietly taken for granted.

**`explicit` needs an authority, not just confidence.** It means the person who said it can actually
commit to it — a client's own word always qualifies. With no client (a builder describing their own
idea), only their own *intent* qualifies: what they want, will build, will spend. Their unconfirmed
*belief about the world* — will users want this, will they pay — is never `explicit`, however
confidently they state it; it is `inferred` (an assumption to confirm) or, when no amount of further
conversation would settle it, `testable`.

**`testable` is the fourth state**, for the gap a builder's idea is usually made of: not "nobody has
told me" (ask), but "nobody knows until something is tried" (test). It carries what would settle it
and does not block readiness — a named test still to run is compatible with "precise enough to build
from"; an unasked question is not.

## The driver: uncertainty × impact

The engine doesn't ask about a slot just because it's empty. It weighs the value of asking:

```
information_value = uncertainty × impact
```

Uncertainty comes from `completeness` and `confidence`. Impact — how much the answer would change
the solution — is estimated from the product context. That second input matters: without context,
impact is only a guess, so the quality of the context cards sets the ceiling on how good the
questions can be.

The result is selective. A slot that's empty but low-stakes (reporting on a small field change) is
left alone. A slot that's only partly known but high-stakes (a business rule that varies by country)
gets a question. The aim is the few questions that change the build, not a full checklist.

## Config vs custom: the platform slot

One slot is optional: `config_vs_custom`. It's on for configurable, multi-client platforms and off
for one-off apps. It asks whether a piece of behavior should be hardcoded, configurable,
client-specific, or shared across all clients.

Most discovery tools assume a single-tenant, greenfield build and never raise this. On a platform
it's one of the highest-impact questions there is: it's the difference between behavior that scales
to every client and behavior that ends up forked per client. Turning the slot on lets the engine
surface the decision early, while it's still cheap to make.

## What the engine produces

Everything is a render of the same model. While the discovery is still open, each turn shows a
**discovery status** (per-pillar progress, a readiness verdict) and the **priority questions** —
ordered by information value, each with the reason it's being asked.

When nothing high-value is left to ask, it produces the deliverable: a **Decision brief**. Alongside
the objective and the assumptions still to confirm, the brief adds a consultant's read — what the
feature introduces, its likely complexity and main cost driver, and the risks and reuse
opportunities worth weighing before the build. Slots parked as low-impact are listed as *deferred*
rather than shown as gaps.

Later stages project the same model further down the delivery pipeline: user stories, then a
day-based estimate whose spread is driven by the slots that are still uncertain.
