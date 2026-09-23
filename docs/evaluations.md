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

Most requests are **single-pass**: one discovery call, captured K times — the shape for watching a
prompt or a context card. A request that also carries `answer.<slot>:` lines is **interactive**: it
drives `DiscoveryService.draft_turn` (the loop behind `requivo discover`) for up to `GOLDEN_TURNS`
turns per run, answering off those lines. Each line is one *layer* — the client's next thing to say
about that slot — handed out in order until it runs out; a question the sheet cannot answer is
skipped, and a turn that answers nothing ends the capture. It exists because from turn 3 the loop
carries the model instead of re-sending the transcript, so only a deep capture measures that.

## The perimeter a request runs under

A request block may carry a `perimeter:` line (default `software`); `golden_run.py` threads it to the
discovery call and records it in the `.runs.json` envelope, where a missing key reads as `software`
(#621). `golden_diff.py` refuses to compare captures from different perimeters — slot ids mean
different things in each schema — naming both and moving no verdict. `expand-into-new-segment` is
the one go-to-market request, with no committed baseline yet.

## What it reports, and why

- **Consensus over K runs.** A slot dimension is a reference only if all K runs agree; the
  per-request noise floor (how many slots are unanimous) is printed.
- **Strong vs weak moves.** Strong = unanimous before *and* after; weak = a bare majority. Act on
  strong, watch weak only in aggregate.
- **A capture identical to HEAD is "not re-captured", never "no change"** — a false all-clear is the
  one failure a regression lens must not have.
- **Challenges are grouped by the slots they contest**, never by wording.
- **The assessment lens** (`--brief`) watches the deliverable: the complexity verdict and which
  premises the engine chose to contest.
- **Every lens runs, and the verdict is the union of those that ran** (#162). A flat slot consensus
  is never evidence the assessment held still.
- **A lens that could not look says so and moves no verdict**: `assessment · not captured — this lens
  did not look`. A baseline that had an assessment the candidate lacks is louder (`assessment !`) but
  still grades as nothing measured.
- **The interactive lens** watches turn 3 onward: questions **re-asked** after an answer, early
  confirmations **lost**, completeness that **regressed** — each a sign the carried model is a lossy
  summary. Reported per run and as the unanimous set.
- **A capture that stopped short is not a clean one**: the lens prints each run's depth and warns under
  five turns; on a single-pass baseline it says *not measured*. Below `MEASURABLE_DEPTH`, a SHALLOW
  capture names the answer-sheet layers no run reached (`AnswerSheet.remaining()`, #163).

## It measures movement, not improvement

The slot tiers are a projection; the questions and challenges are the product. `--questions` is usually
what settles whether a change was an improvement or merely a movement. When the finance card landed,
the engine stopped asking *"what exactly are these totals?"* and started asking *"a traceable
adjustment entry, or an override?"* — that's the read that matters.

## Cost

K calls per single-pass request, doubled under `--brief`; K × `GOLDEN_TURNS` (15 at the defaults)
per interactive request, so capture those alone. Re-capture the targeted request first and the full
set only before committing a baseline. No total is written here: `golden_run.py` prints the ceiling
for what it actually selected before the first call (#290).

The harness is tested offline in `tests/test_golden_lib.py` and `tests/test_golden_harness.py`, which
also refuses a committed baseline whose stored request or answers disagree with `requests.md`, unless
its slug is in `_DECLARED_DRIFT` naming the issue that owns the re-capture.

## Baseline freshness — is the committed capture even current?

A baseline can agree with `requests.md` and still predate a change to what it measures. The first
line of each `golden_diff` readout says whether a commit touching `WATCHED_PATHS` — the prompts,
context cards and perimeter assets under `src/requivo/assets/`, and
`src/requivo/providers/anthropic/generators.py` — landed since the baseline's own last commit:
**current**, **stale** (each commit named with date and subject), or **unknown** (git could not
answer), which never renders as current. Funded by #405 (asset commits unnoticed for a month) and
#410 (a change to the on-wire user message invisible to `prompt_version()`); the scope is those two
mechanisms and says so in its printed line.
