# Example — Leave approval

The canonical example. One vague sentence, taken through the questions, the brief, and then the beat
the rest of this exists for: a changed answer, and a computed account of what it costs. Read it in
order; no install required.

| Step | File | What it is |
|---|---|---|
| 1 | [`request.md`](request.md) | The raw input — a single vague sentence. |
| 2 | [`model.json`](model.json) | The understanding the analysis built: what is known, what is assumed, and the decisions and challenges that rest on each. |
| 3 | [`solution-assessment.md`](solution-assessment.md) | The decision brief — what to review before committing to the scope. |
| 4 | [`prd.md`](prd.md) | A PRD generated from the *same* understanding. |
| 5 | [`acceptance-criteria.md`](acceptance-criteria.md) | Given/When/Then checklist, from the *same* understanding. |
| 6 | delivery outputs — [`epic.md`](epic.md), [`epic.json`](epic.json), [`epic.github.json`](epic.github.json), [`epic.gitlab.json`](epic.gitlab.json), [`release-notes.md`](release-notes.md) | Once scoping is settled: a delivery epic, its tool-neutral export, GitHub/GitLab issue-creation plans, and client-facing release notes — all still views of step 2. |

Steps 3 through 6 are all views of step 2. Any other document (user stories, an estimate) comes from
the same `model.json` — that is the point: the shared understanding is the source of truth, and every
document is generated from it.

Every file here was generated in one sitting from the same `model.json`, which is why they agree with
each other. The filename `solution-assessment.md` is the artifact's name on disk in every session;
what a reader is shown is a decision brief.

## The part a chat transcript cannot do

The table above shows generation. This part shows the reason the understanding is kept at all: when an
answer changes, Requivo can say what that costs — and it says it from the dependency graph, not from a
model's opinion.

Run it yourself, in the Web interface or the CLI. The answers below are the ones this example is built
around.

**1 — Analyse the request.**

```bash
requivo discover "We'd like to set up a leave approval system."
```

Among the questions it raises is one about the existing HR tool: whether the new system reads from it,
replaces it, or lives beside it.

**2 — Answer it one way.**

```bash
requivo answer <slug> "During the pilot both systems stay in sync — the legacy HR tool
                       keeps being written to, and balances have to match on both sides."
```

Two-way synchronization is now part of the understanding, and the next questions follow it rather than
the original request: which system owns a balance, what happens when the two disagree, and whether a
request may be approved while a mismatch is still open.

**3 — Generate the brief, and check what rests on what.**

```bash
requivo brief  <slug>
requivo impact <slug> integrations     # no provider call — a query over the dependency graph
```

The order matters. The brief is where the reasoning layer is produced: the decisions the analysis is
standing on, and the challenges it is raising against them, each recorded with the topics it rests on.
`impact` then reads those edges, and answers *before* you spend anything — which decisions rest on the
integration topic, and which documents consume it. On the model committed here it returns one decision
to re-validate and two premises back in question.

**4 — Change the answer.**

```bash
requivo answer <slug> "Correction: the migration is one-time. After cutover the legacy
                       system becomes read-only — nothing writes back to it."
```

**5 — Read what moved.**

```bash
requivo status <slug>
```

The integration topic has changed, so the decision about what a request may do while a balance
mismatch is open is flagged for re-validation, both premises the brief raised about the sync are back
in question, and the brief itself is marked as needing an update. Nothing was regenerated on your
behalf — Requivo reports, you decide. That report is computed, not generated: the same change would
produce the same list every time, which is exactly what a generated answer cannot promise.

In the Web interface the same sequence renders as a **What changed** block with a **Needs review**
list, immediately under the answer you just gave.

## Reproduce it

Every document in the table above is a view of step 2, so the first thing to do is put step 2 where
the engine keeps understandings: in a session. Both of these are offline — no API key, and no
re-analysis, since the model already exists.

```bash
requivo session init examples/leave-approval/request.md --slug leave-approval
requivo model apply  leave-approval examples/leave-approval/model.json --expected-revision 0
```

You now hold this example's understanding at revision 1, and two more commands read it without
spending anything:

```bash
requivo status leave-approval                # the understanding checklist and readiness
requivo impact leave-approval integrations   # what rests on the integration topic
```

The generators take it from there. Each is one provider call and needs `ANTHROPIC_API_KEY`:

```bash
requivo brief    leave-approval                                   # the decision brief
requivo prd      leave-approval                                   # the PRD
requivo criteria leave-approval                                   # the acceptance criteria
requivo epic     leave-approval --export-json --github --gitlab   # epic.md + neutral export + tracker plans
requivo release  leave-approval v1.0                              # the release notes
requivo stories  leave-approval                                   # user stories (also: requivo estimate)
```

Documents land in `.requivo/sessions/leave-approval/artifacts/`, under whichever directory you ran
from — the files you are reading here are never written to.

The `model.json` here was produced by a real interactive discovery from `request.md`.
