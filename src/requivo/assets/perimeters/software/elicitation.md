# The elicitation framework

The framework is the core of Requivo. It's domain-agnostic: it encodes how an experienced
product person turns a vague request into a model precise enough to build from. The context cards in
`context/` ground that method in a specific product.

## Four pillars

Every requirement belongs to one of four pillars — a simple way to navigate the model and to explain
it to someone new.

| Pillar | Question | Slots |
|---|---|---|
| **Why** | Why are we doing this? | Real problem · Current process · Existing solutions & budget · Success criteria · Payer |
| **What** | What is the system? | Actors & roles · Business objects · Business rules · Workflow |
| **How** | How does it work? | Integrations · Permissions · Config vs custom · Constraints |
| **Validate** | How do we know it's right? | Edge cases · Reporting · Acceptance · Risks & rollout · First users & acquisition |

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
claims         optional, on a mixed slot: each statement   informational in 3.x (#751)
               with its source and confirmation
```

`confidence` is what keeps assumptions visible. An `inferred` slot is something the engine deduced
but hasn't confirmed, so it flows straight into the "Assumptions made" section of the summary —
nothing is quietly taken for granted.

**`explicit` needs an authority, not just confidence.** It means the person who said it can actually
commit to it — a client's own word always qualifies. With no client (a builder describing their own
idea), only their own *intent* qualifies: what they want, will build, will spend. Their unconfirmed
*belief about the world* — will users want this, will they pay — is never `explicit`, however
confidently they state it; it is `inferred` (an assumption to confirm) or, when no amount of further
conversation would settle it, `testable`. One thing a builder can vouch for is their own system: an
as-is read from it, restated to them and confirmed, is `explicit` (#716).

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

## Build vs buy: the cheapest outcome of scoping

`existing_solutions` asks what already exists, is already paid for, or could be bought, and within
what budget (#786). It defaults to high impact because the cheapest outcome of scoping is not to build
at all: a licensed tool nobody switched on, or an off-the-shelf option rejected for a reason worth
re-examining, can delete the project, and the budget envelope sizes the estimate and the architecture.
A request that says "build us…" takes building as its premise, so this slot is asked, not left to the
client to volunteer.

## Who pays, and how the first users are reached

`payer` and `acquisition` are for requests that are product ideas rather than specs (#775). For a
builder's own product, who pays and how, and how the first users are reached, change the product as
much as the segment does, and can decide whether to build at all. Both are required but default to low
impact: for an internal tool or a commissioned system the payer is the client and the users are a
known set rolled out to, so they stay explicit assumptions. Their `impact_signals` raise them to high
when the request is a product idea, and a required slot at high impact blocks readiness until it is
settled, which an optional slot never does. A request that asks whether something is worth building at
all is usually better served by the go-to-market perimeter.

## What the engine produces

Everything is a render of the same model. While the discovery is still open, each turn shows a
**discovery status** (per-pillar progress, a readiness verdict) and the **priority questions** —
ordered by information value, which is shown with each one (`requivo status --json` adds the reason
it is asked). The order is computed from the target slot, not taken from the reasoning: uncertainty
from its completeness and confidence, impact from its impact (the one-line formula is in
`core/analysis.py`), ties keeping the order the turn proposed.

A checkpoint asks every question the turn returned (`QUESTIONS_PER_CHECKPOINT` in the CLI equals
`MAX_QUESTIONS`, and the Claude Code skill follows it, #815), one at a time by information value, then
folds the answers into one turn: a question held back would cost another round trip with the client.
The next turn derives its own questions against the updated model, ranked against what the answers
changed.

When nothing high-value is left to ask, it produces the deliverable: a **Decision brief**. Alongside
the objective and the assumptions still to confirm, the brief adds a consultant's read — what the
feature introduces, its likely complexity and main cost driver, and the risks and reuse
opportunities worth weighing before the build. Slots parked as low-impact are listed as *deferred*
rather than shown as gaps.

Later stages project the same model further down the delivery pipeline: user stories, then a
day-based estimate whose spread is driven by the slots that are still uncertain.
