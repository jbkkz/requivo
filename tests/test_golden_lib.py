"""Unit tests for the golden harness's own logic."""
from __future__ import annotations

import json
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
    return Slot(value=value, completeness=completeness, confidence=confidence,
                impact=impact, evidence="e")


def _model(**impacts) -> EngineOutput:
    """An EngineOutput carrying the named slots at the given impacts, no questions."""
    return EngineOutput(model={sid: _slot(impact=imp) for sid, imp in impacts.items()},
                        questions=[], summary=Summary())


def _challenge(headline, contests=()):
    return Challenge(headline=headline, premise="p", alternative="a",
                     consequence="c", recommendation="r", contests=list(contests))


def _brief(challenges, complexity=Level.high):
    """`challenges` is a list of headlines, or of (headline, contested slot ids) pairs."""
    built = [_challenge(*c) if isinstance(c, tuple) else _challenge(c) for c in challenges]
    return Brief(challenges=built, complexity=complexity)


# ── the noise floor ──────────────────────────────────────────────────────────────────────────────

def test_consensus_reports_modal_value_and_agreement():
    runs = [_model(problem=Impact.high), _model(problem=Impact.high), _model(problem=Impact.low)]
    con = consensus(runs)
    assert con["slots"]["problem"]["impact"] == ("high", 2)   # modal value, 2 of 3 runs
    assert con["n"] == 3


def test_stability_separates_unanimous_slots_from_jittery_ones():
    runs = [_model(problem=Impact.high, workflow=Impact.high),
            _model(problem=Impact.high, workflow=Impact.low),
            _model(problem=Impact.high, workflow=Impact.medium)]
    st = stability(runs)
    assert st["unanimous"]["impact"] == 1
    assert st["jitter"]["impact"] == 1


# ── strong vs weak, the rule the whole lens rests on ─────────────────────────────────────────────

def test_unanimous_before_and_after_is_a_strong_move():
    old = [_model(problem=Impact.low)] * 3
    new = [_model(problem=Impact.high)] * 3
    m = movements(old, new)
    assert len(m["strong"]) == 1 and not m["weak"]
    assert (m["strong"][0]["from"], m["strong"][0]["to"]) == ("low", "high")


def test_bare_majority_is_only_a_weak_move():
    """At K=3 a majority is 2 of 3 — one run flipping."""
    old = [_model(problem=Impact.low)] * 3
    new = [_model(problem=Impact.high), _model(problem=Impact.high), _model(problem=Impact.low)]
    m = movements(old, new)
    assert len(m["weak"]) == 1 and not m["strong"]


def test_a_jittery_old_baseline_is_never_a_reference():
    """If the old runs disagreed, there is nothing reliable to have moved away from."""
    old = [_model(problem=Impact.low), _model(problem=Impact.low), _model(problem=Impact.high)]
    new = [_model(problem=Impact.medium)] * 3
    assert not movements(old, new)["moved"]


def test_no_movement_when_the_value_holds():
    runs = [_model(problem=Impact.high)] * 3
    assert not movements(runs, runs)["moved"]


# ── the assessment lens ──────────────────────────────────────────────────────────────────────────

def test_headlines_cluster_across_phrasing_variants():
    """The word-overlap fallback, used for captures taken before `contests` existed."""
    clusters = _cluster_headlines([
        ["Signature as billing trigger"],
        ["Billing trigger at signature"],
        ["Signature is the billing trigger"],
    ])
    assert list(clusters.values()) == [3]      # one theme, seen in all three runs


def test_the_same_challenge_reworded_beyond_recognition_still_groups():
    """The case that broke the first version of this lens, taken verbatim from a doc-reapproval capture."""
    briefs = [
        _brief([("Visibility of the superseded signed copy", ["edge_cases", "permissions"])]),
        _brief([("Published-document blast radius ignored", ["edge_cases"])]),
        _brief([("Old version stays live mid-re-approval", ["edge_cases", "workflow"])]),
    ]
    con = brief_consensus(briefs)
    assert con["all_themes"]["Edge cases"] == 3      # the slot all three runs really contested
    assert con["themes"] == {"Edge cases"}           # the secondary slots stay below the majority


