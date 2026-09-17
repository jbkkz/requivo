"""Unit tests for the golden harness's own logic (`scripts/golden_lib.py`)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import golden_lib  # noqa: E402
from golden_lib import (  # noqa: E402
    WATCHED_PATHS,
    AnswerSheet,
    Turn,
    _cluster_headlines,
    _freshness_from_git_data,
    answers_for_turn,
    baseline_commits_since,
    brief_consensus,
    brief_movements,
    captured_model,
    captured_perimeter,
    consensus,
    dump_runs,
    is_interactive,
    load_answers,
    load_runs,
    load_turns,
    movements,
    parse_requests,
    stability,
    turn_envelope,
    turn_lens,
    turn_movements,
    unreached_layers,
)

from requivo.core.contracts import (  # noqa: E402
    Brief,
    Challenge,
    Confidence,
    EngineOutput,
    Impact,
    Level,
    Question,
    Slot,
    Summary,
)
from requivo.core.perimeters import DEFAULT_PERIMETER  # noqa: E402

# ── builders ─────────────────────────────────────────────────────────────────────────────────────

def _slot(value="v", impact=Impact.medium, confidence=Confidence.explicit, completeness=80):
    return Slot(value=value, completeness=completeness, confidence=confidence, impact=impact, evidence="e")


def _model(**impacts) -> EngineOutput:
    """An EngineOutput carrying the named slots at the given impacts, no questions."""
    return EngineOutput(model={sid: _slot(impact=imp) for sid, imp in impacts.items()}, questions=[], summary=Summary())


def _challenge(headline, contests=()):
    return Challenge(headline=headline, premise="p", alternative="a", consequence="c", recommendation="r",
                     contests=list(contests))


def _brief(challenges, complexity=Level.high):
    """`challenges` is a list of headlines, or of (headline, contested slot ids) pairs."""
    built = [_challenge(*c) if isinstance(c, tuple) else _challenge(c) for c in challenges]
    return Brief(challenges=built, complexity=complexity)


def _q(slot: str) -> Question:
    return Question(q=f"tell me about {slot}", slot=slot, why="it drives the shape")


_E80 = {"problem": ("explicit", 80)}


def _turn(index: int, answered: list[str], asks: tuple = (), states: dict | None = None) -> Turn:
    """One captured turn: what the sheet answered, and the model that came back."""
    model = {sid: _slot(confidence=conf, completeness=comp) for sid, (conf, comp) in (states or {}).items()}
    return Turn(index=index, answered=list(answered),
                model=EngineOutput(model=model, questions=[_q(s) for s in asks], summary=Summary()))


_LO, _MD, _HI = _model(problem=Impact.low), _model(problem=Impact.medium), _model(problem=Impact.high)


# ── the slot lens: the noise floor, and strong vs weak ───────────────────────────────────────────

def test_consensus_reports_the_modal_value_and_stability_the_jitter():
    con = consensus([_HI, _HI, _LO])
    assert con["slots"]["problem"]["impact"] == ("high", 2) and con["n"] == 3   # modal value, 2 of 3 runs
    st = stability([_model(problem=Impact.high, workflow=Impact.high), _model(problem=Impact.high, workflow=Impact.low),
                    _model(problem=Impact.high, workflow=Impact.medium)])
    assert st["unanimous"]["impact"] == 1 and st["jitter"]["impact"] == 1


@pytest.mark.parametrize("old, new, strong, weak", [
    pytest.param([_LO] * 3, [_HI] * 3, 1, 0, id="unanimous-before-and-after-is-strong"),
    pytest.param([_LO] * 3, [_HI, _HI, _LO], 0, 1, id="a-bare-majority-is-only-weak"),
    pytest.param([_LO, _LO, _HI], [_MD] * 3, 0, 0, id="a-jittery-baseline-is-never-a-reference"),
    pytest.param([_HI] * 3, [_HI] * 3, 0, 0, id="a-held-value-does-not-move"),
])
def test_a_move_is_strong_only_when_unanimous_before_and_after(old, new, strong, weak):
    """The rule the whole lens rests on: at K=3 a majority is one run flipping."""
    m = movements(old, new)
    assert (len(m["strong"]), len(m["weak"]), bool(m["moved"])) == (strong, weak, bool(strong or weak))
    if strong:
        assert (m["strong"][0]["from"], m["strong"][0]["to"]) == ("low", "high")


# ── the assessment lens ──────────────────────────────────────────────────────────────────────────

def test_headlines_cluster_across_phrasing_variants():
    """The word-overlap fallback, used for captures taken before `contests` existed."""
    clusters = _cluster_headlines([["Signature as billing trigger"], ["Billing trigger at signature"],
                                   ["Signature is the billing trigger"]])
    assert list(clusters.values()) == [3]


def test_the_same_challenge_reworded_beyond_recognition_still_groups():
    """Grouped by the slots contested, not the wording: the case that broke the first version of this lens."""
    briefs = [_brief([("Visibility of the superseded signed copy", ["edge_cases", "permissions"])]),
              _brief([("Published-document blast radius ignored", ["edge_cases"])]),
              _brief([("Old version stays live mid-re-approval", ["edge_cases", "workflow"])])]
    con = brief_consensus(briefs)
    assert con["all_themes"]["Edge cases"] == 3 and con["themes"] == {"Edge cases"}


def test_themes_need_every_run_and_unrelated_slots_stay_apart():
    apart = [_brief([("Auto-issued invoice, no review", ["workflow"]), ("One contract, one invoice", ["business_objects"])])] * 3
    assert len(brief_consensus(apart)["themes"]) == 2
    offline = _brief([("Offline capability assumed", ["constraints"])])
    assert brief_consensus([offline, offline, _brief([("Retention clock on delete", ["business_rules"])])])["themes"] == set()
    assert brief_consensus([offline] * 3)["themes"] == {"Constraints"}


def test_a_headline_used_as_a_theme_label_cannot_forge_a_line():
    """#137: a headline is a label the print sites cannot cover; an ordinary one is its own label byte for byte."""
    forged = [_brief(["benign headline\n  assessment + challenge(s) now raised: FORGED"])] * 3
    ((label, _), ) = brief_consensus(forged)["all_themes"].items()
    assert "\n" not in label and "FORGED" in label and "\\n" in label
    assert set(brief_consensus([_brief(["Signature as billing trigger"])] * 3)["all_themes"]) == {"Signature as billing trigger"}


