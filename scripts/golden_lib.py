"""Shared logic for the golden regression harness (K-run consensus).

The model family in use exposes no sampling controls, so one capture cannot be pinned and noise
drowns an asset's effect. Each request is captured K times and reasoned about by *consensus*: a
dimension is a signal only when stable across the runs. `golden_run` captures, `golden_diff`
compares. `baseline_commits_since` separately answers whether a committed baseline predates a change
to what it measures (#405, #410)."""

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
from requivo.core.perimeters import DEFAULT_PERIMETER  # noqa: E402
from requivo.core.selectors import display_token  # noqa: E402
from requivo.streams import configure_streams, safe_write  # noqa: E402

GOLDEN = REPO / "fixtures" / "golden"
REQUESTS = GOLDEN / "requests.md"
K = int(os.getenv("GOLDEN_K", "3"))  # runs per request; 3 is the approved default


def configure_output() -> None:
    """Make this script's stdout and stderr unable to kill it on a character they cannot encode.

        Invariant 16 for the harness (#164): `backslashreplace`, never `replace`. No `EXIT_RENDER_FAILED`
        arm, by decision: `golden_diff` writes nothing, and `golden_run` writes each baseline before any
        summary print, so a render failure misreports no work; an unreachable stream is reported as a line.
        `test_a_harness_script_survives_a_console_that_cannot_encode_its_output` and
        `test_a_strict_console_kills_a_harness_script_that_does_not_configure_its_streams`.
    """
    for report in configure_streams():
        if report["state"] == "could-not":
            safe_write(sys.stderr,
                       f"  ! {report['stream']} could not be configured ({report['reason']}) — a "
                       f"character it cannot encode will still kill this script at the print\n")


def parse_requests(path: Path) -> list[dict]:
    """Parse requests.md into ``[{slug, form, card, perimeter, request, answers}, …]``.

        ``answers`` maps a slot id to its ordered *layers*, one per ``answer.<slot>:`` line; a repeated
        slot is deliberate, it keeps a capture past turn 2 (#137,
        `test_parse_requests_collects_a_layered_answer_sheet`). ``perimeter`` defaults to software (#621,
        `test_perimeter_defaults_to_software_and_reads_an_explicit_value`).
    """
    runs: list[dict] = []
    current: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            current = {"slug": line[4:].strip(), "form": "", "card": "",
                       "perimeter": DEFAULT_PERIMETER, "request": "", "answers": {}}
            runs.append(current)
        elif current is not None and ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            key = key.strip()
            if key in ("form", "card", "perimeter", "request"):
                current[key] = value.strip()
            elif key.startswith("answer."):
                current["answers"].setdefault(key[len("answer."):], []).append(value.strip())
    return [r for r in runs if r["request"]]


def is_interactive(req: dict) -> bool:
    """Does this request drive the interactive shape? The answer sheet is the switch; no separate key."""
    return bool(req.get("answers"))


def runs_path(slug: str) -> Path:
    return GOLDEN / f"{slug}.runs.json"


def dump_runs(slug: str, request: str, models: list[EngineOutput],
              briefs: list[Brief] | None = None, *, model: str,
              perimeter: str = DEFAULT_PERIMETER) -> Path:
    """Persist the K captured models for one request as a single JSON envelope.

        ``briefs`` is opt-in (``--brief``). ``model`` is keyword-only and required — the id the call was
        given, never re-read (#515, `test_dump_runs_requires_the_model_it_ran_on`). ``perimeter`` defaults
        to software: every capture before #621 ran under it
        (`test_captured_perimeter_round_trips_and_defaults_to_software`).
    """
    import json
    payload = {"request": request, "model": model, "perimeter": perimeter,
               "runs": [m.model_dump() for m in models]}
    if briefs is not None:
        payload["briefs"] = [b.model_dump() for b in briefs]
    path = runs_path(slug)
    # Explicitly UTF-8: the baseline is provider prose, and a locale codec would fake a regression (#11).
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def captured_model(text: str) -> str | None:
    """The model id a `.runs.json` envelope was captured on, or **None** before #515 — never agreement."""
    import json
    value = json.loads(text).get("model")
    return value if isinstance(value, str) and value else None


def captured_perimeter(text: str) -> str:
    """The perimeter a `.runs.json` envelope was captured under; a missing key is software (#621, #608)."""
    import json
    value = json.loads(text).get("perimeter")
    return value if isinstance(value, str) and value else DEFAULT_PERIMETER