def test_challenges_contesting_unrelated_slots_stay_apart():
    """The grouping must not collapse everything into one theme either."""
    briefs = [_brief([("Auto-issued invoice, no review", ["workflow"]),
                      ("One contract, one invoice", ["business_objects"])])] * 3
    assert len(brief_consensus(briefs)["themes"]) == 2


def test_a_challenge_only_some_runs_raise_is_not_stable():
    """Challenge themes need every run, not a majority."""
    briefs = [_brief([("Offline capability assumed", ["constraints"])]),
              _brief([("Offline capability assumed", ["constraints"])]),
              _brief([("Retention clock on delete", ["business_rules"])])]
    assert brief_consensus(briefs)["themes"] == set()

    everywhere = [_brief([("Offline capability assumed", ["constraints"])])] * 3
    assert brief_consensus(everywhere)["themes"] == {"Constraints"}


def test_a_headline_used_as_a_theme_label_cannot_forge_a_line():
    """The other half of #137's sweep, and the one the print sites could not cover."""
    forged = [_brief(["benign headline\n  assessment + challenge(s) now raised: FORGED"])] * 3
    ((label, _), ) = brief_consensus(forged)["all_themes"].items()
    assert "\n" not in label
    assert "FORGED" in label and "\\n" in label

    # must not fire: an ordinary headline is its own label, byte for byte.
    plain = [_brief(["Signature as billing trigger"])] * 3
    assert set(brief_consensus(plain)["all_themes"]) == {"Signature as billing trigger"}


def test_a_challenge_the_engine_stopped_raising_is_reported():
    old = [_brief(["Signature as billing trigger", "Offline capability assumed"])] * 3
    new = [_brief(["Offline capability assumed", "Rounding convention"])] * 3
    b = brief_movements(old, new)
    assert b["themes_removed"] == ["Signature as billing trigger"]
    assert b["themes_added"] == ["Rounding convention"]


def test_complexity_verdict_is_graded_like_a_slot():
    old = [_brief([], Level.high)] * 3
    unanimous = [_brief([], Level.medium)] * 3
    assert brief_movements(old, unanimous)["complexity"]["strong"] is True

    split = [_brief([], Level.medium), _brief([], Level.medium), _brief([], Level.high)]
    assert brief_movements(old, split)["complexity"]["strong"] is False


def test_a_held_verdict_and_challenge_set_reports_nothing():
    runs = [_brief(["Offline capability assumed"], Level.medium)] * 3
    b = brief_movements(runs, runs)
    assert b["complexity"] is None and not b["themes_added"] and not b["themes_removed"]


# ── the multi-turn lens ──────────────────────────────────────────────────────────────────────────
#
# #77 moved the interactive `discover` loop onto `DiscoveryService.draft_turn`.

def _q(slot: str) -> Question:
    return Question(q=f"tell me about {slot}", slot=slot, why="it drives the shape")


def _turn(index: int, answered: list[str], *, asks: tuple = (),
          states: dict | None = None) -> Turn:
    """One captured turn: what the sheet answered, and the model that came back."""
    model = {sid: _slot(confidence=conf, completeness=comp)
             for sid, (conf, comp) in (states or {}).items()}
    return Turn(index=index, answered=list(answered),
                model=EngineOutput(model=model, questions=[_q(s) for s in asks],
                                   summary=Summary()))


def test_parse_requests_collects_a_layered_answer_sheet(tmp_path):
    """A slot may be answered more than once — each line is the next layer a client volunteers when the engine
    comes back to that slot."""
    p = tmp_path / "requests.md"
    p.write_text("\n".join(["### s", "form: f", "card: c", "request: r",
                            "answer.problem: first layer", "answer.actors: who",
                            "answer.problem: second layer"]), encoding="utf-8")
    req = parse_requests(p)[0]
    assert req["answers"] == {"problem": ["first layer", "second layer"], "actors": ["who"]}
    assert is_interactive(req) is True


def test_a_request_without_an_answer_sheet_is_single_pass(tmp_path):
    p = tmp_path / "requests.md"
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