def test_brief_movements_grades_the_verdict_like_a_slot_and_reports_theme_changes():
    old = [_brief(["Signature as billing trigger", "Offline capability assumed"], Level.high)] * 3
    b = brief_movements(old, [_brief(["Offline capability assumed", "Rounding convention"], Level.medium)] * 3)
    assert b["themes_removed"] == ["Signature as billing trigger"] and b["themes_added"] == ["Rounding convention"]
    assert b["complexity"]["strong"] is True
    split = [_brief([], Level.medium), _brief([], Level.medium), _brief([], Level.high)]
    assert brief_movements(old, split)["complexity"]["strong"] is False
    held = brief_movements(old, old)
    assert held["complexity"] is None and not held["themes_added"] and not held["themes_removed"]


# ── the multi-turn lens (#77 moved the interactive loop onto `DiscoveryService.draft_turn`) ───────

def test_parse_requests_collects_a_layered_answer_sheet(tmp_path):
    """Each repeated `answer.<slot>:` line is the next layer a client volunteers; no sheet is single-pass."""
    p = tmp_path / "requests.md"
    p.write_text("\n".join(["### s", "form: f", "card: c", "request: r", "answer.problem: first layer",
                            "answer.actors: who", "answer.problem: second layer"]), encoding="utf-8")
    req = parse_requests(p)[0]
    assert req["answers"] == {"problem": ["first layer", "second layer"], "actors": ["who"]}
    assert is_interactive(req) is True
    p.write_text("### s\nform: f\ncard: c\nrequest: r\n", encoding="utf-8")
    req = parse_requests(p)[0]
    assert req["answers"] == {} and is_interactive(req) is False


