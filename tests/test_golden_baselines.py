"""A committed golden baseline must still measure what fixtures/golden/requests.md currently
describes -- or say, in code, exactly why not (#275).

`golden_run.py` reads `requests.md` and writes each `<slug>.runs.json` baseline; `golden_diff.py`
reads the baseline and compares it to a *fresh* capture. Neither ever compares the baseline to the
request set it was supposedly captured from, so a baseline can silently drift out of step with
`requests.md` and nothing goes red -- the harness just answers a different question than it
reports, with no warning. That happened once, on `main`: `training-budget`'s answer sheet was
deepened from ~2 layers per slot to up to 10 (#193) and the baseline went uncaptured for a while
(#194), until the full seven-baseline re-capture in #405 brought every committed baseline,
including this one, back into agreement with `requests.md`.

This is a pure offline file comparison -- no discovery call, no client, no network -- over the two
files already on disk (`requests.md`, `<slug>.runs.json`).

Three genuinely different situations, not one collapsed "mismatch" verdict:

  - no committed baseline at all for a request in `requests.md` -- LEGITIMATE. Nobody has paid for
    the capture yet; this is not a failure.
  - a committed baseline whose stored `request`/`answers` disagree with what `requests.md` now
    says for that slug -- DRIFT. Goes red naming the slug and which field(s) disagree, unless the
    slug is a declared exception in `_DECLARED_DRIFT` naming the issue that owns the (paid)
    re-capture -- the state `training-budget` was in until #405's re-capture retired it; see the
    comment above `_DECLARED_DRIFT` below.
  - a committed baseline for a slug `requests.md` no longer names at all -- ORPHANED. Dead weight:
    neither `golden_run.py` nor `golden_diff.py` reads it any more, since both key off
    `requests.md`.

`_report()` is a pure function over two in-memory dicts (not the real files), so the must-fire and
must-not-fire cases below exercise the actual comparison the real-fixture test uses, without needing
to write or mutate anything on disk.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from golden_lib import GOLDEN, REQUESTS, parse_requests  # noqa: E402

# A baseline whose stored request/answers are known to disagree with requests.md, kept on disk on
# purpose until the paid re-capture lands (K x GOLDEN_TURNS API calls for an interactive request --
# not spent by this suite, which makes no network call of its own). Every entry names the issue that
# owns the re-capture. The same idiom `tests/test_encoding.py` and `tests/test_persistence.py`
# already use for a by-design allowlist: an entry whose files have since come back into agreement is
# stale and goes red in `test_a_stale_declared_exception_that_now_agrees_is_flagged` below.
# Empty, and that is the healthy state: every baseline currently agrees with requests.md. The
# mechanism stays because the next asset edit that outruns its re-capture needs it again -- the
# `training-budget`/#194 entry that lived here was retired by the full re-capture in #405, which
# is what an entry is for.
_DECLARED_DRIFT: dict[str, str] = {}


def _drifted_fields(req: dict, baseline: dict) -> list[str]:
    """Which of `request`/`answers` a committed baseline disagrees with `requests.md` on. Empty
    means the baseline agrees."""
    fields = []
    if baseline.get("request") != req.get("request"):
        fields.append("request")
    if baseline.get("answers", {}) != req.get("answers", {}):
        fields.append("answers")
    return fields


def _report(requests_by_slug: dict[str, dict], baselines_by_slug: dict[str, dict],
            declared_drift: dict[str, str]) -> dict:
    """The three verdicts above, computed over injected data so the pipeline itself -- not just
    `_drifted_fields` -- can be proven red or green against a deliberately constructed fixture,
    without touching the real committed baselines."""
    drifted, stale_exceptions = [], []
    for slug, req in requests_by_slug.items():
        baseline = baselines_by_slug.get(slug)
        if baseline is None:
            continue  # legitimate: nobody has paid for this capture yet
        fields = _drifted_fields(req, baseline)
        if not fields:
            if slug in declared_drift:
                stale_exceptions.append(slug)
            continue
        if slug not in declared_drift:
            drifted.append(slug)
    orphans = sorted(set(baselines_by_slug) - set(requests_by_slug))
    return {"drifted": sorted(drifted), "stale_exceptions": sorted(stale_exceptions),
            "orphans": orphans}


def _all_committed_baselines() -> dict[str, dict]:
    return {p.name.removesuffix(".runs.json"): json.loads(p.read_text(encoding="utf-8"))
            for p in GOLDEN.glob("*.runs.json")}


# ── must-fire / must-not-fire pairs over synthetic data -- no disk, no real fixtures ────────────

def test_an_edited_request_line_without_recapture_is_reported_as_drift():
    """must fire."""
    requests = {"x": {"slug": "x", "request": "the NEW wording", "answers": {}}}
    baselines = {"x": {"request": "the OLD wording", "answers": {}}}
    report = _report(requests, baselines, declared_drift={})
    assert report["drifted"] == ["x"]


def test_an_added_answer_layer_without_recapture_is_reported_as_drift():
    """must fire -- the shape #194/#275 actually found."""
    requests = {"x": {"slug": "x", "request": "same", "answers": {"workflow": ["a", "b", "c"]}}}
    baselines = {"x": {"request": "same", "answers": {"workflow": ["a", "b"]}}}
    report = _report(requests, baselines, declared_drift={})
    assert report["drifted"] == ["x"]