def test_the_answer_sheet_hands_each_layer_out_once():
    """Consumed FIFO, so a client never repeats themselves and the loop keeps finding new ground."""
    sheet = AnswerSheet({"problem": ["first", "second"]})
    assert sheet.reply_for("problem") == "first"
    assert sheet.reply_for("problem") == "second"
    assert sheet.reply_for("problem") is None      # exhausted
    assert sheet.reply_for("actors") is None       # never had anything to say


def test_a_turn_answers_only_the_questions_the_sheet_can_speak_to():
    """The skip is the fixture's version of a user pressing Enter, and it has to be visible."""
    sheet = AnswerSheet({"problem": ["the real problem"]})
    block, answered = answers_for_turn([_q("problem"), _q("risks")], sheet)
    assert answered == ["problem"]
    assert "[slot: problem]" in block and "risks" not in block


def test_a_turn_the_sheet_cannot_speak_to_at_all_ends_the_capture():
    """`converse()` stops when no question got an answer, and so must the harness."""
    block, answered = answers_for_turn([_q("risks")], AnswerSheet({}))
    assert block is None and answered == []


def test_load_turns_says_it_could_not_look_at_a_single_pass_capture():
    """Third state. A single-pass capture has nothing to say about turn 3."""
    text = json.dumps({"request": "r", "runs": [_model(problem=Impact.high).model_dump()]})
    assert load_turns(text) is None


def test_load_runs_reads_the_last_turn_of_each_run_when_there_is_no_runs_key():
    """The multi-turn envelope does not duplicate the final models under `runs`."""
    runs = [[_turn(1, ["problem"], states={"problem": ("inferred", 40)}),
             _turn(2, [], states={"problem": ("explicit", 90)})]]
    loaded = load_runs(turn_envelope("r", {"problem": ["p"]}, runs, model="m"))
    assert len(loaded) == 1
    assert loaded[0].model["problem"].completeness == 90


def test_a_question_re_asked_after_the_client_answered_it_is_counted():
    """The failure mode the whole issue is about: the transcript is gone from turn 3."""
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
           _turn(3, [], asks=("problem",), states={"problem": ("explicit", 80)})]
    lens = turn_lens([run])
    assert lens["measured"] is True
    assert lens["reasked"] == {"Real problem": 1}


def test_an_engine_that_moves_on_reports_no_re_ask():
    """The positive control's twin. Without it, a lens that never fires and a lens that is broken produce the
    same empty dict."""
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
           _turn(3, ["risks"], asks=("risks",), states={"problem": ("explicit", 80)})]
    assert turn_lens([run])["reasked"] == {}


def test_a_re_ask_before_turn_three_is_not_counted():
    """Turns 1 and 2 send exactly what the old loop sent, so a repeat there is the engine's own behaviour and
    not evidence about the grounding change."""
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)})]
    assert turn_lens([run])["reasked"] == {}


def test_a_slot_the_client_confirmed_early_and_the_model_later_forgot_is_reported():
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, [], states={"problem": ("explicit", 80)}),
           _turn(3, [], states={"problem": ("empty", 10)})]
    assert turn_lens([run])["lost"] == {"Real problem": 1}


def test_a_slot_that_stayed_confirmed_is_not_reported_as_lost():
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, [], states={"problem": ("explicit", 80)}),
           _turn(3, [], states={"problem": ("explicit", 95)})]
    assert turn_lens([run])["lost"] == {}


def test_completeness_falling_back_across_a_deep_turn_is_reported():
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, [], states={"problem": ("explicit", 90)}),
           _turn(3, [], states={"problem": ("explicit", 55)})]
    assert turn_lens([run])["regressed"] == {"Real problem": 1}


def test_a_finding_in_every_run_is_the_strong_tier():
    """The same rule the slot lens already applies: unanimous is what you act on, one run is noise."""
    reasks = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
              _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
              _turn(3, [], asks=("problem",), states={"problem": ("explicit", 80)})]
    clean = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
             _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
             _turn(3, ["risks"], asks=("risks",), states={"problem": ("explicit", 80)})]
    assert turn_lens([reasks, reasks])["unanimous"]["reasked"] == ["Real problem"]
    assert turn_lens([reasks, clean])["unanimous"]["reasked"] == []
    assert turn_lens([reasks, clean])["reasked"] == {"Real problem": 1}