def test_perimeter_defaults_to_software_and_reads_an_explicit_value(tmp_path):
    """#621: a block with no `perimeter:` line reads `DEFAULT_PERIMETER`."""
    p = tmp_path / "requests.md"
    p.write_text("### s\nform: f\ncard: c\nrequest: r\n", encoding="utf-8")
    assert parse_requests(p)[0]["perimeter"] == DEFAULT_PERIMETER
    p.write_text("### s\nperimeter: go-to-market\nform: f\ncard: c\nrequest: r\n", encoding="utf-8")
    assert parse_requests(p)[0]["perimeter"] == "go-to-market"


def test_the_answer_sheet_hands_each_layer_out_once_and_reports_what_is_left():
    """FIFO, so a client never repeats themselves; exhausted slots drop out of `remaining()` (#163)."""
    sheet = AnswerSheet({"problem": ["first", "second"], "actors": ["who"]})
    assert sheet.remaining() == {"problem": 2, "actors": 1}
    assert sheet.reply_for("problem") == "first" and sheet.reply_for("problem") == "second"
    assert sheet.reply_for("problem") is None and sheet.reply_for("risks") is None
    assert sheet.remaining() == {"actors": 1}
    sheet.reply_for("actors")
    assert sheet.remaining() == {}


def test_a_turn_answers_only_what_the_sheet_can_speak_to_and_ends_when_it_cannot():
    """The skip is the fixture's version of a user pressing Enter; no answer at all ends the capture."""
    block, answered = answers_for_turn([_q("problem"), _q("risks")], AnswerSheet({"problem": ["the real problem"]}))
    assert answered == ["problem"] and "[slot: problem]" in block and "risks" not in block
    assert answers_for_turn([_q("risks")], AnswerSheet({})) == (None, [])


def test_the_envelope_readers_read_a_turn_capture_and_say_so_on_a_single_pass_one():
    """`load_runs` reads the last turn of each run; a single-pass capture has no turns and no sheet."""
    runs = [[_turn(1, ["problem"], states={"problem": ("inferred", 40)}), _turn(2, [], states={"problem": ("explicit", 90)})]]
    text = turn_envelope("r", {"problem": ["p1", "p2"]}, runs, model="m")
    loaded = load_runs(text)
    assert len(loaded) == 1 and loaded[0].model["problem"].completeness == 90
    assert load_answers(text) == {"problem": ["p1", "p2"]}
    single = json.dumps({"request": "r", "runs": [_HI.model_dump()]})
    assert load_turns(single) is None and load_answers(single) == {}


@pytest.mark.parametrize("run, key, expected", [
    pytest.param([_turn(1, ["problem"], ("problem",), _E80), _turn(2, ["actors"], ("actors",), _E80), _turn(3, [], ("problem",), _E80)],
                 "reasked", {"Real problem": 1}, id="a-question-re-asked-after-it-was-answered"),
    pytest.param([_turn(1, ["problem"], ("problem",), _E80), _turn(2, ["actors"], ("actors",), _E80), _turn(3, ["risks"], ("risks",), _E80)],
                 "reasked", {}, id="an-engine-that-moves-on"),
    pytest.param([_turn(1, ["problem"], ("problem",), _E80), _turn(2, ["problem"], ("problem",), _E80)],
                 "reasked", {}, id="a-re-ask-before-turn-three-is-the-engines-own"),
    pytest.param([_turn(1, ["problem"], ("problem",), _E80), _turn(2, [], states=_E80), _turn(3, [], states={"problem": ("empty", 10)})],
                 "lost", {"Real problem": 1}, id="a-slot-confirmed-early-and-later-forgotten"),
    pytest.param([_turn(1, ["problem"], ("problem",), _E80), _turn(2, [], states=_E80), _turn(3, [], states={"problem": ("explicit", 95)})],
                 "lost", {}, id="a-slot-that-stayed-confirmed"),
    pytest.param([_turn(1, ["problem"], ("problem",), _E80), _turn(2, [], states={"problem": ("explicit", 90)}), _turn(3, [], states={"problem": ("explicit", 55)})],
                 "regressed", {"Real problem": 1}, id="completeness-falling-back-across-a-deep-turn"),
])
def test_the_turn_lens_counts_a_finding_only_from_turn_three(run, key, expected):
    """Turns 1 and 2 send what the old loop sent; a finding there is not evidence about the grounding change."""
    lens = turn_lens([run])
    assert lens["measured"] is True and lens[key] == expected


