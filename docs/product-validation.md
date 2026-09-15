# Product validation

Requivo is well tested and under-validated. The test suite answers *does it do what it says*; it
cannot answer *is what it says worth doing*. That second question has one honest method — run it
against the alternative on real requests and write down what happened.

This is a manual protocol. Deliberately no analytics and no telemetry ship in the open-source
application: a product-usefulness question answered by instrumenting users is a different question,
and a worse one. Run this yourself, on your own past requests.

## The claim under test

> Requivo surfaces the decisions that would change the scope, keeps them traceable, and tells you what
> a changed answer costs — better than a strong prompt to a capable model does.

Notice what is *not* claimed. Not "asks better questions" alone: a good model asks good questions. The
claim is about what survives the conversation.

## The baseline

Not a weak strawman. Give the model the same request and a genuinely good prompt:

```text
You are a senior product manager. Here is a client request.

1. Ask me the questions whose answers would materially change what gets built. Skip the ones that
   wouldn't. Explain why each one matters.
2. Challenge any premise in the request that looks expensive or risky.
3. Once I've answered, write a specification I could take into an estimate.
```

Use the same model Requivo is configured with, so the comparison is about the product and not the
provider. Run the baseline in a fresh chat.

## The requests

At least four, from your own past work, anonymised. Cover different shapes:

1. **Multi-country leave management** — one domain, heavy configurability.
2. **Field technician offline operations** — one domain, hard technical constraint.
3. **Approval workflow with an external integration** — two systems, ownership questions.
4. **A request outside the software-scoping shape** — not scoping software at all: a go-to-market
   plan, a hiring decision, a pricing change, anything whose fit is genuinely in question rather than
   merely awkward. Every slot in the model is built for software scoping, so this shape asks a
   different question than the first three: not "does it ask good questions" but "does it know it
   doesn't apply."

   **What a pass looks like here is not a good document.** It is an honest statement of the boundary —
   Requivo, or the person running the protocol, saying plainly that this request isn't the shape it
   models, rather than forcing an answer through the software slots because that is the only place it
   has to put one. A polished PRD for a hiring decision is not a better result than that statement; it
   is a worse one wearing a good result's clothes. If a later build routes this shape to a perimeter
   of its own, the same pass condition carries over to that perimeter's edge: the win is still the
   honest boundary, not the document.

A request you already know the outcome of is the most useful kind: you can tell whether a question was
prescient or merely plausible.

## What to record

For shapes 1–3, in both the baseline and Requivo:

| | Baseline | Requivo |
|---|---|---|
| Questions asked | | |
| …of those, genuinely useful | | |
| …of those, that **changed the scope** | | |
| Generic or filler questions | | |
| Time to a usable brief | | |
| Would you send the brief to a client as-is? | | |

The third row is the one that matters. A question is scope-changing if the answer would have moved
the estimate, the architecture, or what you agreed to deliver. Count it honestly — the temptation is
to credit anything that sounds insightful.

Shape 4 is not scored against this table — there is no brief to send, and forcing one through the
rows would repeat the exact mistake the shape exists to catch. Record instead whether the boundary was
stated, and where: at discovery, at readiness, in the generated artifact, or nowhere.

## Then the two things a chat cannot do

These are the actual bet. Test them separately, because they are where the product either earns the
Core's complexity or does not.

**Resumption.** Come back two days later. Open the session. How long before you can act — and how much
of what you knew survived? Do the same with the baseline chat.

**Change impact.** Change one answer you already gave. In Requivo, record what it reports: which topics
moved, which decisions need re-validating, which documents need updating. In the baseline, ask the
model the same question and check its answer against what you know to be true. Requivo's answer is
computed from the dependency graph; the baseline's is generated. Note where they differ, and which one
was right.

If Requivo's advantage does not show up here, it does not show up. Everything upstream — the slots,
the readiness rules, the revisions — exists to make these two moments work.

## Recording the result

Write it down as you go, in a scratch file, not from memory afterwards. Memory rounds toward whatever
you hoped would happen.

**State which of the four moments the run actually tested — discovery, resumption, change impact,
trust — and give each one a state rather than leaving it blank.** The first real run of this protocol
tested discovery and trust and never touched resumption or change impact, and the recording said
nothing about that gap: six rows, all upstream of the two moments the protocol itself calls the actual
bet, with no place to mark the other two as skipped. Six months later that reads as "we ran the
protocol and lost," when what happened was narrower and different. Record all four, explicitly, every
time:

| Moment | State |
|---|---|
| Discovery | tested / not tested |
| Resumption | tested / not tested |
| Change impact | tested / not tested |
| Trust | tested / not tested |

**Trust** is the "What to record" table's own last row: *would you send the brief to a client
as-is?* It is the one moment that costs nothing extra to test — it falls out of the questions row
above, in the same sitting, with no two-day floor and no need to change an answer — which is exactly
why a run that records "trust: not tested" is the one gap in the four that most needs explaining.

**Not tested is a legitimate entry, not an apology.** Resumption alone carries a two-day floor, so a
single sitting can honestly test only discovery and trust — that is a fine reason to write "not
tested" against the other two, and a worse one to leave the cell blank and let a reader assume it was
covered.

Three outcomes are all worth having:

- **It wins on change impact and resumption.** Then the product hypothesis holds, and the next work is
  making that visible earlier in the flow.
- **It wins on questions but not on the two moments.** Then Requivo is a very good prompt with a
  session store attached, and the Core is over-built for what it delivers.
- **It loses.** Then the interesting question is *why* — usually context. Requivo's questions are only
  as sharp as the [context cards](context-cards.md) it reads. A loss with no cards loaded is a
  different result from a loss with a good one.

A verdict on a run that skipped a moment is a verdict about only the moments it recorded as tested —
never read a "wins on questions" outcome as also covering resumption or change impact it never ran.

## Where a completed run lives

The material in a real run is customer-derived — an actual request from an actual person, and often
the outcome you already know for it. It does not belong in this repository, or in this repository's
issues, tests or fixtures, even paraphrased or reworded. `requivo-lab/` is the private repository that
exists for exactly this kind of material; its `corpus/` directory is where a completed run's scratch
file goes once the run is done. See [open-source-strategy.md](open-source-strategy.md) for the full
boundary between the public and private repositories.

Decide this before the run starts, not while holding the transcript: write the scratch file as you go,
and when it's done, move it to `requivo-lab/corpus/` rather than leaving it wherever it was convenient
to type.

## What this is not

Do not fold this into the [golden harness](evaluations.md). That harness measures whether an asset
edit moved the engine's behaviour above the noise floor — a real, narrow, mechanical question. Scoring
subjective usefulness on the same scale would make a judgment look like a measurement, and the number
would then be quoted without its caveats. Keep them apart.