def test_the_lens_reports_how_deep_each_run_actually_got():
    """A capture that stopped at turn 2 measures nothing about this issue."""
    lens = turn_lens([[_turn(1, ["problem"], asks=("problem",)), _turn(2, [])]])
    assert lens["depths"] == [2]
    assert lens["deep_enough"] is False


def test_the_lens_says_it_could_not_look_rather_than_reporting_nothing():
    lens = turn_lens(None)
    assert lens["measured"] is False and lens["reason"]
    assert "reasked" not in lens        # no empty finding set to misread as clean


def test_turn_movements_reports_that_it_could_not_compare_a_single_pass_baseline():
    run = [_turn(1, ["problem"], asks=("problem",)), _turn(2, [])]
    m = turn_movements(None, [run])
    assert m["measured"] is False and m["reason"]


def test_turn_movements_reports_a_re_ask_the_engine_gained_or_dropped():
    before = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
              _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
              _turn(3, [], asks=("problem",), states={"problem": ("explicit", 80)})]
    after = [_turn(1, ["actors"], asks=("actors",), states={"actors": ("explicit", 80)}),
             _turn(2, ["problem"], asks=("problem",), states={"actors": ("explicit", 80)}),
             _turn(3, [], asks=("actors",), states={"actors": ("explicit", 80)})]
    m = turn_movements([before], [after])
    assert m["measured"] is True
    assert m["reasked_added"] == ["Actors & roles"]
    assert m["reasked_removed"] == ["Real problem"]


# -- #163: the sheet a SHALLOW capture never got to --------------------------------------------
#
# `AnswerSheet.remaining()` was removed as dead in #137 and the diagnosis it would have powered had to be run by hand: which of the sheet's authored layers a capture's runs never reached.

def test_the_answer_sheet_reports_what_it_still_has_to_say():
    sheet = AnswerSheet({"problem": ["first", "second"], "actors": ["who"]})
    assert sheet.remaining() == {"problem": 2, "actors": 1}
    sheet.reply_for("problem")
    assert sheet.remaining() == {"problem": 1, "actors": 1}
    sheet.reply_for("problem")
    sheet.reply_for("actors")
    assert sheet.remaining() == {}   # exhausted slots drop out, rather than reporting a bare 0


def test_unreached_layers_reports_what_no_run_in_the_capture_ever_got_to():
    """The #163 diagnosis. A layer counts as unreached only when *every* run left it on the sheet."""
    layers = {"problem": ["first", "second", "third"]}
    deeper = [_turn(1, ["problem"], asks=("problem",)), _turn(2, ["problem"], asks=("problem",))]
    shallower = [_turn(1, ["problem"], asks=("problem",))]
    assert unreached_layers(layers, [deeper, shallower]) == {"Real problem": 1}


def test_a_layer_reached_by_even_one_run_is_not_reported_as_unreached():
    """must not fire: the positive control's twin."""
    layers = {"problem": ["first", "second"]}
    deeper = [_turn(1, ["problem"], asks=("problem",)), _turn(2, ["problem"], asks=("problem",))]
    shallower = [_turn(1, ["problem"], asks=("problem",))]
    assert unreached_layers(layers, [deeper, shallower]) == {}


def test_turn_lens_carries_unreached_layers_only_when_given_a_sheet():
    run = [_turn(1, ["problem"], asks=("problem",)), _turn(2, [])]
    assert "unreached_layers" not in turn_lens([run])
    lens = turn_lens([run], layers={"problem": ["first", "second"]})
    assert lens["unreached_layers"] == {"Real problem": 1}


def test_load_answers_reads_the_persisted_sheet():
    text = turn_envelope("r", {"problem": ["p1", "p2"]}, [[_turn(1, ["problem"])]],
                         model="m")
    assert load_answers(text) == {"problem": ["p1", "p2"]}


def test_both_envelope_writers_record_the_model_the_capture_ran_on():
    """#515: an envelope recorded a capture's *input* and nothing about the conditions it ran under."""
    interactive = turn_envelope("r", {"problem": ["p"]}, [[_turn(1, ["problem"])]],
                                model="claude-sonnet-5")
    assert captured_model(interactive) == "claude-sonnet-5"
    single_pass = json.dumps({"request": "r", "model": "claude-opus-4-8", "runs": []})
    assert captured_model(single_pass) == "claude-opus-4-8"