def load_runs(text: str, *, perimeter: str = DEFAULT_PERIMETER) -> list[EngineOutput]:
    """Parse a `.runs.json` envelope (from disk or `git show`) into its list of models.

        An interactive capture stores only `turns`; its run's model is the last turn's, read back here so
        every lens treats it as an ordinary baseline (#137). ``perimeter`` must be the envelope's own, or a
        go-to-market capture's slots fail against software's schema (#621,
        `test_a_first_go_to_market_capture_is_read_back_and_reported_honestly`).
    """
    import json
    payload = json.loads(text)
    ctx = {"perimeter": perimeter}
    if "runs" in payload:
        return [EngineOutput.model_validate(r, context=ctx) for r in payload["runs"]]
    return [EngineOutput.model_validate(run[-1]["model"], context=ctx) for run in payload["turns"]]


def load_briefs(text: str) -> list[Brief]:
    """The assessments captured alongside the models, or [] if this request doesn't watch them."""
    import json
    payload = json.loads(text)
    return [Brief.model_validate(b) for b in payload.get("briefs", [])]


def load_answers(text: str) -> dict[str, list[str]]:
    """The answer sheet a capture was taken with, or `{}`: it is *input*, needed by the #163 diagnosis."""
    import json
    payload = json.loads(text)
    return {sid: list(vals) for sid, vals in payload.get("answers", {}).items()}


def _mode(values: list) -> tuple[object, int]:
    """Most common value and how many of the K runs agree on it."""
    return Counter(values).most_common(1)[0]


def consensus(models: list[EngineOutput], *, perimeter: str = DEFAULT_PERIMETER) -> dict:
    """Per-slot consensus over K runs: for `impact` and `state`, the modal value and agreement count.

        Agreement == K (unanimous) is the only case a later change can be attributed to a real cause.
        ``perimeter`` labels the stable themes from the capture's own schema (#621).
    """
    n = len(models)
    slot_ids = list(models[0].model.keys())
    out = {"n": n, "slots": {}, "themes": _stable_themes(models, perimeter=perimeter)}
    for sid in slot_ids:
        impacts = [str(getattr(m.model[sid].impact, "value", m.model[sid].impact))
                   for m in models if sid in m.model]
        states = [state_of(m.model[sid]) for m in models if sid in m.model]
        out["slots"][sid] = {
            "impact": _mode(impacts),
            "state": _mode(states),
        }
    return out


def _stable_themes(models: list[EngineOutput], *, perimeter: str = DEFAULT_PERIMETER) -> set[str]:
    """Question-target labels in a majority of the K runs: the engine's stable focus on this request."""
    n = len(models)
    counts: Counter = Counter()
    for m in models:
        for lab in {slot_label(q.slot, perimeter) for q in m.questions}:
            counts[lab] += 1
    return {lab for lab, c in counts.items() if c > n / 2}


def stability(models: list[EngineOutput], *, perimeter: str = DEFAULT_PERIMETER) -> dict:
    """The empirical noise floor for one request: unanimous vs jittery slots per dimension, plus stable themes."""
    con = consensus(models, perimeter=perimeter)
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
# The assessment lens: the `complexity` verdict and the challenges, grouped by the slot ids they
# contest (`Challenge.contests`) — the engine rephrases at the concept level, so word overlap read
# one challenge as lost and gained. Word overlap survives only for captures predating `contests`.

_STOPWORDS = {"a", "an", "the", "as", "at", "in", "on", "of", "for", "to", "and", "or", "vs", "is",
              "be", "by", "with", "not", "no", "its", "it", "this", "that", "are", "may", "can"}


def _words(headline: str) -> frozenset[str]:
    """Content words of a headline, lowercased — the key a cluster is matched on."""
    raw = "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in headline).split()
    return frozenset(w for w in raw if w not in _STOPWORDS and len(w) > 2)


def _cluster_headlines(per_run: list[list[str]], threshold: float = 0.4) -> dict[str, int]:
    """Fallback for captures predating `contests`: cluster headlines on Jaccard overlap of content words."""
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
    # `display_token` where a headline becomes a label, since it reaches golden_diff's prints: a newline
    # could forge a readout line. Not plugin_cli_drift's lossy `_log_safe`: the wording is what is judged.
    # test_a_headline_used_as_a_theme_label_cannot_forge_a_line.
    return {display_token(c["label"]): len(c["runs"]) for c in clusters}