def test_a_finding_in_every_run_is_the_strong_tier():
    """The same rule the slot lens applies: unanimous is what you act on, one run is noise."""
    reasks = [_turn(1, ["problem"], ("problem",), _E80), _turn(2, ["actors"], ("actors",), _E80), _turn(3, [], ("problem",), _E80)]
    clean = [_turn(1, ["problem"], ("problem",), _E80), _turn(2, ["actors"], ("actors",), _E80), _turn(3, ["risks"], ("risks",), _E80)]
    assert turn_lens([reasks, reasks])["unanimous"]["reasked"] == ["Real problem"]
    assert turn_lens([reasks, clean])["unanimous"]["reasked"] == []
    assert turn_lens([reasks, clean])["reasked"] == {"Real problem": 1}


def test_the_lens_reports_how_deep_each_run_actually_got():
    lens = turn_lens([[_turn(1, ["problem"], ("problem",)), _turn(2, [])]])
    assert lens["depths"] == [2] and lens["deep_enough"] is False


def test_the_lens_says_it_could_not_look_rather_than_reporting_nothing():
    lens = turn_lens(None)
    assert lens["measured"] is False and lens["reason"]
    assert "reasked" not in lens        # no empty finding set to misread as clean


def test_turn_movements_compares_re_asks_and_says_when_it_could_not():
    before = [_turn(1, ["problem"], ("problem",), _E80), _turn(2, ["actors"], ("actors",), _E80), _turn(3, [], ("problem",), _E80)]
    a80 = {"actors": ("explicit", 80)}
    after = [_turn(1, ["actors"], ("actors",), a80), _turn(2, ["problem"], ("problem",), a80), _turn(3, [], ("actors",), a80)]
    m = turn_movements([before], [after])
    assert m["measured"] is True and m["reasked_added"] == ["Actors & roles"] and m["reasked_removed"] == ["Real problem"]
    single_pass = turn_movements(None, [[_turn(1, ["problem"], ("problem",)), _turn(2, [])]])
    assert single_pass["measured"] is False and single_pass["reason"]


def test_unreached_layers_reports_what_no_run_in_the_capture_ever_got_to():
    """#163: a layer is unreached only when *every* run left it on the sheet."""
    deeper = [_turn(1, ["problem"], ("problem",)), _turn(2, ["problem"], ("problem",))]
    shallower = [_turn(1, ["problem"], ("problem",))]
    assert unreached_layers({"problem": ["first", "second", "third"]}, [deeper, shallower]) == {"Real problem": 1}
    assert unreached_layers({"problem": ["first", "second"]}, [deeper, shallower]) == {}


def test_turn_lens_carries_unreached_layers_only_when_given_a_sheet():
    run = [_turn(1, ["problem"], ("problem",)), _turn(2, [])]
    assert "unreached_layers" not in turn_lens([run])
    assert turn_lens([run], layers={"problem": ["first", "second"]})["unreached_layers"] == {"Real problem": 1}


# ── #515 / #621: the model and the perimeter a capture ran under ─────────────────────────────────