def test_dump_runs_requires_the_model_it_ran_on(tmp_path, monkeypatch):
    """#515: `model` is keyword-only and has no default, on purpose."""
    monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)
    with pytest.raises(TypeError):
        dump_runs("r", "a request", [_model(problem=Impact.high)])  # type: ignore[call-arg]

    path = dump_runs("r", "a request", [_model(problem=Impact.high)], model="claude-sonnet-5")
    assert captured_model(path.read_text(encoding="utf-8")) == "claude-sonnet-5"


def test_a_baseline_written_before_the_model_was_recorded_reads_as_unknown():
    """The third state is `None`, never a default and never the configured model."""
    assert captured_model(json.dumps({"request": "r", "runs": []})) is None
    # Must fire on the near-misses too: a key that is present and useless is still not an answer.
    assert captured_model(json.dumps({"request": "r", "model": "", "runs": []})) is None
    assert captured_model(json.dumps({"request": "r", "model": None, "runs": []})) is None


def test_load_answers_is_empty_for_a_single_pass_capture():
    text = json.dumps({"request": "r", "runs": [_model(problem=Impact.high).model_dump()]})
    assert load_answers(text) == {}


# -- #621: which perimeter a capture ran under -----------------------------------------------------

def test_captured_perimeter_round_trips_and_defaults_to_software(tmp_path, monkeypatch):
    """Mirrors #515's `model` pairing (unlike `model`, not required -- default software, since there was only
    one perimeter before #608), for both writers and a key-less baseline alike."""
    monkeypatch.setattr(golden_lib, "GOLDEN", tmp_path)
    interactive = turn_envelope("r", {}, [[_turn(1, [])]], model="m", perimeter="go-to-market")
    assert captured_perimeter(interactive) == "go-to-market"
    default = dump_runs("r", "a request", [_model(problem=Impact.high)], model="m")
    assert captured_perimeter(default.read_text(encoding="utf-8")) == DEFAULT_PERIMETER
    gtm = dump_runs("r2", "a request", [_model(problem=Impact.high)], model="m",
                    perimeter="go-to-market")
    assert captured_perimeter(gtm.read_text(encoding="utf-8")) == "go-to-market"
    assert captured_perimeter(json.dumps({"request": "r", "runs": []})) == DEFAULT_PERIMETER
    assert captured_perimeter(json.dumps({"request": "r", "perimeter": "", "runs": []})) == \
        DEFAULT_PERIMETER


# -- #405/#410: baseline freshness -- a committed baseline predating a real commit that changes what
# a capture measures must be visible, without a control run, before any lens output is read ---------
#
# `_freshness_from_git_data` is the pure core `baseline_commits_since` wraps around three git calls (is-shallow, last-commit-touching-the-baseline, commits-since-touching-`watched`).

def test_a_baseline_with_no_commits_since_touching_watched_paths_is_current():
    """must not fire -- the positive control for the test below."""
    report = _freshness_from_git_data(is_shallow=False, baseline=("sha1", "2026-08-01T00:00:00+00:00"),
                                       since_commits=[])
    assert report == {"state": "current", "captured_at": "2026-08-01T00:00:00+00:00", "commits": []}


def test_a_commit_touching_a_watched_path_since_the_baseline_marks_it_stale():
    """must fire -- the #405 shape itself: a watched-path commit landed after the baseline's own commit and
    the baseline never re-captured against it."""
    commits = [{"sha": "abc123def", "date": "2026-09-01", "subject": "edit a prompt"}]
    report = _freshness_from_git_data(is_shallow=False, baseline=("sha1", "2026-08-01T00:00:00+00:00"),
                                       since_commits=commits)
    assert report["state"] == "stale"
    assert report["captured_at"] == "2026-08-01T00:00:00+00:00"
    assert report["commits"] == commits


