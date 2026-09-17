"""A bare `golden_run.py` must not fold an interactive request into a full-set capture (#276)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import golden_run  # noqa: E402
from golden_run import planned_calls, select_runs  # noqa: E402


def _req(slug: str, answers: dict | None = None) -> dict:
    return {"slug": slug, "request": f"request for {slug}", "answers": answers or {}}


_SINGLE = _req("single-pass-a")
_INTERACTIVE = _req("interactive-a", answers={"problem": ["layer one"]})


# ── must-fire / must-not-fire pairs ─────────────────────────────────────────────────────────────

def test_a_bare_invocation_skips_every_interactive_request():
    """must fire -- the defect this issue exists to close: a bare run used to include everything."""
    selected, skipped = select_runs([_SINGLE, _INTERACTIVE], wanted=set(), capture_all=False)
    assert selected == [_SINGLE]
    assert skipped == [_INTERACTIVE]


def test_a_bare_invocation_with_no_interactive_requests_captures_everything():
    """must not fire -- the positive control: a set with nothing interactive in it must not lose a single-pass
    request to the same filter."""
    selected, skipped = select_runs([_SINGLE], wanted=set(), capture_all=False)
    assert selected == [_SINGLE]
    assert skipped == []


def test_naming_an_interactive_slug_explicitly_still_captures_it():
    """must not fire -- acceptance criterion: naming the slug is still today's behaviour."""
    selected, skipped = select_runs([_SINGLE, _INTERACTIVE], wanted={"interactive-a"},
                                    capture_all=False)
    assert selected == [_INTERACTIVE]
    assert skipped == []


def test_all_flag_captures_every_interactive_request_in_a_full_set_run():
    """must not fire, given --all -- the documented opt-in for a full-set run that really does want
    everything, cost and all."""
    selected, skipped = select_runs([_SINGLE, _INTERACTIVE], wanted=set(), capture_all=True)
    assert selected == [_SINGLE, _INTERACTIVE]
    assert skipped == []


def test_naming_an_explicit_slug_ignores_the_all_flag_and_the_interactive_skip_alike():
    """An explicit slug list is exhaustive on its own."""
    selected, skipped = select_runs([_SINGLE, _INTERACTIVE], wanted={"single-pass-a"},
                                    capture_all=True)
    assert selected == [_SINGLE]
    assert skipped == []


# ── main() actually uses the selection and reports what it skipped ─────────────────────────────

def test_main_prints_which_interactive_requests_it_skipped_and_how_to_capture_them(
        tmp_path, monkeypatch, capsys):
    """`main()` with the client and capture loop stubbed out (#276)."""
    captured_slugs: list[str] = []
    monkeypatch.setattr(golden_run, "GOLDEN", tmp_path)
    monkeypatch.setattr(golden_run, "REPO", tmp_path.parent)
    monkeypatch.setattr(golden_run, "REQUESTS", tmp_path / "requests.md")
    (tmp_path / "requests.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(golden_run, "Anthropic", lambda *a, **k: object())
    monkeypatch.setattr(golden_run, "parse_requests", lambda _path: [_SINGLE, _INTERACTIVE])
    monkeypatch.setattr(golden_run, "capture",
                        lambda _client, req, _with_brief, *, model: captured_slugs.append(
                            req["slug"]))

    rc = golden_run.main([])

    assert rc == 0
    assert captured_slugs == ["single-pass-a"]
    err = capsys.readouterr().err
    assert "interactive-a" in err
    assert "golden_run.py interactive-a" in err, (
        "the skip line should name the exact command to capture the skipped request alone"
    )


# ── the model is resolved once and threaded, never re-read per capture (#515) ──────────────────

def test_main_resolves_the_model_once_and_threads_it_to_every_capture(tmp_path, monkeypatch):
    """`capture_model()` reads the environment."""
    resolutions: list[str] = []

    def fake_capture_model() -> str:
        resolutions.append("called")
        return f"model-{len(resolutions)}"

    seen_models: list[str] = []
    monkeypatch.setattr(golden_run, "GOLDEN", tmp_path)
    monkeypatch.setattr(golden_run, "REPO", tmp_path.parent)
    monkeypatch.setattr(golden_run, "REQUESTS", tmp_path / "requests.md")
    (tmp_path / "requests.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(golden_run, "Anthropic", lambda *a, **k: object())
    monkeypatch.setattr(golden_run, "capture_model", fake_capture_model)
    monkeypatch.setattr(golden_run, "parse_requests",
                        lambda _path: [_req("single-pass-a"), _req("single-pass-b")])
    monkeypatch.setattr(golden_run, "capture",
                        lambda _client, _req, _with_brief, *, model: seen_models.append(model))

    assert golden_run.main([]) == 0
    assert resolutions == ["called"], "capture_model() must be resolved exactly once per invocation"
    assert seen_models == ["model-1", "model-1"], (
        "every capture in one run must be threaded the same resolved id"
    )


# ── the announced cost is derived from the set, never written down as a total (#290) ───────────

def test_the_announced_call_count_moves_with_the_request_set():
    """must fire on the class #290 reports: a total written into prose is right the day it's written and
    silently wrong once an eighth request lands, with nothing going red."""
    six = [_req(f"single-{i}") for i in range(6)]
    assert planned_calls(six, with_brief=False) == 6 * golden_run.K
    assert planned_calls(six + [_req("single-6")], with_brief=False) == 7 * golden_run.K


def test_an_interactive_request_is_costed_at_its_own_per_turn_rate():
    """must fire -- and it is why the total cannot be one multiplication over `len(runs)`."""
    mixed = [_SINGLE, _INTERACTIVE]
    assert planned_calls(mixed, with_brief=False) == golden_run.K * (1 + golden_run.TURNS)
    assert planned_calls([_SINGLE, _SINGLE], with_brief=False) < planned_calls(mixed, with_brief=False)


def test_brief_doubles_a_single_pass_request_and_leaves_an_interactive_one_alone():
    """must not fire on the interactive half -- the positive control for the `--brief` arm."""
    assert planned_calls([_SINGLE], with_brief=True) == 2 * golden_run.K
    assert planned_calls([_SINGLE], with_brief=True) > planned_calls([_SINGLE], with_brief=False)
    assert planned_calls([_INTERACTIVE], with_brief=True) == planned_calls([_INTERACTIVE], with_brief=False)


def test_main_announces_the_count_it_computed_for_the_set_it_actually_selected(
        tmp_path, monkeypatch, capsys):
    """The derivation is only worth having if the printed line uses it."""
    monkeypatch.setattr(golden_run, "GOLDEN", tmp_path)
    monkeypatch.setattr(golden_run, "REPO", tmp_path.parent)
    monkeypatch.setattr(golden_run, "REQUESTS", tmp_path / "requests.md")
    (tmp_path / "requests.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(golden_run, "Anthropic", lambda *a, **k: object())
    monkeypatch.setattr(golden_run, "parse_requests", lambda _path: [_SINGLE, _INTERACTIVE])
    monkeypatch.setattr(golden_run, "capture", lambda _client, _req, _with_brief, **_kw: None)

    assert golden_run.main([]) == 0

    out = capsys.readouterr().out
    assert f"up to {planned_calls([_SINGLE], with_brief=False)} API calls" in out
    assert f"up to {planned_calls([_SINGLE, _INTERACTIVE], with_brief=False)} API calls" not in out