def test_both_envelope_writers_record_the_model_and_an_older_baseline_reads_as_unknown():
    """#515: the third state is `None`, never a default; a present-but-useless key is still not an answer."""
    interactive = turn_envelope("r", {"problem": ["p"]}, [[_turn(1, ["problem"])]], model="claude-sonnet-5")
    assert captured_model(interactive) == "claude-sonnet-5"
    assert captured_model(json.dumps({"request": "r", "model": "claude-opus-4-8", "runs": []})) == "claude-opus-4-8"
    for body in ({"request": "r", "runs": []}, {"request": "r", "model": "", "runs": []}, {"request": "r", "model": None, "runs": []}):
        assert captured_model(json.dumps(body)) is None


def test_dump_runs_requires_the_model_it_ran_on(tmp_path, monkeypatch):
    """#515: `model` is keyword-only and has no default, on purpose."""
    monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)
    with pytest.raises(TypeError):
        dump_runs("r", "a request", [_HI])  # type: ignore[call-arg]
    path = dump_runs("r", "a request", [_HI], model="claude-sonnet-5")
    assert captured_model(path.read_text(encoding="utf-8")) == "claude-sonnet-5"


def test_captured_perimeter_round_trips_and_defaults_to_software(tmp_path, monkeypatch):
    """#621: both writers record it, and a key-less or empty value reads as software (the only perimeter before #608)."""
    monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)
    assert captured_perimeter(turn_envelope("r", {}, [[_turn(1, [])]], model="m", perimeter="go-to-market")) == "go-to-market"
    default = dump_runs("r", "a request", [_HI], model="m")
    assert captured_perimeter(default.read_text(encoding="utf-8")) == DEFAULT_PERIMETER
    gtm = dump_runs("r2", "a request", [_HI], model="m", perimeter="go-to-market")
    assert captured_perimeter(gtm.read_text(encoding="utf-8")) == "go-to-market"
    assert captured_perimeter(json.dumps({"request": "r", "runs": []})) == DEFAULT_PERIMETER
    assert captured_perimeter(json.dumps({"request": "r", "perimeter": "", "runs": []})) == DEFAULT_PERIMETER


# ── #405/#410: baseline freshness. `_freshness_from_git_data` is the pure core behind three git calls ──

_BASELINE = ("sha1", "2026-08-01T00:00:00+00:00")


@pytest.mark.parametrize("since, state", [([], "current"),
                                          ([{"sha": "abc123def", "date": "2026-09-01", "subject": "edit a prompt"}], "stale")],
                         ids=["must-not-fire-nothing-watched-changed", "must-fire-#405-a-watched-commit-after-the-baseline"])
def test_a_commit_touching_a_watched_path_since_the_baseline_marks_it_stale(since, state):
    report = _freshness_from_git_data(is_shallow=False, baseline=_BASELINE, since_commits=since)
    assert report == {"state": state, "captured_at": _BASELINE[1], "commits": since}


@pytest.mark.parametrize("kwargs, reason", [
    pytest.param(dict(is_shallow=True, baseline=_BASELINE, since_commits=[]), "shallow clone", id="a-shallow-clone"),
    pytest.param(dict(is_shallow=None, baseline=_BASELINE, since_commits=[]), "could not tell whether this is a shallow clone",
                 id="an-unknown-shallow-check"),
    pytest.param(dict(is_shallow=False, baseline=None, since_commits=[]), "no commit history", id="a-baseline-with-no-history"),
    pytest.param(dict(is_shallow=False, baseline=None, since_commits=None, baseline_error="fatal: bad object HEAD"),
                 "bad object HEAD", id="a-failed-baseline-log-keeps-its-own-reason"),
    pytest.param(dict(is_shallow=False, baseline=_BASELINE, since_commits=None), "git log", id="a-failed-since-log-is-not-zero-commits"),
])
def test_an_uncertain_freshness_input_is_reported_unknown_with_its_reason(kwargs, reason):
    """must fire (#405): every way the git data can be untrustworthy reads as *unknown*, never as current."""
    report = _freshness_from_git_data(**kwargs)
    assert report["state"] == "unknown" and reason in report["reason"], report
    if kwargs.get("baseline_error"):
        assert "no commit history" not in report["reason"], report