def test_a_shallow_clone_is_reported_unknown_not_current():
    """must fire."""
    report = _freshness_from_git_data(is_shallow=True, baseline=("sha1", "2026-08-01T00:00:00+00:00"),
                                       since_commits=[])
    assert report == {"state": "unknown",
                       "reason": "shallow clone -- commit history is truncated, so a count of "
                                 "commits since the baseline cannot be trusted"}


def test_an_unknown_shallow_check_itself_is_reported_unknown():
    """must fire."""
    report = _freshness_from_git_data(is_shallow=None, baseline=("sha1", "2026-08-01T00:00:00+00:00"),
                                       since_commits=[])
    assert report == {"state": "unknown",
                       "reason": "could not tell whether this is a shallow clone"}


def test_a_baseline_with_no_commit_history_is_reported_unknown():
    """must fire -- the baseline file has no commit touching it in HEAD at all (e.g. staged but never
    committed, or the path is wrong), so there is no anchor to count commits since."""
    report = _freshness_from_git_data(is_shallow=False, baseline=None, since_commits=[])
    assert report["state"] == "unknown"
    assert "no commit history" in report["reason"]


def test_a_failed_baseline_log_is_reported_with_its_own_reason_not_as_no_history():
    """must fire -- the git call for the baseline's own last commit did not merely come back empty (#405)."""
    report = _freshness_from_git_data(is_shallow=False, baseline=None, since_commits=None,
                                       baseline_error="fatal: bad object HEAD")
    assert report["state"] == "unknown"
    assert "bad object HEAD" in report["reason"], report
    assert "no commit history" not in report["reason"], report


def test_a_failed_since_log_is_reported_unknown_even_with_a_good_baseline():
    """must fire -- the baseline's own commit was found, but the second git log (commits since, scoped to
    `watched`) failed; a `None` here must not be read as "zero commits"."""
    report = _freshness_from_git_data(is_shallow=False, baseline=("sha1", "2026-08-01T00:00:00+00:00"),
                                       since_commits=None)
    assert report["state"] == "unknown"
    assert "git log" in report["reason"]


def test_watched_paths_cover_both_funding_instances():
    """`WATCHED_PATHS` is what #405 and #410 fund -- narrowing it silently (or widening it past what is
    reproduced) is exactly the "reads as covering the whole mechanism" trap the brief names."""
    assert "src/requivo/assets/prompts" in WATCHED_PATHS
    assert "src/requivo/assets/context" in WATCHED_PATHS
    assert "src/requivo/assets/perimeters" in WATCHED_PATHS
    assert "src/requivo/providers/anthropic/generators.py" in WATCHED_PATHS


def test_baseline_commits_since_finds_a_known_stale_baseline_in_a_synthetic_repo(tmp_path, monkeypatch):
    """Integration, not a fixture -- but a synthetic repo, not the real one (#450)."""
    import subprocess

    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (tmp_path / "watched").mkdir()
    (tmp_path / "watched" / "f.txt").write_text("0")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "b.txt").write_text("baseline")
    run("add", ".")
    run("commit", "-q", "-m", "baseline commit")
    (tmp_path / "watched" / "f.txt").write_text("1")
    run("add", ".")
    run("commit", "-q", "-m", "watched-path commit after the baseline")

    monkeypatch.setattr(golden_lib, "REPO", tmp_path)
    report = golden_lib.baseline_commits_since("fixtures/b.txt", watched=("watched",))
    assert report["state"] == "stale", report
    assert len(report["commits"]) == 1, report
    assert report["commits"][0]["subject"] == "watched-path commit after the baseline", report


def test_baseline_commits_since_reports_unknown_on_a_real_shallow_clone(tmp_path, monkeypatch):
    """The `unknown`-on-shallow behaviour, pinned end-to-end against a REAL shallow clone rather than only the
    pure-core `_freshness_from_git_data(is_shallow=True, ...)` case above (#450)."""
    import subprocess

    source = tmp_path / "source"
    source.mkdir()

    def run(*args, cwd=source):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (source / "fixtures").mkdir()
    (source / "fixtures" / "b.txt").write_text("baseline")
    run("add", ".")
    run("commit", "-q", "-m", "baseline commit")
    (source / "fixtures" / "b.txt").write_text("changed")
    run("add", ".")
    run("commit", "-q", "-m", "a second commit, so the clone below has real history to truncate")

    shallow = tmp_path / "shallow"
    # Two belt-and-suspenders reasons this clones a `file://` URI rather than a bare path, not just one.
    subprocess.run(["git", "clone", "-q", "--no-local", "--depth", "1", source.resolve().as_uri(),
                   str(shallow)], check=True, capture_output=True)

    monkeypatch.setattr(golden_lib, "REPO", shallow)
    report = golden_lib.baseline_commits_since("fixtures/b.txt")
    assert report["state"] == "unknown", report
    assert "shallow" in report["reason"], report


