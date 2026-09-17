# Evaluations — the golden harness

> Behaviour is tuned by editing Markdown/JSON assets, and the engine is non-deterministic — so "did
> that change help?" can't be answered by one run. This harness answers it.

## Workflow

```bash
python scripts/golden_run.py <slug>          # capture a fixed request K times (K=3)
python scripts/golden_diff.py                # what moved, above the measured noise floor
python scripts/golden_diff.py <slug> --questions   # the questions and challenges themselves
```

Edit an asset → `golden_run` → `golden_diff` → commit the new baseline if the change is intended.
`fixtures/golden/requests.md` is the fixed request set — one per problem *form*. A bare
`golden_run.py` captures only single-pass requests; every interactive one (see below) is skipped
by default and named, with the command to capture it alone -- pass a slug explicitly, or `--all`,
to include it (#276).

## Two shapes of request

Most requests are **single-pass**: one discovery call, captured K times. That is the right shape for
watching a prompt or a context card, and it is the wrong shape for watching the *interactive* loop,
because a single-pass capture only ever has a turn 1.

A request that also carries `answer.<slot>:` lines is **interactive**. It drives
`DiscoveryService.draft_turn` — the loop behind `requivo discover` — for up to `GOLDEN_TURNS` turns
per run, answering the engine's questions off those lines. Each line is one *layer*: the next thing
that client has to say when the engine comes back to that slot. Layers are handed out in order and
then run out, which is what keeps the conversation moving instead of looping on one reply; a question
the sheet cannot answer is skipped, exactly as a user pressing Enter skips it, and a turn that
answers nothing ends the capture.

It exists because the interactive loop is grounded differently from turn 3 onward: it carries the
model and no longer re-sends the transcript. Turns 1 and 2 are byte-identical to the loop it
replaced, so only a capture that runs deep says anything at all about that change.

## The perimeter a request runs under

A request block in `requests.md` may carry a `perimeter:` line naming which installed perimeter
(`core/perimeters.py`, #608) the discovery call runs under. It defaults to `software` when the line
is absent, so every request written before #621 is unchanged byte-for-byte. `golden_run.py` threads
it through to the discovery call, and the captured `.runs.json` envelope records which perimeter it
ran under, on the same footing `model` (#515) already does — a baseline that does not say which
perimeter it measured cannot be checked, and a missing key reads as `software` rather than as a
third unknown state, because there was only one perimeter before #608 (the same migration a session
without a recorded perimeter already gets).

`golden_diff.py` refuses to compare a baseline and a candidate captured under different perimeters,
rather than diff slot ids that mean different things in each schema: it names both perimeters, says
it cannot compare, and moves no verdict — the same shape as a lens that could not look (see "Every
lens runs" below). The other requests in the same run are unaffected; only the mismatched one is
refused. `fixtures/golden/requests.md` carries one go-to-market request, `expand-into-new-segment`,
with no committed baseline yet — capturing one is a deliberate spend the maintainer makes when ready,
the same as any other first capture in this file.

## What it reports, and why

- **Consensus over K runs, not a single capture.** A slot dimension is a usable reference only if all
  K runs agree on it. The per-request noise floor (how many slots are unanimous) is printed so you know
  how much signal a request can carry.
- **Strong vs weak moves.** Strong = unanimous before *and* after; weak = a bare majority (at K=3, one
  run flipping). Act on strong, watch weak only in aggregate.
- **A capture identical to HEAD is reported as "not re-captured", never "no change"** — a false
  all-clear is the one failure mode a regression lens must not have.
- **Challenges are grouped by the slots they contest, never by wording**: the engine rephrases at
  the concept level, so two wordings of one challenge share no words.
- **The assessment lens** (`--brief`) watches the deliverable instead of the discovery state: the
  complexity verdict and which premises the engine chose to contest.
- **Every lens runs, and the verdict is the union of the ones that ran** — the strongest signal any
  of them found. They are independent measurements of one capture rather than votes on one question,
  so a flat slot consensus is never evidence that the assessment held still. The slot section's
  *"no change above the noise floor"* line decides only whether that section is a dash; it used to
  end the whole request, which made the assessment lens unreachable in exactly the case it exists
  for, on the capture the maintainer had paid double for.
- **A lens that could not look says so on its own line, and moves no verdict.** A request captured
  without `--brief` reads `assessment · not captured — this lens did not look`, which is a different
  sentence from `assessment · verdict and challenges unchanged`. A capture whose baseline *had* an
  assessment and no longer does is louder — `assessment ! … nothing to compare`, because committing
  it would drop a lens — and still grades as nothing measured, because nothing was. Re-capturing the
  whole set without `--brief` is the documented workflow for an `engine.md` or context-card change,
  and every single-pass baseline currently carries an assessment, so grading that state as a finding
  would report six strong signals on a run where nothing moved.
- **The interactive lens** watches turn 3 and beyond on an interactive request, in three measures —
  questions **re-asked** after the client had already answered them, early confirmations the model
  **lost** by the end, and completeness that **regressed** across a deep turn. Each is a way the
  carried model could turn out to be a lossy summary of the transcript it replaced. Findings are
  reported per run and as the unanimous set, on the same rule as a slot move: act on unanimous.
- **A capture that stopped short is not a clean one.** The lens prints the depth each run reached and
  warns when any of them came in under five turns, because a conversation that converged at turn 3
  never reached the question. And on a single-pass baseline it says *not measured* rather than
  printing an empty finding set, which would read exactly like a clean one.
- **A SHALLOW capture names what the sheet still had to say.** `AnswerSheet.remaining()` is replayed
  against each run's own record of what it answered, and a layer is only reported when *every* run
  in the capture left it unused — one run reaching it means the sheet was not why the capture stayed
  shallow. Reported only below `MEASURABLE_DEPTH`: a healthy capture is deliberately given more
  layers than five turns can consume, and leftovers there are by design (#163).

## It measures movement, not improvement

The slot tiers are a projection; the questions and challenges are the product. `--questions` is usually
what settles whether a change was an improvement or merely a movement. When the finance card landed,
the engine stopped asking *"what exactly are these totals?"* and started asking *"a traceable
adjustment entry, or an override?"* — that's the read that matters.

## Cost

K calls per **single-pass** request, doubled under `--brief`. An **interactive** request costs
K × `GOLDEN_TURNS` instead — 15 at the defaults — so capture it on its own rather than as part of a
full-set run. Re-capture the targeted request first and the full set only before committing a
baseline.

The figures above are per request *shape*, and no total for the request set appears here on purpose:
`golden_run.py` computes the ceiling for the requests it actually parsed and selected and prints it
before the first call, so the number to budget against is the live one rather than one written down
on the day the set happened to have six single-pass requests in it (#290).

The harness's own logic is unit-tested in `tests/test_golden_lib.py`, its capture loop in
`tests/test_golden_capture.py`, and what it prints — the per-lens states and the union verdict — in
`tests/test_golden_readout.py` (no API calls in any of them).

`tests/test_golden_baselines.py` checks every committed baseline against `requests.md` and refuses
one that has drifted out of step, unless the slug is a declared exception in that file's
`_DECLARED_DRIFT` naming the issue that owns the (paid) re-capture. That dict is empty as of the
seven-baseline re-capture in #405 — every committed baseline currently agrees with `requests.md` —
and stays in the file as the mechanism for the next asset edit that outruns its re-capture.

## Baseline freshness — is the committed capture even current?

`tests/test_golden_baselines.py` (above) catches a baseline whose stored *request*/*answers* disagree
with `requests.md`. It says nothing about a baseline that still agrees with `requests.md` but was
captured before a prompt, context card, or the generator code that assembles the on-wire messages,
changed underneath it — which is a different way for a baseline to be measuring something other than
what a reader assumes.

`golden_diff` reports that too now, as the first line of every request's readout, before any lens
output: whether a commit touching `WATCHED_PATHS` (`scripts/golden_lib.py` —
`src/requivo/assets/{prompts,context,framework}/` and
`src/requivo/providers/anthropic/generators.py`) landed since the committed baseline's own last
commit in HEAD. Three states, and the third never collapses into the first:

- **current** — no commit touching `WATCHED_PATHS` since the baseline was captured.
- **stale** — one or more did; each is named with its date and subject, so a movement reported below
  can be told apart from a working-tree edit's own effect.
- **unknown** — git could not answer (unavailable, a shallow clone, or the baseline has no commit
  history in HEAD). Never rendered as `current` — the collapse this file's "It measures movement, not
  improvement" section already refuses for a byte-identical capture applies here to a commit count.

Funded by two reproduced instances: #405 itself (three asset commits landed between one baseline
capture and the next, unnoticed for a month) and #410 (`ba526f6` dropped `indent=2` from the JSON
`generators.py` sends as the user message for every `--brief` capture — invisible to
`prompt_version()`, which hashes only the *system* prompt, and to `tests/test_golden_baselines.py`,
which compares only `request`/`answers`). `WATCHED_PATHS` is scoped to exactly those two mechanisms
and says so in its own printed line; it is not a claim that nothing else can move what a capture
measures.
