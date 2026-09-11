"""Shared logic for the golden regression harness (K-run consensus).

The engine is non-deterministic and the model family in use (Claude 5 / Opus 4.8) exposes no sampling
controls — `temperature`/`top_p`/`top_k` are removed and 400 if sent — so a single capture can't be
pinned. At n=1 the run-to-run noise drowns the signal a prompt or context-card change actually causes.

The answer is statistical: capture each request K times and reason about the *consensus*. A slot's
impact or confidence is only trustworthy as a signal if it is stable across the K runs; a dimension
that flickers run-to-run is noise and can't be used to detect change. This module computes that
consensus and the per-request stability (the empirical noise floor); `golden_run` captures the K runs,
`golden_diff` compares two K-run baselines through it.

A separate, unrelated concern also lives here: `baseline_commits_since` (below) answers whether a
*committed* baseline itself predates a real change to what a capture measures -- see its own section
for why (#405, #410), guarded by `test_a_commit_touching_a_watched_path_since_the_baseline_marks_it_stale`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from requivo.core.analysis import slot_label, state_of  # noqa: E402
from requivo.core.contracts import Brief, EngineOutput  # noqa: E402
from requivo.core.selectors import display_token  # noqa: E402
from requivo.streams import configure_streams, safe_write  # noqa: E402

GOLDEN = REPO / "fixtures" / "golden"
REQUESTS = GOLDEN / "requests.md"
K = int(os.getenv("GOLDEN_K", "3"))  # runs per request; 3 is the approved default


def configure_output() -> None:
    """Make this script's stdout and stderr unable to kill it on a character they cannot encode.

    Invariant 16, in the scripts that measure the product (#164). Both harness scripts print box
    rules, arrows, check marks and provider-written prose, and neither reached `streams.py` — which
    runs only from `cli.app()`. On a cp1252 console that is a `UnicodeEncodeError` at the `print`,
    **after** the work it was reporting has landed: `golden_run` spends real API calls, so a capture
    that completes fifteen of them, writes its runs file and then dies rendering the summary leaves a
    traceback standing where a result should be, over work already paid for.

    The class, not the instances. Sweeping the glyphs out of the two files would fix today's strings
    and leave the next print to reopen it, which is the argument `streams.py`'s own docstring makes at
    length; this is one call, so the question is decided in one place — with
    `errors="backslashreplace"` and never `replace`, because a reader cannot tell a substituted
    character from one that was never there.

    **No `EXIT_RENDER_FAILED` arm here, unlike `cli.app()`, and that is a decision.** That arm exists
    to stop a *paid, mutating* command being misreported as failed. `golden_diff` neither calls nor
    writes anything, so there is nothing for it to misreport and a guard that provably cannot fire is
    worse than none; `golden_run` writes each request's baseline **before** any summary print and
    already catches per-request failure, so its work is durable by the time a render could die. What
    is left is `configure_stream`'s own third state — a stream it could not reach is exactly the one
    that can still crash — and that is reported here as a line somebody can read, the way `doctor`
    reports it for the product, rather than as an exit code nothing under `scripts/` consumes.
    `test_a_harness_script_survives_a_console_that_cannot_encode_its_output` is what fails when a
    script stops calling this, with
    `test_a_strict_console_kills_a_harness_script_that_does_not_configure_its_streams` as the other
    half.
    """
    for report in configure_streams():
        if report["state"] == "could-not":
            safe_write(sys.stderr,
                       f"  ! {report['stream']} could not be configured ({report['reason']}) — a "
                       f"character it cannot encode will still kill this script at the print\n")


def parse_requests(path: Path) -> list[dict]:
    """Parse requests.md into ``[{slug, form, card, request, answers}, …]`` (see the file's own header).

    ``answers`` maps a slot id to the *layers* the fixture client will volunteer about it, in order,
    one per ``answer.<slot>:`` line. A block with no such line is a single-pass request and captures
    exactly as it always did; a block with one is an interactive request and captures a multi-turn
    conversation instead. Repeating a slot is deliberate, not a mistake to reject — it is what keeps
    a capture running past turn 2 (#137). Guarded by `test_parse_requests_collects_a_layered_answer_sheet`."""
    runs: list[dict] = []
    current: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            current = {"slug": line[4:].strip(), "form": "", "card": "", "request": "",
                       "answers": {}}
            runs.append(current)
        elif current is not None and ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            key = key.strip()
            if key in ("form", "card", "request"):
                current[key] = value.strip()
            elif key.startswith("answer."):
                current["answers"].setdefault(key[len("answer."):], []).append(value.strip())
    return [r for r in runs if r["request"]]


def is_interactive(req: dict) -> bool:
    """Does this request drive the interactive multi-turn shape? The answer sheet is the switch, not
    a separate `turns:` key — two declarations of one fact is one of them going stale."""
    return bool(req.get("answers"))


def runs_path(slug: str) -> Path:
    return GOLDEN / f"{slug}.runs.json"


def dump_runs(slug: str, request: str, models: list[EngineOutput],
              briefs: list[Brief] | None = None, *, model: str) -> Path:
    """Persist the K captured models for one request as a single JSON envelope.

    ``briefs`` is optional and captured only for the requests we watch the *assessment* on — it costs
    a second API call per run, so it is opt-in rather than the default (see ``golden_run --brief``).

    ``model`` is **keyword-only and required**: pass the id the call was actually given, never
    `current_model_name()` re-read here, so a capture that cannot say what it ran on cannot be
    written (#515). Guarded by `test_dump_runs_requires_the_model_it_ran_on`."""
    import json
    payload = {"request": request, "model": model, "runs": [m.model_dump() for m in models]}
    if briefs is not None:
        payload["briefs"] = [b.model_dump() for b in briefs]
    path = runs_path(slug)
    # Explicitly UTF-8, matching the read in `golden_diff.py`. `json.dumps` defaults to
    # `ensure_ascii=True`, so today's payload is pure ASCII and the codec never bites -- but the
    # baseline is provider-written prose, and one `ensure_ascii=False` here would silently write a
    # cp1252 file that the next diff reads as a prompt regression that never happened (#11).
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def captured_model(text: str) -> str | None:
    """The model id a `.runs.json` envelope was captured on, or **None** for a baseline written
    before #515 added the key.

    None is a third state and not a default: every baseline on disk the day this landed carries no
    key, and rendering that as agreement with whatever model is configured now is the exact collapse
    `_freshness_from_git_data` already refuses one function along — *`unknown` must never read as
    `current`*. Callers branch on `is None` explicitly. Both envelope shapes carry the key at the
    same top level, so this reads a single-pass and an interactive capture identically."""
    import json
    value = json.loads(text).get("model")
    return value if isinstance(value, str) and value else None


def load_runs(text: str) -> list[EngineOutput]:
    """Parse a `.runs.json` envelope (from disk or `git show`) into its list of models.

    An interactive capture stores the whole conversation under `turns` and no `runs` key at all: the
    model a run *ends* on is the last turn's, so duplicating it would put the same 16 KB on disk
    twice and give a later edit two places to disagree. Reading it back here rather than at each call
    site is what lets `consensus`, `movements`, `stability` and `--questions` treat an interactive
    baseline as an ordinary one without knowing it is (#137)."""
    import json
    payload = json.loads(text)
    if "runs" in payload:
        return [EngineOutput.model_validate(r) for r in payload["runs"]]
    return [EngineOutput.model_validate(run[-1]["model"]) for run in payload["turns"]]


def load_briefs(text: str) -> list[Brief]:
    """The assessments captured alongside the models, or [] if this request doesn't watch them."""
    import json
    payload = json.loads(text)
    return [Brief.model_validate(b) for b in payload.get("briefs", [])]


def load_answers(text: str) -> dict[str, list[str]]:
    """The answer sheet a capture was taken with, or `{}` for a single-pass capture that never had
    one. Persisted alongside the turns (`turn_envelope`) because it is *input*, not output — the
    #163 diagnosis needs to know what the fixture client could still have said, not only what the
    engine went on to ask."""
    import json
    payload = json.loads(text)
    return {sid: list(vals) for sid, vals in payload.get("answers", {}).items()}


def _mode(values: list) -> tuple[object, int]:
    """Most common value and how many of the K runs agree on it."""
    return Counter(values).most_common(1)[0]


def consensus(models: list[EngineOutput]) -> dict:
    """Per-slot consensus over K runs: for each of `impact` and `state`, the modal value and the
    agreement count (how many of K runs share it). Agreement == K means unanimous — the only case a
    later change can be attributed to a real cause rather than sampling noise."""
    n = len(models)
    slot_ids = list(models[0].model.keys())
    out = {"n": n, "slots": {}, "themes": _stable_themes(models)}
    for sid in slot_ids:
        impacts = [str(getattr(m.model[sid].impact, "value", m.model[sid].impact))
                   for m in models if sid in m.model]
        states = [state_of(m.model[sid]) for m in models if sid in m.model]
        out["slots"][sid] = {
            "impact": _mode(impacts),
            "state": _mode(states),
        }
    return out


def _stable_themes(models: list[EngineOutput]) -> set[str]:
    """Question-target labels that appear in a majority of the K runs — the stable focus of the
    engine on this request, as opposed to a theme that showed up in a single noisy run."""
    n = len(models)
    counts: Counter = Counter()
    for m in models:
        for lab in {slot_label(q.slot) for q in m.questions}:
            counts[lab] += 1
    return {lab for lab, c in counts.items() if c > n / 2}


def stability(models: list[EngineOutput]) -> dict:
    """The empirical noise floor for one request: how much of the model is stable enough to diff on.
    Returns counts of unanimous vs jittery slots per dimension, plus the stable question themes."""
    con = consensus(models)
    n = con["n"]
    unan = {"impact": 0, "state": 0}
    jitter = {"impact": 0, "state": 0}
    for meta in con["slots"].values():
        for dim in ("impact", "state"):
            if meta[dim][1] == n:
                unan[dim] += 1
            else:
                jitter[dim] += 1
    return {"n": n, "unanimous": unan, "jitter": jitter,
            "themes": sorted(con["themes"]), "total_slots": len(con["slots"])}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# The assessment lens
#
# Discovery is watched through slots and question themes; the *assessment* is the deliverable, and it
# is mostly prose. Two things in it are comparable across runs: the `complexity` verdict (categorical,
# so consensus applies directly) and the **challenge headlines** — 3–6 words naming the premise being
# contested. Headlines are the assessment's equivalent of a question theme: they say what the engine
# chose to push back on, which is the differentiator we actually care about keeping sharp.
#
# Challenges never repeat verbatim across runs, so they have to be grouped rather than matched. They
# are grouped by the **slot ids they contest** (`Challenge.contests`) — the structural statement of
# what the challenge is about. Word overlap was tried first and is not good enough: the engine
# rephrases at the concept level, not just by reordering, and "Visibility of the superseded signed
# copy" / "Published-document blast radius ignored" are the same challenge with no word in common.
# Under word matching those read as one challenge lost and another gained, which is exactly the false
# alarm this lens exists to avoid. Word overlap survives only as a fallback for captures taken before
# `contests` existed.

_STOPWORDS = {"a", "an", "the", "as", "at", "in", "on", "of", "for", "to", "and", "or", "vs", "is",
              "be", "by", "with", "not", "no", "its", "it", "this", "that", "are", "may", "can"}


def _words(headline: str) -> frozenset[str]:
    """Content words of a headline, lowercased — the key a cluster is matched on."""
    raw = "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in headline).split()
    return frozenset(w for w in raw if w not in _STOPWORDS and len(w) > 2)


def _cluster_headlines(per_run: list[list[str]], threshold: float = 0.4) -> dict[str, int]:
    """Fallback grouping for captures taken before `contests` existed: greedy single-pass clustering
    of headlines on Jaccard overlap of their content words, counting the runs each theme appeared in.

    It only catches rewordings that reuse the same vocabulary. That limit is why challenges are keyed
    on contested slots now — see `_challenge_themes`."""
    clusters: list[dict] = []   # {"words": frozenset, "label": str, "runs": set[int]}
    for run_idx, headlines in enumerate(per_run):
        for headline in headlines:
            words = _words(headline)
            if not words:
                continue
            best, best_score = None, 0.0
            for cluster in clusters:
                union = words | cluster["words"]
                score = len(words & cluster["words"]) / len(union) if union else 0.0
                if score > best_score:
                    best, best_score = cluster, score
            if best is not None and best_score >= threshold:
                best["runs"].add(run_idx)
                best["words"] = best["words"] | words   # absorb the variant so later runs match
            else:
                clusters.append({"words": words, "label": headline, "runs": {run_idx}})
    # `display_token` at the point provider prose becomes a *label*. Every other theme label is
    # `slot_label(slot_id)` — a validated slot id through the schema's table — and this fallback is the
    # one path where a headline is the label, so it is the one path where `golden_diff`'s
    # `assessment + challenge(s) now raised: …` can be handed a newline and write what reads as a
    # second line of the readout at column 0. Squashed where the value enters rather than at each of
    # the four prints it reaches, which is the reasoning `_log_safe` in `scripts/plugin_cli_drift.py`
    # applies to its own sink; `display_token` rather than that helper because a headline is judged
    # on its exact wording here and returns byte-for-byte when it is safe, where `_log_safe` is lossy
    # by design and would silently rewrite the text the lens exists to compare. The name matters:
    # `_one_line`, which that helper is built on, squashes whitespace and nothing else, and #176 is
    # what happens when it is mistaken for the whole answer at a sink a CI runner parses.
    # `test_a_headline_used_as_a_theme_label_cannot_forge_a_line` is what fails if this is removed.
    return {display_token(c["label"]): len(c["runs"]) for c in clusters}


def _challenge_themes(briefs: list[Brief]) -> dict[str, int]:
    """Group the K runs' challenges into themes and count the runs each appeared in.

    Keyed on the contested slot ids where the capture has them, which makes the grouping exact. Falls
    back to headline word overlap only when no run declared `contests` — i.e. a capture predating the
    field. A mixed set is treated as structural: a run that named its slots is not degraded because
    another run didn't."""
    if not any(c.contests for b in briefs for c in b.challenges):
        return _cluster_headlines([[c.headline for c in b.challenges] for b in briefs])

    # A theme is a contested slot, exactly as a question theme is a questioned slot on the discovery
    # side. No similarity threshold to tune, and it survives the engine naming a different set of
    # secondary slots each run — what stays constant across runs is the slot the challenge is really
    # about. Two distinct challenges contesting the same slot do merge; that is the same trade the
    # question-theme lens already makes, and it reads honestly: "in a majority of runs the engine
    # contested something about the workflow".
    runs_per_slot: dict[str, set[int]] = {}
    for run_idx, brief in enumerate(briefs):
        for challenge in brief.challenges:
            for slot_id in challenge.contests:
                runs_per_slot.setdefault(slot_id, set()).add(run_idx)
    return {slot_label(slot_id): len(runs) for slot_id, runs in runs_per_slot.items()}


def brief_consensus(briefs: list[Brief]) -> dict:
    """Per-request consensus over K assessments: the modal complexity verdict with its agreement
    count, the challenge themes that a majority of runs raised, and the shape of the output (how many
    challenges/risks/opportunities it tends to produce)."""
    n = len(briefs)
    complexities = [str(getattr(b.complexity, "value", b.complexity)) for b in briefs]
    themes = _challenge_themes(briefs)
    return {
        "n": n,
        "complexity": _mode(complexities),
        # Unanimity, not a majority. Each challenge names two or three contested slots and a run
        # raises about three challenges, so a majority bar marks nearly every slot as stable and the
        # readout saturates — a lost challenge changes nothing because its neighbours still cover the
        # same slots. Requiring every run to contest a slot is both more discriminating and the same
        # bar `movements()` applies to a strong slot move.
        "themes": {label for label, count in themes.items() if count == n},
        "all_themes": themes,
        "counts": {
            "challenges": [len(b.challenges) for b in briefs],
            "risks": [len(b.risks) for b in briefs],
            "opportunities": [len(b.opportunities) for b in briefs],
            "open_decisions": [len(b.open_decisions) for b in briefs],
        },
    }


def brief_movements(old: list[Brief], new: list[Brief]) -> dict:
    """What changed between two K-run assessment baselines. The complexity verdict is graded strong /
    weak on the same rule as a slot (strong = unanimous before *and* after). Challenge themes are
    reported as gained or lost — a theme the engine used to raise in a majority of runs and no longer
    does is the assessment's version of a regression, and the one this lens exists to catch."""
    co, cn = brief_consensus(old), brief_consensus(new)
    o_val, o_agree = co["complexity"]
    n_val, n_agree = cn["complexity"]
    verdict = None
    if o_agree == co["n"] and n_val != o_val and n_agree > cn["n"] / 2:
        verdict = {"from": o_val, "to": n_val, "old_agree": o_agree,
                   "new_agree": n_agree, "n": cn["n"], "strong": n_agree == cn["n"]}
    return {
        "complexity": verdict,
        "themes_added": sorted(cn["themes"] - co["themes"]),
        "themes_removed": sorted(co["themes"] - cn["themes"]),
        "old_counts": co["counts"], "new_counts": cn["counts"],
    }


def movements(old: list[EngineOutput], new: list[EngineOutput]) -> dict:
    """Changes between two K-run baselines that clear the noise floor, split by how much they can be
    trusted. Both tiers need the OLD baseline unanimous on that dimension (a reliable reference):

    - **strong**: the new consensus is *also* unanimous on a different value. Every run agrees, before
      and after — this cannot be one run's jitter.
    - **weak**: the new consensus is only a majority. At K=3 a majority is 2 of 3, so a single run
      flipping produces one of these. Informative in aggregate, not on its own.

    Reading only the strong tier is the default; the weak tier is worth watching when several land on
    the same slot or the same request. Also reports stable question themes that appeared or vanished.
    """
    co, cn = consensus(old), consensus(new)
    n_new = cn["n"]
    majority = n_new // 2 + 1
    strong, weak = [], []
    for sid, ometa in co["slots"].items():
        nmeta = cn["slots"].get(sid)
        if not nmeta:
            continue
        for dim in ("impact", "state"):
            o_val, o_agree = ometa[dim]
            n_val, n_agree = nmeta[dim]
            if o_agree != co["n"] or n_val == o_val:   # unreliable reference, or nothing moved
                continue
            if n_agree < majority:                     # the new runs don't even agree — pure noise
                continue
            entry = {"slot": slot_label(sid), "dim": dim, "from": o_val, "to": n_val,
                     "old_agree": o_agree, "new_agree": n_agree, "n": n_new}
            (strong if n_agree == n_new else weak).append(entry)
    return {
        "strong": strong,
        "weak": weak,
        "moved": strong + weak,   # kept for callers that want the union
        "themes_added": sorted(cn["themes"] - co["themes"]),
        "themes_removed": sorted(co["themes"] - cn["themes"]),
    }


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# The interactive lens — turn 3 and beyond
#
# Everything above watches a *single-pass* discovery: one request in, one model out, K times. That
# shape cannot see the thing #77 changed. #77 moved the CLI's interactive `discover` loop onto
# `DiscoveryService.draft_turn`, and a turn there sends exactly three messages — the original
# request, the model so far, the answers just given. The old loop grew its message list instead, so
# from turn 3 the earlier rounds of question-and-answer were still on the wire and now are not: the
# model is the only carrier of what the client already said.
#
# Turns 1 and 2 are byte-identical between the two shapes and were verified as such in #77 itself, so
# a two-turn capture measures nothing at all here. Everything below therefore counts from `DEEP_TURN`
# and a capture is only `deep_enough` at `MEASURABLE_DEPTH` — a run that converged at turn 3 is not
# evidence that nothing is wrong, it is a run that never reached the question.
#
# Three things are worth measuring, and each is a way the carried model could be a lossy summary of
# the transcript it replaced:
#
#   reasked   the engine asks again about a slot the client already answered. The transcript is gone,
#             so nothing but the model says the ground was covered — this is the direct symptom.
#   lost      a slot the client answered in turn 1 or 2 is no longer `explicit` in the final model.
#             The client's own words were the evidence; if the model has stopped calling it confirmed,
#             the evidence did not survive the fold.
#   regressed a slot's completeness falls back across a deep turn. The model is meant to accumulate.
#
# All three are counted per run and reported two ways, on the same rule the slot lens already uses:
# the raw per-run count, and the unanimous set. Act on unanimous; at K=3 a single run is jitter.

DEEP_TURN = 3          # the first turn at which the two grounding shapes differ
MEASURABLE_DEPTH = 5   # a capture shallower than this did not reach the question (#137)

# Turns per run, and therefore the budget: an interactive request costs K x TURNS calls where a
# single-pass one costs K. Derived from MEASURABLE_DEPTH rather than written twice, so raising the
# floor can never leave the cap below it — a capture that stopped one turn short of being readable is
# the one failure here that looks exactly like a successful one.
TURNS = int(os.getenv("GOLDEN_TURNS", str(MEASURABLE_DEPTH)))


@dataclass
class Turn:
    """One captured interactive turn: the model the engine returned, and which slots the fixture
    client answered off the back of it. `answered` is not derivable from the model — it is the
    intersection of what was asked and what the answer sheet still had to say — and it is what every
    measurement below is taken against, so it is captured rather than reconstructed."""

    index: int                              # 1-based, matching the loop's own turn counter
    answered: list[str] = field(default_factory=list)
    model: EngineOutput = None              # type: ignore[assignment]


class AnswerSheet:
    """The fixture client. Hands out one layer per slot per ask, in order, and then has nothing more
    to say about it.

    That exhaustion is the point rather than a limitation. A sheet that answered the same slot
    identically forever would never let the conversation move, and a sheet with one answer per slot
    would run dry by turn 3 — layers are what make a capture reach turn 5 the way a real elicitation
    does, by having something further to say each time the engine comes back."""

    def __init__(self, layers: dict[str, list[str]]):
        self._layers = {sid: list(vals) for sid, vals in layers.items()}

    def reply_for(self, slot: str) -> str | None:
        """The next layer for this slot, or None — this client has nothing (further) to say about it.
        None is the fixture's version of a user pressing Enter at the prompt, which `converse()`
        treats as a skipped question rather than as an answer."""
        queue = self._layers.get(slot)
        return queue.pop(0) if queue else None

    def remaining(self) -> dict[str, int]:
        """Layers this client still had something further to say about, keyed by slot.

        The #163 diagnosis for a run that stopped early: it may have converged because the sheet
        ran dry, or it may have stopped for want of a question the engine never came back to ask,
        in which case the sheet still holds ground unreached. A slot the sheet has fully spoken
        about does not appear at all — an unreached count of zero and "the sheet was never asked
        about this slot" would otherwise read the same way, and neither is a finding."""
        return {sid: len(queue) for sid, queue in self._layers.items() if queue}


def answer_line(slot: str, question: str, reply: str) -> str:
    """One answered question, in the exact words `converse()` sends.

    This capture is only a measurement of the interactive path for as long as it hands the seam the
    same bytes a user would, and a differently-shaped answer block still reasons and still returns a
    plausible model — so the drift would be invisible in the output.
    `test_the_golden_harness_answers_a_turn_in_exactly_the_words_this_loop_does` is what fails when
    either side is reworded."""
    return f"[slot: {slot}] Q: {question} → A: {reply}"


def answers_for_turn(questions, sheet: AnswerSheet) -> tuple[str | None, list[str]]:
    """The answer block for one turn, and the slots it actually spoke to.

    Returns `(None, [])` when the sheet could not answer a single question — which ends the capture,
    exactly as `converse()` stops on an empty reply list. Continuing would pay for turns carrying no
    new client input, and would let a run reach `MEASURABLE_DEPTH` without deserving it."""
    replies, answered = [], []
    for q in questions:
        reply = sheet.reply_for(q.slot)
        if reply is None:
            continue
        replies.append(answer_line(q.slot, q.q, reply))
        answered.append(q.slot)
    return ("\n".join(replies) if replies else None), answered


def turn_envelope(request: str, layers: dict[str, list[str]], runs: list[list[Turn]],
                  *, model: str) -> str:
    """Serialize an interactive capture. The answer sheet is stored alongside the turns because it is
    *input*: a diff whose sheet changed is not a diff about the engine, and without the sheet on disk
    there is no way to tell those apart. ``model`` is required for the reason `dump_runs` states at
    length (#515) — the two writers are the two places a capture's conditions can be recorded, so a
    key present in one and absent from the other would make the readout's third state depend on
    which shape of request you happened to capture."""
    import json
    return json.dumps({
        "request": request,
        "model": model,
        "answers": {sid: list(vals) for sid, vals in layers.items()},
        "turns": [[{"index": t.index, "answered": t.answered, "model": t.model.model_dump()}
                   for t in run] for run in runs],
    }, indent=2)


def dump_turn_runs(slug: str, request: str, layers: dict[str, list[str]],
                   runs: list[list[Turn]], *, model: str) -> Path:
    """Persist an interactive capture. Explicitly UTF-8 for the reason `dump_runs` gives (#11)."""
    path = runs_path(slug)
    path.write_text(turn_envelope(request, layers, runs, model=model), encoding="utf-8")
    return path


def load_turns(text: str) -> list[list[Turn]] | None:
    """The captured conversations, or **None** when this baseline is single-pass.

    None is a third state and not an empty result: a single-pass capture has nothing to say about
    turn 3, and every caller has to render that differently from a multi-turn capture that found
    nothing wrong. Returning `[]` here would make the two indistinguishable."""
    import json
    payload = json.loads(text)
    raw = payload.get("turns")
    if raw is None:
        return None
    return [[Turn(index=t["index"], answered=list(t["answered"]),
                  model=EngineOutput.model_validate(t["model"])) for t in run] for run in raw]


def _reasked_in(run: list[Turn]) -> set[str]:
    """Slots the engine asked about at `DEEP_TURN` or later having already been answered."""
    covered: set[str] = set()
    out: set[str] = set()
    for turn in run:
        if turn.index >= DEEP_TURN:
            out |= {slot_label(q.slot) for q in turn.model.questions if q.slot in covered}
        # after, not before: a turn's `answered` is the reply *to* that turn's questions, so it is
        # only "already covered" from the following turn on.
        covered.update(turn.answered)
    return out


def _lost_in(run: list[Turn]) -> set[str]:
    """Slots the client answered before `DEEP_TURN` that the final model no longer calls confirmed.

    A slot the client spoke to directly is explicit evidence. If it reads inferred or unknown at the
    end, the fold from turn 3 onward did not carry the client's own words forward — which is the
    difference between the two grounding shapes, stated as an outcome."""
    early = {s for t in run if t.index < DEEP_TURN for s in t.answered}
    final = run[-1].model.model
    return {slot_label(sid) for sid in early
            if sid not in final or state_of(final[sid]) != "confirmed"}


def _regressed_in(run: list[Turn]) -> set[str]:
    """Slots whose completeness fell back across a turn boundary at `DEEP_TURN` or later."""
    out: set[str] = set()
    for before, after in zip(run, run[1:]):
        if after.index < DEEP_TURN:
            continue
        later = after.model.model
        out |= {slot_label(sid) for sid, slot in before.model.model.items()
                if sid in later and later[sid].completeness < slot.completeness}
    return out


def unreached_layers(layers: dict[str, list[str]], runs: list[list[Turn]]) -> dict[str, int]:
    """How much of the answer sheet this capture's K runs never got to, per slot, labeled.

    The #163 diagnosis for a capture that stayed SHALLOW: a run that converged early may have done
    so because the sheet ran dry, or it may have stopped for want of a question the engine never
    came back to ask, in which case the sheet still holds unreached ground -- the diagnosis that had
    to be run by hand to explain the 4/5/4 depths.

    A layer only counts as unreached when *every* run in the capture left it on the sheet: if even
    one run's conversation got that far, the layer was reachable and the sheet is not why the
    capture stayed shallow -- some other run simply took a different path through the questions.
    Replayed through a fresh `AnswerSheet` per run, consuming exactly the slots that run's own
    `Turn.answered` record says it consumed, rather than re-deriving the arithmetic separately, so
    this can never disagree with what the capture actually asked and answered."""
    per_run: list[dict[str, int]] = []
    for run in runs:
        sheet = AnswerSheet(layers)
        for turn in run:
            for slot in turn.answered:
                sheet.reply_for(slot)
        per_run.append(sheet.remaining())
    out: dict[str, int] = {}
    for sid in layers:
        left = min(r.get(sid, 0) for r in per_run) if per_run else 0
        if left:
            out[slot_label(sid)] = left
    return out


def turn_lens(runs: list[list[Turn]] | None, layers: dict[str, list[str]] | None = None) -> dict:
    """What the K captured conversations say about grounding from `DEEP_TURN` onward.

    With nothing to read it returns `{"measured": False, "reason": …}` and **no finding keys at
    all** — deliberately, so a caller cannot iterate an empty `reasked` and print a clean bill of
    health for a capture that was never looked at. Guarded by
    `test_the_lens_says_it_could_not_look_rather_than_reporting_nothing`.

    `layers` is the answer sheet this capture was taken with (#163), and it is optional: pass it and
    the result carries `unreached_layers`; omit it and the lens reads exactly as it always has.
    Guarded by `test_turn_lens_carries_unreached_layers_only_when_given_a_sheet` and
    `test_unreached_layers_reports_what_no_run_in_the_capture_ever_got_to`."""
    if not runs:
        return {"measured": False,
                "reason": "single-pass capture — no turns to read, so nothing here speaks to the "
                          "grounding from turn 3 onward"}
    n = len(runs)
    depths = [len(r) for r in runs]
    found = {"reasked": Counter(), "lost": Counter(), "regressed": Counter()}
    for run in runs:
        for key, fn in (("reasked", _reasked_in), ("lost", _lost_in), ("regressed", _regressed_in)):
            for label in fn(run):
                found[key][label] += 1
    out = {
        "measured": True,
        "n": n,
        "depths": depths,
        # Not a pass/fail: it is the reader's warning that a run which stopped early cannot be quoted
        # as evidence of a clean deep turn.
        "deep_enough": min(depths) >= MEASURABLE_DEPTH,
        "reasked": dict(found["reasked"]),
        "lost": dict(found["lost"]),
        "regressed": dict(found["regressed"]),
        "unanimous": {key: sorted(lab for lab, c in counter.items() if c == n)
                      for key, counter in found.items()},
    }
    if layers:
        out["unreached_layers"] = unreached_layers(layers, runs)
    return out


def turn_movements(old: list[list[Turn]] | None, new: list[list[Turn]] | None) -> dict:
    """What changed between two interactive baselines, on the unanimous tier.

    `measured: False` when either side is single-pass. That is the ordinary state the first time an
    interactive request is captured, and it has to say so rather than report an empty diff — a
    request that has just become interactive and a request whose deep turns are clean would otherwise
    print the same thing."""
    if not old or not new:
        missing = "the baseline in HEAD" if not old else "the working-tree capture"
        return {"measured": False,
                "reason": f"{missing} has no turns — an interactive capture can only be compared "
                          f"against another interactive capture"}
    lens_old, lens_new = turn_lens(old), turn_lens(new)
    out = {"measured": True, "depths": {"from": lens_old["depths"], "to": lens_new["depths"]}}
    for key in ("reasked", "lost", "regressed"):
        before = set(lens_old["unanimous"][key])
        after = set(lens_new["unanimous"][key])
        out[f"{key}_added"] = sorted(after - before)
        out[f"{key}_removed"] = sorted(before - after)
    return out

# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Baseline freshness — is a committed capture even current with respect to what it measures?
#
# `golden_diff` compares a fresh working-tree capture against the baseline committed in HEAD and
# reports what moved, but nothing in that comparison says whether the *baseline itself* was captured
# against the prompt/context/framework assets and generator code the tree carries today, or against
# an older version of them. So an editor who changes one prompt and runs the harness sees the
# combined effect of their own edit *and* every earlier asset change nobody re-captured against, with
# no way to tell the two apart — the reader becomes the control, silently. That happened twice, and
# both instances fund this per CLAUDE.md's own meta-guard budget rule (two named instances, folded
# into the existing golden_lib/golden_diff tier rather than a new one):
#
#   #405  three commits under `src/requivo/assets/{prompts,context,framework}/` landed between one
#         committed baseline and the next, and nothing said so — the baseline quietly answered a
#         different question than `golden_diff` reported it as answering, for a month.
#   #410  `ba526f6` dropped `indent=2` from the JSON `generators.py` sends as the *user* message for
#         every `--brief` capture (`advise(...)`'s `out.model_dump_json()`). `prompt_version()` only
#         hashes the *system* prompt and `tests/test_golden_baselines.py` only compares `request`/
#         `answers` against `requests.md`, so nothing in the tree could see it: every committed
#         `--brief` baseline was captured against a user message the tree no longer sends, three
#         commits after the #405 fix landed, and the suite stayed green throughout.
#
# `WATCHED_PATHS` is scoped to exactly those two instances — the assets a capture's system prompt is
# built from, and the one module that assembles every on-wire *user* message. It is deliberately not
# exhaustive of everything that can change what a capture measures: `core/context.py`'s own assembly
# logic, `completion.py`'s retry/JSON-extraction behaviour, and the model id itself are all real ways
# a capture's meaning can move without a commit under `WATCHED_PATHS` — none of them has a reproduced
# instance backing it yet, so none is in scope (the same bar CLAUDE.md's meta-guard section applies
# to a new check generally). `baseline_commits_since` and everything downstream of it says exactly
# which paths it checked, in its own output, rather than reading as coverage it does not have.
WATCHED_PATHS: tuple[str, ...] = (
    "src/requivo/assets/prompts",
    "src/requivo/assets/context",
    "src/requivo/assets/framework",
    "src/requivo/providers/anthropic/generators.py",
)


def _git(args: list[str]) -> tuple[bool, str]:
    """Run one git command inside REPO. Never raises: git being unavailable, this being a shallow
    clone, or a path having no history in HEAD are all things a caller has to report, never crash on
    -- the same rule `turn_lens` already applies to its own "nothing to measure" case, one section up.

    Captured as **bytes** and decoded explicitly with `.decode("utf-8")` (invariant 16) --
    deliberately not `subprocess.run(text=True)`, whose universal-newlines translation used to rewrite
    a raw `\r` in a commit subject into a second, forged `\n`-terminated record downstream (#456).
    Decoding bytes here, once, removes the rewrite for every caller of `_git`, present and future.
    Guarded by `test_a_hostile_commit_subject_cannot_forge_a_second_commit_row`.

    `UnicodeDecodeError` is caught alongside the process-launch failures so a non-UTF-8 byte
    in a commit subject is a reported `unknown`, never a crash mid-readout."""
    try:
        res = subprocess.run(["git", *args], cwd=REPO, capture_output=True, timeout=10)
        stdout = res.stdout.decode("utf-8")
        stderr = res.stderr.decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        return False, str(exc)
    if res.returncode != 0:
        return False, stderr.strip() or "git exited non-zero with no stderr"
    return True, stdout


_SEP = "\x1f"  # unit separator: unlike "|" or ":" it never appears in a commit subject


def _freshness_from_git_data(is_shallow: bool | None, baseline: tuple[str, str] | None,
                              since_commits: list[dict] | None,
                              baseline_error: str | None = None) -> dict:
    """The pure core `baseline_commits_since` wraps: three states over already-fetched git data, so
    they can be exercised without a real repository (see `tests/test_golden_lib.py`).

    `None` in `baseline`/`since_commits` stands for "that git call found nothing to answer with" --
    see `baseline_commits_since` for what produces each one. `baseline_error` is a *different* shape
    of `None`: the git call for the baseline's own commit did not merely come back empty, it failed
    outright (git unavailable, corrupted object, permission error), and that has its own reason
    rather than folding into "no commit history" -- a real failure and a genuinely history-less path
    are different facts a maintainer would investigate differently, and collapsing them was reported
    as a finding on this function's first review (self-review of #405, before the fold happened).

    `is_shallow=True` gets its own `unknown` reason rather than folding into a git-failed case: the
    git calls all *succeed* on a shallow clone, they just answer a truncated question -- the one
    input here that fails silently rather than loudly, which is exactly the shape `golden_diff`'s own
    docstring already refuses for a byte comparison ("a capture identical to HEAD reports not
    re-captured, never no change"). This is that rule one layer up, for a commit *count* instead."""
    if is_shallow is None:
        return {"state": "unknown", "reason": "could not tell whether this is a shallow clone"}
    if is_shallow:
        return {"state": "unknown",
                "reason": "shallow clone -- commit history is truncated, so a count of commits "
                          "since the baseline cannot be trusted"}
    if baseline_error is not None:
        return {"state": "unknown",
                "reason": f"git log (last commit touching the baseline) failed: {baseline_error}"}
    if baseline is None:
        return {"state": "unknown", "reason": "no commit history for this baseline in HEAD"}
    if since_commits is None:
        return {"state": "unknown", "reason": "git log (commits since the baseline) failed"}
    _, captured_at = baseline
    return {"state": "stale" if since_commits else "current",
            "captured_at": captured_at, "commits": since_commits}


def baseline_commits_since(rel_path: str, watched: tuple[str, ...] = WATCHED_PATHS) -> dict:
    """Is the committed baseline at `rel_path` (relative to REPO -- e.g.
    ``fixtures/golden/<slug>.runs.json``) current with respect to `watched`, or how many commits
    landed since its own last commit in HEAD touching one of those paths.

    Three states in the returned dict's `state`: ``current``, ``stale`` (with `commits`, oldest
    first), or ``unknown`` (with `reason`) -- see `_freshness_from_git_data` for what decides each.
    `unknown` must never be read as `current` by a caller: every caller in this file branches on
    `state` explicitly rather than only checking `commits`, because an empty `commits` list means two
    different things depending on which state it sits under."""
    ok, shallow_out = _git(["rev-parse", "--is-shallow-repository"])
    is_shallow = (shallow_out.strip() == "true") if ok else None

    ok, log_out = _git(["log", "-1", f"--format=%H{_SEP}%cI", "HEAD", "--", rel_path])
    baseline = None
    # A `False` here is the git call itself failing, not the ordinary "no history yet" case below --
    # kept apart so `_freshness_from_git_data` can give each its own reason.
    baseline_error = None if ok else log_out
    if ok and log_out.strip():
        sha, _, captured_at = log_out.strip().partition(_SEP)
        baseline = (sha, captured_at)

    since_commits = None
    if baseline is not None:
        # --reverse: git log's default is newest-first, and the docstring above promises oldest
        # first -- the order that matters for `golden_diff`'s own truncation (`commits[:5]`), since
        # the *earliest* watched-path commit after the baseline is usually the one that actually
        # started the drift, and hiding it behind "... and N more" would bury the lead.
        ok, since_out = _git(["log", "--reverse", f"--format=%H{_SEP}%cI{_SEP}%s",
                              f"{baseline[0]}..HEAD", "--", *watched])
        if ok:
            since_commits = []
            # split("\n"), never splitlines() (#456): splitlines() treats nine characters as ending a
            # line -- none of which ends a `git log --format` record, only the literal `\n` git itself
            # writes between entries does. Guarded by
            # test_a_hostile_commit_subject_cannot_forge_a_second_commit_row.
            for line in since_out.split("\n"):
                if not line:
                    continue
                sha, _, rest = line.partition(_SEP)
                date, _, subject = rest.partition(_SEP)
                since_commits.append({"sha": sha[:9], "date": date[:10], "subject": subject})

    return _freshness_from_git_data(is_shallow, baseline, since_commits, baseline_error)