def test_baseline_commits_since_reports_unknown_for_a_path_with_no_history():
    """must fire, the negative control for the integration test above."""
    report = baseline_commits_since("fixtures/golden/this-slug-does-not-exist.runs.json")
    assert report["state"] == "unknown", report

def test_baseline_commits_since_orders_commits_oldest_first(tmp_path, monkeypatch):
    """git log's default order is newest-first."""
    import subprocess

    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (tmp_path / "watched").mkdir()
    (tmp_path / "watched" / "f.txt").write_text("0")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "b.txt").write_text("baseline")
    run("add", ".")
    run("commit", "-q", "-m", "baseline commit")
    for i in range(1, 4):
        (tmp_path / "watched" / "f.txt").write_text(str(i))
        run("add", ".")
        run("commit", "-q", "-m", f"watched commit {i}")

    monkeypatch.setattr(golden_lib, "REPO", tmp_path)
    report = golden_lib.baseline_commits_since("fixtures/b.txt", watched=("watched",))
    assert report["state"] == "stale", report
    subjects = [c["subject"] for c in report["commits"]]
    assert subjects == ["watched commit 1", "watched commit 2", "watched commit 3"], subjects


# `_HOSTILE_SUBJECTS` is #456's own reproduction.
#
# `\r` is listed first and carries the extra weight.
_HOSTILE_SUBJECTS = [
    "docs: tidy\rFORGED",       # CR -- the issue's own PoC, and the one universal-newlines hides
    "docs: tidy\x0bFORGED",     # VT
    "docs: tidy\x0cFORGED",     # FF
    "docs: tidy\x1cFORGED",     # FS
    "docs: tidy\x1dFORGED",     # GS
    "docs: tidy\x1eFORGED",     # RS
    "docs: tidy\x85FORGED",     # NEL
    "docs: tidy FORGED",   # LINE SEPARATOR
    "docs: tidy FORGED",   # PARAGRAPH SEPARATOR
]


def test_a_hostile_commit_subject_cannot_forge_a_second_commit_row(tmp_path, monkeypatch):
    """must fire -- #456. Each of `_HOSTILE_SUBJECTS` used to become *two* rows in `baseline_commits_since`'s
    own `commits` list."""
    import subprocess

    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (tmp_path / "watched").mkdir()
    (tmp_path / "watched" / "f.txt").write_text("start")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "b.txt").write_text("baseline")
    run("add", ".")
    run("commit", "-q", "-m", "baseline commit")

    subjects = [*_HOSTILE_SUBJECTS, "an entirely ordinary subject"]
    for i, subject in enumerate(subjects):
        (tmp_path / "watched" / "f.txt").write_text(f"content-{i}")
        run("add", ".")
        run("commit", "-q", "-m", subject)

    monkeypatch.setattr(golden_lib, "REPO", tmp_path)
    report = golden_lib.baseline_commits_since("fixtures/b.txt", watched=("watched",))
    assert report["state"] == "stale", report
    # One row per commit made -- never two, whatever the subject carried (the must-fire half).
    assert len(report["commits"]) == len(subjects), report["commits"]
    got_subjects = [c["subject"] for c in report["commits"]]
    assert got_subjects == subjects, got_subjects
    # No row's `sha` is ever the forged fragment split off a neighbour.
    for c in report["commits"]:
        assert c["sha"] != "FORGED", report["commits"]
        assert len(c["sha"]) == 9, report["commits"]
    # The must-not-fire control: the ordinary subject, last in the sequence, is unmangled.
    assert report["commits"][-1]["subject"] == "an entirely ordinary subject", report["commits"]


