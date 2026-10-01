# Example — Event check-in & freelancer reconciliation

A deliberately messy request, end to end. Read it in order; no install required.

This one is the stress test: not a tidy one-line brief but a **real-shaped client email** — a symptom
rather than a spec, three or four features conflated into one, and a hard constraint plus a deadline
buried at the end. It's here to show what the engine does when the input is the kind of thing a client
actually sends on a Friday night.

| Step | File | What it is |
|---|---|---|
| 1 | [`request.md`](request.md) | The raw input — a rambling, multi-part client email. |
| 2 | [`model.json`](model.json) | The structured model the discovery built from it (the product). |
| 3 | [`solution-assessment.md`](solution-assessment.md) | The deliverable — executive summary, **challenges**, design decisions, risks, opportunities, next steps — as the text-fenced terminal capture `requivo demo` replays. |
| 4 | [`brief.md`](brief.md) | The same decision brief, as the clean Markdown `ArtifactService.save` actually records and Requivo Web renders — what its keyless "Explore a worked example" button seeds alongside the model, so the click delivers the brief too, with no key and no call (#429). |
| 5 | [`epic.md`](epic.md) | A delivery epic — the work broken into trackable issues with dependencies, from the *same* model. |
| 6 | [`acceptance-criteria.md`](acceptance-criteria.md) | Given/When/Then recette checklist, from the *same* model. |
| 7 | [`estimate.md`](estimate.md) | An uncertainty-aware estimate — a day range per task, a low–high total, and the unresolved slots that drive the spread — as the text-fenced terminal capture of `requivo estimate`. |

Steps 3 through 7 are all views of step 2 — the model is the product, everything else is a render of it.

## What to look at

The request asks for "something that ties this together." The engine's job is to notice that *this
together* is really three disconnected problems, and to push back before anyone builds. In the
assessment:

- **It refuses the conflation.** The top challenge — *"One word, two different problems"* — splits the
  single requested "mismatch alert" into two structurally different builds: real-time attendee approval
  at the door, and post-event finance reconciliation. Different data models, different urgency,
  different owners.
- **It catches a legal tripwire nobody wrote down.** From *"invoices don't line up with hours worked,"*
  the engine flags that clocking freelancer hours like a timesheet is a classic **disguised-employment
  (salariat déguisé)** fact pattern under French labor law — the client may be building evidence against
  their own contractor classification. It recommends checking with legal before the reconciliation rule
  is even designed.
- **It sequences against the fixed deadline.** *"Six weeks, two builds, no rehearsal slack"* — protect
  the one path that fails visibly in front of the client (the door), de-scope reconciliation to a
  post-event report unless real-time alerting is proven necessary.

The epic then carries those judgments through on its own: the first three issues are spikes that resolve
the split, the legal basis, and the source-of-truth question, and everything downstream depends on them.

## Reproduce it

The generators write back into a session, so put this model into one first. Both commands are offline
— no API key, and no discovery re-run:

```bash
requivo session init examples/event-checkin-reconciliation/request.md --slug event-checkin-reconciliation
requivo model apply  event-checkin-reconciliation examples/event-checkin-reconciliation/model.json --expected-revision 0
```

Then read it, or regenerate any view of it. `impact` costs nothing; each generator is one provider
call and needs `ANTHROPIC_API_KEY`:

```bash
requivo impact   event-checkin-reconciliation constraints   # what the deadline holds up — no provider call
requivo brief    event-checkin-reconciliation               # the decision brief
requivo epic     event-checkin-reconciliation               # the delivery epic
requivo criteria event-checkin-reconciliation               # the acceptance criteria
requivo estimate event-checkin-reconciliation               # a day-range estimate (also: requivo stories, requivo prd)
```

Documents land in `.requivo/sessions/event-checkin-reconciliation/artifacts/`, under whichever
directory you ran from — the files you are reading here are never written to.

The `model.json` here was produced by a single discovery pass from `request.md`. The engine is
non-deterministic, so a fresh run phrases things differently — the shape of the pushback is what's stable.
The same holds for `estimate.md`, one captured run of `requivo estimate` against this model: a fresh run
splits the work and sizes the ranges differently — a range per task, a low–high total and the slots that
spread it are what's stable.