def _challenge_themes(briefs: list[Brief]) -> dict[str, int]:
    """Group the K runs' challenges into themes and count the runs each appeared in.

        Keyed on contested slot ids; headline overlap only when no run declared `contests`.
    """
    if not any(c.contests for b in briefs for c in b.challenges):
        return _cluster_headlines([[c.headline for c in b.challenges] for b in briefs])

    # A theme is a contested slot, as a question theme is a questioned slot; no threshold to tune.
    runs_per_slot: dict[str, set[int]] = {}
    for run_idx, brief in enumerate(briefs):
        for challenge in brief.challenges:
            for slot_id in challenge.contests:
                runs_per_slot.setdefault(slot_id, set()).add(run_idx)
    # Labels against software's schema: `capture()` refuses `--brief` for any other perimeter. Thread
    # the perimeter through if one gets its own assessment (#621).
    return {slot_label(slot_id): len(runs) for slot_id, runs in runs_per_slot.items()}


def brief_consensus(briefs: list[Brief]) -> dict:
    """Consensus over K assessments: modal complexity, majority challenge themes, and output shape."""
    n = len(briefs)
    complexities = [str(getattr(b.complexity, "value", b.complexity)) for b in briefs]
    themes = _challenge_themes(briefs)
    return {
        "n": n,
        "complexity": _mode(complexities),
        # Unanimity, not a majority: a majority bar saturates, since each challenge contests two or three slots.
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
    """What changed between two K-run assessment baselines: complexity graded strong/weak, themes gained or lost."""
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


def movements(old: list[EngineOutput], new: list[EngineOutput], *,
             perimeter: str = DEFAULT_PERIMETER) -> dict:
    """Changes between two K-run baselines that clear the noise floor, split by trust.

        Both tiers need the OLD baseline unanimous. **strong**: the new consensus is unanimous on a
        different value; **weak**: only a majority (one run flipping at K=3). Also reports stable question
        themes gained or lost. One ``perimeter``: `diff_one` has refused a mismatch already (#621).
    """
    co, cn = consensus(old, perimeter=perimeter), consensus(new, perimeter=perimeter)
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
            entry = {"slot": slot_label(sid, perimeter), "dim": dim, "from": o_val, "to": n_val,
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
# The interactive lens — turn 3 and beyond. `DiscoveryService.draft_turn` sends the request, the model
# and the latest answers (#77), so from `DEEP_TURN` the model is the only carrier of what the client
# said; turns 1-2 are byte-identical to the old loop. Measured per run and as the unanimous set:
#   reasked   a slot already answered is asked again;
#   lost      a slot answered in turn 1 or 2 is no longer `explicit` at the end;
#   regressed a slot's completeness falls back across a deep turn.

DEEP_TURN = 3          # the first turn at which the two grounding shapes differ
MEASURABLE_DEPTH = 5   # a capture shallower than this did not reach the question (#137)

# Turns per run (the budget: K x TURNS calls), derived from MEASURABLE_DEPTH so the cap never falls below it.
TURNS = int(os.getenv("GOLDEN_TURNS", str(MEASURABLE_DEPTH)))


@dataclass
class Turn:
    """One captured interactive turn: the returned model, and the slots the fixture client answered."""

    index: int                              # 1-based, matching the loop's own turn counter
    answered: list[str] = field(default_factory=list)
    model: EngineOutput = None              # type: ignore[assignment]


class AnswerSheet:
    """The fixture client: one layer per slot per ask, in order, then nothing more — layers let a capture run deep."""

    def __init__(self, layers: dict[str, list[str]]):
        self._layers = {sid: list(vals) for sid, vals in layers.items()}

    def reply_for(self, slot: str) -> str | None:
        """The next layer for this slot, or None (the fixture's Enter: a skipped question)."""
        queue = self._layers.get(slot)
        return queue.pop(0) if queue else None

    def remaining(self) -> dict[str, int]:
        """Layers the client still had to give, by slot (#163); a fully spoken slot does not appear."""
        return {sid: len(queue) for sid, queue in self._layers.items() if queue}


def answer_line(slot: str, question: str, reply: str) -> str:
    """One answered question, in the exact words `converse()` sends.

        Any drift would still reason plausibly, so it is pinned:
        `test_the_golden_harness_answers_a_turn_in_exactly_the_words_this_loop_does`.
    """
    return f"[slot: {slot}] Q: {question} → A: {reply}"


def answers_for_turn(questions, sheet: AnswerSheet) -> tuple[str | None, list[str]]:
    """The answer block for one turn and the slots it spoke to; `(None, [])` ends the capture, as in `converse()`."""
    replies, answered = [], []
    for q in questions:
        reply = sheet.reply_for(q.slot)
        if reply is None:
            continue
        replies.append(answer_line(q.slot, q.q, reply))
        answered.append(q.slot)
    return ("\n".join(replies) if replies else None), answered


def turn_envelope(request: str, layers: dict[str, list[str]], runs: list[list[Turn]],
                  *, model: str, perimeter: str = DEFAULT_PERIMETER) -> str:
    """Serialize an interactive capture, with its answer sheet (input) and its `model`/`perimeter`, as `dump_runs`."""
    import json
    return json.dumps({
        "request": request,
        "model": model,
        "perimeter": perimeter,
        "answers": {sid: list(vals) for sid, vals in layers.items()},
        "turns": [[{"index": t.index, "answered": t.answered, "model": t.model.model_dump()}
                   for t in run] for run in runs],
    }, indent=2)


def dump_turn_runs(slug: str, request: str, layers: dict[str, list[str]],
                   runs: list[list[Turn]], *, model: str,
                   perimeter: str = DEFAULT_PERIMETER) -> Path:
    """Persist an interactive capture. Explicitly UTF-8 for the reason `dump_runs` gives (#11)."""
    path = runs_path(slug)
    path.write_text(turn_envelope(request, layers, runs, model=model, perimeter=perimeter),
                    encoding="utf-8")
    return path


def load_turns(text: str, *, perimeter: str = DEFAULT_PERIMETER) -> list[list[Turn]] | None:
    """The captured conversations, or **None** when single-pass (not `[]`, which would read as clean).

        ``perimeter``: as `load_runs` (#621).
    """
    import json
    payload = json.loads(text)
    raw = payload.get("turns")
    if raw is None:
        return None
    ctx = {"perimeter": perimeter}
    return [[Turn(index=t["index"], answered=list(t["answered"]),
                  model=EngineOutput.model_validate(t["model"], context=ctx)) for t in run]
            for run in raw]


def _reasked_in(run: list[Turn], *, perimeter: str = DEFAULT_PERIMETER) -> set[str]:
    """Slots the engine asked about at `DEEP_TURN` or later having already been answered."""
    covered: set[str] = set()
    out: set[str] = set()
    for turn in run:
        if turn.index >= DEEP_TURN:
            out |= {slot_label(q.slot, perimeter) for q in turn.model.questions if q.slot in covered}
        # after, not before: a turn's `answered` covers the slot only from the following turn.
        covered.update(turn.answered)
    return out


def _lost_in(run: list[Turn], *, perimeter: str = DEFAULT_PERIMETER) -> set[str]:
    """Slots the client answered before `DEEP_TURN` that the final model no longer calls confirmed."""
    early = {s for t in run if t.index < DEEP_TURN for s in t.answered}
    final = run[-1].model.model
    return {slot_label(sid, perimeter) for sid in early
            if sid not in final or state_of(final[sid]) != "confirmed"}


def _regressed_in(run: list[Turn], *, perimeter: str = DEFAULT_PERIMETER) -> set[str]:
    """Slots whose completeness fell back across a turn boundary at `DEEP_TURN` or later."""
    out: set[str] = set()
    for before, after in zip(run, run[1:]):
        if after.index < DEEP_TURN:
            continue
        later = after.model.model
        out |= {slot_label(sid, perimeter) for sid, slot in before.model.model.items()
                if sid in later and later[sid].completeness < slot.completeness}
    return out


def unreached_layers(layers: dict[str, list[str]], runs: list[list[Turn]], *,
                     perimeter: str = DEFAULT_PERIMETER) -> dict[str, int]:
    """How much of the answer sheet no run of this capture reached, per slot, labeled (#163).

        A layer counts only when *every* run left it; replayed through a fresh `AnswerSheet` per run from
        each `Turn.answered`, so it cannot disagree with what the capture did.
    """
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
            out[slot_label(sid, perimeter)] = left
    return out


def turn_lens(runs: list[list[Turn]] | None, layers: dict[str, list[str]] | None = None, *,
             perimeter: str = DEFAULT_PERIMETER) -> dict:
    """What the K captured conversations say about grounding from `DEEP_TURN` onward.

        With nothing to read: `{"measured": False, "reason": …}` and no finding keys, so no caller prints
        a clean bill (`test_the_lens_says_it_could_not_look_rather_than_reporting_nothing`). `layers`
        adds `unreached_layers` (#163, `test_turn_lens_carries_unreached_layers_only_when_given_a_sheet`,
        `test_unreached_layers_reports_what_no_run_in_the_capture_ever_got_to`).
    """
    if not runs:
        return {"measured": False,
                "reason": "single-pass capture — no turns to read, so nothing here speaks to the "
                          "grounding from turn 3 onward"}
    n = len(runs)
    depths = [len(r) for r in runs]
    found = {"reasked": Counter(), "lost": Counter(), "regressed": Counter()}
    for run in runs:
        for key, fn in (("reasked", _reasked_in), ("lost", _lost_in), ("regressed", _regressed_in)):
            for label in fn(run, perimeter=perimeter):
                found[key][label] += 1
    out = {
        "measured": True,
        "n": n,
        "depths": depths,
        # A warning, not a verdict: a run that stopped early is no evidence of a clean deep turn.
        "deep_enough": min(depths) >= MEASURABLE_DEPTH,
        "reasked": dict(found["reasked"]),
        "lost": dict(found["lost"]),
        "regressed": dict(found["regressed"]),
        "unanimous": {key: sorted(lab for lab, c in counter.items() if c == n)
                      for key, counter in found.items()},
    }
    if layers:
        out["unreached_layers"] = unreached_layers(layers, runs, perimeter=perimeter)
    return out


def turn_movements(old: list[list[Turn]] | None, new: list[list[Turn]] | None, *,
                   perimeter: str = DEFAULT_PERIMETER) -> dict:
    """What changed between two interactive baselines, on the unanimous tier; `measured: False` if either is single-pass."""
    if not old or not new:
        missing = "the baseline in HEAD" if not old else "the working-tree capture"
        return {"measured": False,
                "reason": f"{missing} has no turns — an interactive capture can only be compared "
                          f"against another interactive capture"}
    lens_old = turn_lens(old, perimeter=perimeter)
    lens_new = turn_lens(new, perimeter=perimeter)
    out = {"measured": True, "depths": {"from": lens_old["depths"], "to": lens_new["depths"]}}
    for key in ("reasked", "lost", "regressed"):
        before = set(lens_old["unanimous"][key])
        after = set(lens_new["unanimous"][key])
        out[f"{key}_added"] = sorted(after - before)
        out[f"{key}_removed"] = sorted(before - after)
    return out

# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Baseline freshness: was the committed baseline captured against today's assets? Funded by #405
# (asset commits unnoticed between captures for a month) and #410 (a change to the on-wire user
# message, invisible to `prompt_version()`). `WATCHED_PATHS` covers exactly those two mechanisms
# and every output names the paths it checked.
WATCHED_PATHS: tuple[str, ...] = (
    "src/requivo/assets/prompts",
    "src/requivo/assets/context",
    # Every perimeter's schema (#608): a schema move in either invalidates the baseline that reads it.
    "src/requivo/assets/perimeters",
    "src/requivo/providers/anthropic/generators.py",
)


def _git(args: list[str]) -> tuple[bool, str]:
    """Run one git command inside REPO; never raises — a failure is a state for the caller.

        Bytes decoded as UTF-8, never `text=True`, whose newline translation forged a second commit row
        (#456, `test_a_hostile_commit_subject_cannot_forge_a_second_commit_row`); a decode error is
        `unknown`, not a crash.
    """
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
    """The pure core of `baseline_commits_since`: three states over already-fetched git data.

        `baseline_error` (the git call failed) and a history-less path get different reasons, and a
        shallow clone is `unknown` — its calls succeed but answer a truncated question.
    """
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
    """Is the committed baseline at `rel_path` current with respect to `watched`?

        Returns `state`: ``current``, ``stale`` (with `commits`, oldest first) or ``unknown`` (with
        `reason`). Callers branch on `state`: an empty `commits` means different things under each.
    """
    ok, shallow_out = _git(["rev-parse", "--is-shallow-repository"])
    is_shallow = (shallow_out.strip() == "true") if ok else None

    ok, log_out = _git(["log", "-1", f"--format=%H{_SEP}%cI", "HEAD", "--", rel_path])
    baseline = None
    # The git call failing, kept apart from "no history yet" below.
    baseline_error = None if ok else log_out
    if ok and log_out.strip():
        sha, _, captured_at = log_out.strip().partition(_SEP)
        baseline = (sha, captured_at)

    since_commits = None
    if baseline is not None:
        # --reverse: oldest first, so `commits[:5]` keeps the commit that started the drift.
        ok, since_out = _git(["log", "--reverse", f"--format=%H{_SEP}%cI{_SEP}%s",
                              f"{baseline[0]}..HEAD", "--", *watched])
        if ok:
            since_commits = []
            # split("\n"), never splitlines() (#456): test_a_hostile_commit_subject_cannot_forge_a_second_commit_row.
            for line in since_out.split("\n"):
                if not line:
                    continue
                sha, _, rest = line.partition(_SEP)
                date, _, subject = rest.partition(_SEP)
                since_commits.append({"sha": sha[:9], "date": date[:10], "subject": subject})

    return _freshness_from_git_data(is_shallow, baseline, since_commits, baseline_error)

