---
name: estimate
description: Derive user stories and a day-based, uncertainty-aware effort estimate from a Requivo session's model, in this Claude session, and save both as tracked artifacts against one revision. Use when the user wants an effort estimate for the current scope.
allowed-tools: Bash(requivo doctor:*), Bash(requivo model show:*), Bash(requivo status:*), Bash(requivo schema:*), Bash(requivo artifact save:*), Read
---

# /requivo:estimate

Produce a **day-based effort estimate**, one entry per user story — stories first, because the
estimate is reasoned against them, and #519 (`decision: the-estimate-graduates`) saves both together
against one revision: the estimate's provenance is incomplete without the stories it rests on. **You**
do the estimation; Requivo tracks both documents. This mirrors
`src/requivo/assets/prompts/stories.md` + the `Stories` contract, and `assets/prompts/estimate.md` +
`EstimateDraft`, at commit `969cfd9` plus #821's delivery work, money and swings — the same two calls `requivo estimate` makes, restated here
because the plugin cannot import those files at runtime (`decision:
plugin-skills-mirror-a-pinned-cli-commit`). Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md` unless you already hold it from an earlier
`/requivo:*` in this conversation — and read it again whenever you are unsure you still do.

## 0. Preflight
Run the shared **preflight** from REASONING.md before anything else: `requivo doctor --json`, checking
whether the command ran *at all* rather than what it reported. If it could not run, the `requivo` CLI
is not installed — follow REASONING.md's missing-CLI flow. Nothing has been saved at this
point, so there is no half-written estimate to find.

## 1. Load the model
```
requivo model show <slug>
requivo status <slug> --json
```
Note the `revision` — call it `N`. Both documents you write below are tied to this one revision, per
invariant 12: one snapshot, two files.

## 2. Reason → stories, then the estimate
**First, the stories.** Follow `/requivo:stories`'s own reasoning rules (4–12 stories, one shippable
slice each, developer notes where they apply, traceable to the model's slots by their labels, honouring confidence and impact) —
this skill does not repeat them; the referenced skill is the source.

**Next, name the soft slots** — this is what the CLI's own `estimate` generator computes in Python
(`core/analysis.py:soft_slots`) and hands the model as ground truth, so name them the same way rather
than reasoning about uncertainty from scratch: a slot is **soft** when its `impact` is `medium` or
`high` **and** either its `completeness` is below 70 or its `confidence` is not `explicit`. Read these
off the model you loaded in step 1.

**Then, the estimate**, from those stories and the soft slots you just named:
- Estimate each story as a **day range** (`days_low`, `days_high`) plus a complexity label `S`/`M`/`L`:
  `S` ≈ up to ~1 day, `M` ≈ 1–3 days, `L` ≈ 3+ days. Use the range to express real spread, not
  padding — tight for a confident story, wide for a shaky one.
- **Widen the range** for any story resting on a soft slot: unresolved scope is real cost risk. A
  story resting only on explicit, complete slots gets a tight range.
- For each item, name the topics that most drive its effort (usually a subset of the story's own
  traces), in plain language — never the raw slot id.
- One **basis** per item, one or two sentences — what the work is and what sets the range.
- Estimate **only** the stories you just wrote. Do not invent stories or scope. A story resting on a
  proposal (`proposed:` evidence) is estimated as proposed, its note tagged from REASONING.md's
  *Source tags*.
- `days_low` never exceeds `days_high` for an item — the low end is optimistic, not a floor error.

**Then make the number honest** — a total that only sums the stories' build reads low, because it is:
- **How to read it**: what one day covers (build, tests, review, fixing what QA finds), the team and
  stack assumed, and what is deliberately not costed.
- **Delivery work beyond the stories**, each a day range with its basis: environments, CI and hosting;
  design; QA and acceptance with the client; project management; rollout, migration and training —
  sized for this build. Whatever you leave out is listed under *Not included*, never silently absent.
- **What this prices**: the stories follow the recommended path, so the total prices it. Then the
  **alternative** path with its own rough day range (the custom build when the path is buying or
  configuring) and any recurring licence per month or year, outside the day totals. Configuring costs
  a fraction of building: a configure total near the build figure means build-sized ranges — resize.
  Turn days into money only as days × the day rate, in the *Cost* and *Alternative* lines.
- **Calibrate**: the high end covers one round of rework; an unseen API starts with a spike; offline
  sync, payments, health or financial data carry security testing and a pilot; configuring is days.
- **Day rate**: from the model when it states one, otherwise a typical rate for the client's market,
  marked *assumed*. **Budget**: only a one-off amount the model states; compare it with the cost range.
- **What would move this number**: 2–6 questions the client can answer, each with the days its answer
  adds or removes (signed, e.g. `+3 to +8`, `-12 to -6`). Unresolved topics no question covers are
  listed after them, by label.
- 2–5 risks the questions do not already say.
- **Totals, cost and confidence are computed, not judged** — the same formulas the CLI applies in
  Python, so state them rather than leaving them to a feel: `Total` sums `days_low` and `days_high`
  across every story **and** every delivery item. `Cost` multiplies both ends by the day rate. The
  budget *covers the whole range* when it buys at least the high end in days, falls *short of even the
  low end* below the low end, and otherwise *the low end fits, the high end does not*. `Confidence`
  counts the soft slots (call the count `n`): `n ≤ 1` is `high`, `n ≤ 3` is `medium`, otherwise `low`.

**Voice.** No slot ids, percentages or confidence labels in the prose.

**Language.** Write the stories and the estimate — headings, notes and risks included — in the
session's `docs_language` from `requivo status <slug> --json` (REASONING.md, *Every document is
written in the session's documents language*); story ids and the table's structure stay as they are.

Write the estimate as Markdown:
```
# Estimate

> Uncertainty-aware estimate — generated by Requivo

**Total:** <low>–<high> days — stories <low>–<high>, delivery work <low>–<high> · **Confidence:** <high|medium|low, from the formula above>

**Cost:** about <low × rate>–<high × rate> <currency> at <rate> <currency> a day (<stated|assumed>).

**Budget:** The stated budget of <amount> <currency> <covers the whole range | buys about <n> days at this rate: …>.

**What this prices.** <the path this number is for, and why that one>

**Alternative — <the other path>:** <low>–<high> days (about <low × rate>–<high × rate> <currency>); <its recurring cost>.

## How to read these figures

- **One day:** <what a day covers>
- **Team assumed:** <team and stack>
- **Not included:**
  - <item>

## Per story

| Story | Size | Days | Basis |
|---|---|---|---|
| S1 — <title> | M | 2–3 | <basis> |

## Delivery work beyond the stories

| Work | Days | Basis |
|---|---|---|
| <work> | 3–5 | <basis> |

## What would move this number

- <question> — +3 to +8 days

Also still open, not priced above: <label>, <label>.

## Risks and unknowns

- <risk>
```

## 3. Save both, stories first, against the same revision
```
requivo artifact save <slug> --type stories --file - --revision N --json
requivo artifact save <slug> --type estimate --file - --revision N --json
```
Both calls use the `N` you read in step 1 — not a revision either save might have moved to. Read
`stale` back from each: if either is `true`, the model moved while you were writing — say so plainly
and offer to redo both, since the estimate's basis (the stories) would need to move with it.

## 4. Point at the next step, once
An estimate over stale stories is a stale estimate: if the model changes later, `/requivo:status
<slug>` shows which of the two artifacts needs regenerating. Otherwise, `/requivo:criteria <slug>`
turns the same model into a recette checklist a client can sign off on.