def test_watched_paths_cover_both_funding_instances():
    """`WATCHED_PATHS` is what #405 and #410 fund; narrowing it silently is the trap the brief names."""
    for path in ("src/requivo/assets/prompts", "src/requivo/assets/context", "src/requivo/assets/perimeters",
                 "src/requivo/providers/anthropic/generators.py"):
        assert path in WATCHED_PATHS


def _repo(root: Path, watched_subjects: list[str]) -> None:
    """A synthetic repo: one baseline commit, then one watched-path commit per subject (#450)."""
    def git(*args, cwd=root):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (root / "watched").mkdir()
    (root / "watched" / "f.txt").write_text("0", encoding="utf-8")
    (root / "fixtures").mkdir()
    (root / "fixtures" / "b.txt").write_text("baseline", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "baseline commit")
    for i, subject in enumerate(watched_subjects, 1):
        (root / "watched" / "f.txt").write_text(str(i), encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", subject)


def test_baseline_commits_since_orders_the_watched_commits_since_the_baseline_oldest_first(tmp_path, monkeypatch):
    """Integration against a synthetic repo, not the real one (#450); git log's own order is newest-first."""
    _repo(tmp_path, [f"watched commit {i}" for i in range(1, 4)])
    monkeypatch.setattr(golden_lib, "REPO", tmp_path)
    report = golden_lib.baseline_commits_since("fixtures/b.txt", watched=("watched",))
    assert report["state"] == "stale", report
    assert [c["subject"] for c in report["commits"]] == ["watched commit 1", "watched commit 2", "watched commit 3"], report


def test_baseline_commits_since_reports_unknown_on_a_real_shallow_clone(tmp_path, monkeypatch):
    """The `unknown`-on-shallow behaviour end-to-end, against a REAL shallow clone (#450)."""
    source = tmp_path / "source"
    source.mkdir()
    _repo(source, ["a second commit, so the clone below has real history to truncate"])
    shallow = tmp_path / "shallow"
    # A `file://` URI rather than a bare path, so `--depth` is honoured and the clone is really shallow.
    subprocess.run(["git", "clone", "-q", "--no-local", "--depth", "1", source.resolve().as_uri(), str(shallow)],
                   check=True, capture_output=True)
    monkeypatch.setattr(golden_lib, "REPO", shallow)
    report = golden_lib.baseline_commits_since("fixtures/b.txt")
    assert report["state"] == "unknown" and "shallow" in report["reason"], report


def test_baseline_commits_since_reports_unknown_for_a_path_with_no_history():
    """must fire, the negative control for the integration tests above."""
    assert baseline_commits_since("fixtures/golden/this-slug-does-not-exist.runs.json")["state"] == "unknown"


# `_HOSTILE_SUBJECTS` is #456's own reproduction; `\r` is the PoC and the one universal-newlines hides.
_HOSTILE_SUBJECTS = ["docs: tidy\rFORGED", "docs: tidy\x0bFORGED", "docs: tidy\x0cFORGED", "docs: tidy\x1cFORGED",
                     "docs: tidy\x1dFORGED", "docs: tidy\x1eFORGED", "docs: tidy\x85FORGED",
                     "docs: tidy FORGED", "docs: tidy FORGED"]


def test_a_hostile_commit_subject_cannot_forge_a_second_commit_row(tmp_path, monkeypatch):
    """must fire -- #456: each hostile subject used to become *two* rows of `commits`."""
    subjects = [*_HOSTILE_SUBJECTS, "an entirely ordinary subject"]
    _repo(tmp_path, subjects)
    monkeypatch.setattr(golden_lib, "REPO", tmp_path)
    report = golden_lib.baseline_commits_since("fixtures/b.txt", watched=("watched",))
    assert report["state"] == "stale", report
    assert [c["subject"] for c in report["commits"]] == subjects, report["commits"]
    assert all(c["sha"] != "FORGED" and len(c["sha"]) == 9 for c in report["commits"]), report["commits"]