def test_a_matching_baseline_is_not_reported_as_drift():
    """must not fire -- the positive control for the two tests above: without it, a comparison that
    never actually compared anything would pass them just as happily."""
    requests = {"x": {"slug": "x", "request": "same", "answers": {"a": ["1"]}}}
    baselines = {"x": {"request": "same", "answers": {"a": ["1"]}}}
    report = _report(requests, baselines, declared_drift={})
    assert report == {"drifted": [], "stale_exceptions": [], "orphans": []}


def test_a_request_with_no_committed_baseline_is_not_reported_as_drift():
    """must not fire -- the "nobody has paid for this yet" state must not read like a disagreement.
    This is the case a single "mismatch" verdict collapses into the one above it (#275)."""
    requests = {"x": {"slug": "x", "request": "r", "answers": {}}}
    report = _report(requests, baselines_by_slug={}, declared_drift={})
    assert report == {"drifted": [], "stale_exceptions": [], "orphans": []}


def test_a_declared_exception_suppresses_a_known_drift():
    """must not fire, given the exception -- naming a slug in `_DECLARED_DRIFT` is what lets a known,
    accepted drift stay green until the re-capture lands."""
    requests = {"x": {"slug": "x", "request": "new", "answers": {}}}
    baselines = {"x": {"request": "old", "answers": {}}}
    report = _report(requests, baselines, declared_drift={"x": "#999"})
    assert report["drifted"] == []


def test_a_stale_declared_exception_that_now_agrees_is_flagged():
    """must fire -- an exception naming a slug whose files have since come back into agreement is
    suppressing nothing and is unchecked prose, the same rule `tests/test_encoding.py`'s
    `test_the_by_design_exemptions_still_exist` applies to its own table."""
    requests = {"x": {"slug": "x", "request": "r", "answers": {}}}
    baselines = {"x": {"request": "r", "answers": {}}}
    report = _report(requests, baselines, declared_drift={"x": "#999"})
    assert report["stale_exceptions"] == ["x"]


def test_a_baseline_with_no_matching_request_is_orphaned():
    """must fire -- a baseline requests.md no longer names is dead weight, not a legitimate 'unpaid'
    state, since it is the reverse case (baseline present, request gone)."""
    requests: dict[str, dict] = {}
    baselines = {"gone": {"request": "r", "answers": {}}}
    report = _report(requests, baselines, declared_drift={})
    assert report["orphans"] == ["gone"]


# ── the real committed fixtures ──────────────────────────────────────────────────────────────────

def test_the_scan_actually_sees_the_real_requests_and_baselines():
    """Guards the guard: an empty scan would make every assertion below vacuously true (invariant
    7's own lesson -- `assert not []` is an all-clear nobody earned)."""
    requests = parse_requests(REQUESTS)
    baselines = _all_committed_baselines()
    assert len(requests) >= 5, f"expected several requests in {REQUESTS}, found {len(requests)}"
    assert len(baselines) >= 5, f"expected several committed baselines in {GOLDEN}, found {len(baselines)}"


def test_every_committed_baseline_agrees_with_requests_md_or_is_a_declared_exception():
    requests = {r["slug"]: r for r in parse_requests(REQUESTS)}
    baselines = _all_committed_baselines()
    report = _report(requests, baselines, _DECLARED_DRIFT)

    assert not report["drifted"], (
        "committed baseline(s) disagree with fixtures/golden/requests.md, and are not a declared "
        "exception -- re-capture with `python scripts/golden_run.py <slug>` (or "
        "`<slug> --brief`/interactively as the request requires), or add a `_DECLARED_DRIFT` entry "
        f"above naming the issue that owns the re-capture: {report['drifted']}"
    )
    assert not report["stale_exceptions"], (
        "these `_DECLARED_DRIFT` entries now agree with requests.md -- the exception suppresses "
        f"nothing and is stale; remove it: {report['stale_exceptions']}"
    )
    assert not report["orphans"], (
        "these committed baselines have no matching request in requests.md any more -- delete the "
        f"file or restore the request: {report['orphans']}"
    )


def test_declared_drift_exceptions_name_a_real_slug_with_a_real_baseline():
    """The mirror of the by-design exemption tables in `tests/test_encoding.py` and
    `tests/test_persistence.py`: an exception naming a slug `requests.md` no longer carries,
    or one with no committed baseline at all, is unchecked prose suppressing nothing."""
    requests = {r["slug"] for r in parse_requests(REQUESTS)}
    baselines = _all_committed_baselines()
    for slug in _DECLARED_DRIFT:
        assert slug in requests, f"_DECLARED_DRIFT names {slug!r}, which is not in requests.md"
        assert slug in baselines, (
            f"_DECLARED_DRIFT names {slug!r}, which has no committed baseline to except"
        )
