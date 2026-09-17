"""LLM-authored prose cannot write a line of the terminal render path (#213)."""
from __future__ import annotations

import ast
import builtins
import inspect
import io
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _fakes import StubProvider, out, slot

from requivo.cli import converse
from requivo.core.contracts import (
    Brief,
    Challenge,
    ContextJudgment,
    DesignDecision,
    EngineOutput,
    EstimateDraft,
    Leverage,
    Opportunity,
    Stories,
)
from requivo.core.dependencies import propagate, thinner_evidence
from requivo.core.persistence import ArtifactStatus, RevisionRecord
from requivo.render.terminal import (
    docs_menu_rows,
    render_brief,
    render_context_judgment,
    render_dependency_map,
    render_docs_menu,
    render_estimate,
    render_evidence,
    render_grounding,
    render_impact,
    render_session_cost,
    render_stories,
    render_turn,
)
from requivo.services.discovery import DiscoveryService, Grounding

# A newline, then a claim at column 0, then a screen clear.
FORGED = "Real text.\nFORGED AT COLUMN ZERO\x1b[2J"

# Every character that can move a cursor or end a line.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _render(fn, *args) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


def _forged_lines(text: str) -> list:
    return [ln for ln in text.splitlines() if ln.startswith("FORGED")]


def _raw_controls(text: str) -> str:
    """The literal control characters still present, newlines excluded."""
    return "".join(c for c in _CONTROL.findall(text) if c != "\n")


def _model_with_question(q: str) -> EngineOutput:
    d = out({"problem": slot(80, "explicit", "high")}).model_dump()
    d["questions"] = [{"q": q, "slot": "problem", "why": "because"}]
    return EngineOutput.model_validate(d)


def _drive_converse(disco, request, *, answer="an answer") -> list:
    """Run `converse()` for real, recording the exact prompt string `input()` was handed (#330)."""
    prompts: list[str] = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return answer

    real_input = builtins.input
    builtins.input = fake_input
    try:
        converse(disco, request)
    finally:
        builtins.input = real_input
    return prompts


def _brief(**overrides) -> Brief:
    base = dict(
        problem="A problem.", solution="A solution.", complexity="medium",
        complexity_reasons=["A reason."], cost_driver="A driver.", risks=["A risk."],
        next_steps=["A step."], open_decisions=["An open decision."],
        challenges=[Challenge(headline="H", premise="P", alternative="A", consequence="C", recommendation="R")],
        decisions=[DesignDecision(decision="D", why="W", alternative="Alt", tradeoff="T")],
        opportunities=[Opportunity(text="O", leverage=Leverage.high, modules=["m"])],
    )
    base.update(overrides)
    return Brief(**base)


def _revision(revision: int, priced_as_of: str) -> RevisionRecord:
    return RevisionRecord(revision=revision, created_at=f"2026-01-0{revision}T00:00:00Z",
                         previous_revision=revision - 1 or None, usage_input_tokens=1000, usage_output_tokens=200,
                         usage_cache_read_tokens=0, usage_cache_write_tokens=0, usage_rate_per_mtok=(2.0, 10.0),
                         usage_priced_as_of=priced_as_of)


# The renderer names the forged sweep below actually calls (#331), with the persisted field each reads
# where it is not model prose: RevisionRecord.usage_priced_as_of (#388), context_cards (#40),
# ArtifactStatus.filename (#544), ContextJudgment.reason (#593).
_SWEPT_RENDERERS = {
    "render_turn", "render_brief", "render_stories", "render_estimate", "render_dependency_map", "render_impact",
    "render_evidence", "render_session_cost", "render_grounding", "render_docs_menu", "render_context_judgment",
}

# `render_*` functions in `render/terminal.py` that render no model-authored prose.
_NON_PROSE_RENDERERS = {
    "render_understanding": "labels are schema slot ids (via slot_label), not model-authored prose",
    "render_readiness": "a fixed verdict string plus schema slot id labels",
    "render_next_command": "a fixed command template plus a slug and an artifact type, no model text",
    "render_stale": "artifact filenames from ARTIFACT_FILENAMES and schema slot labels, no model text",
    "render_usage": "the in-process usage ledger this run itself built -- never persisted, never read back off disk (#388)",
    "render_turn_state": "render_understanding plus the readiness verdict -- schema slot labels and a fixed verdict string (#592)",
}

# What each swept renderer must also have printed, proving the other rows rendered something to be forged through.
_ALSO_RENDERED = {"render_session_cost": "2026-01-01", "render_docs_menu": "DOCUMENTS"}


def _thinner(model: EngineOutput) -> EngineOutput:
    """`model` with every slot's confidence reduced to `empty` -- the derivation-time state."""
    d = model.model_dump()
    for s in d["model"].values():
        s["confidence"] = "empty"
    return EngineOutput.model_validate(d)


def _with_unreviewable(model: EngineOutput) -> EngineOutput:
    """`model` plus a second forged decision that names no slot it rests on."""
    d = model.model_dump()
    d["decisions"].append({"decision": FORGED + " (two)", "derived_from": []})
    return EngineOutput.model_validate(d)


def test_every_llm_authored_string_the_terminal_renders_is_neutralized():
    """The sweep, and the reason this file exists rather than eleven tests beside eleven renderers."""
    d = out({"problem": slot(80, "explicit", "high")}).model_dump()
    d["questions"] = [{"q": FORGED, "slot": "problem", "why": FORGED}]
    d["summary"]["objective"] = FORGED
    d["decisions"] = [{"decision": FORGED, "why": FORGED, "alternative": FORGED, "tradeoff": FORGED, "derived_from": ["problem"]}]
    d["challenges"] = [{"headline": FORGED, "premise": FORGED, "alternative": FORGED, "consequence": FORGED,
                        "recommendation": FORGED, "contests": ["problem"]}]
    d["opportunities"] = [{"text": FORGED, "leverage": "high", "modules": [FORGED]}]
    forged_model = EngineOutput.model_validate(d)
    brief = _brief(problem=FORGED, solution=FORGED, complexity_reasons=[FORGED], cost_driver=FORGED, risks=[FORGED],
                   next_steps=[FORGED], open_decisions=[FORGED],
                   challenges=[Challenge(headline=FORGED, premise=FORGED, alternative=FORGED, consequence=FORGED, recommendation=FORGED)],
                   decisions=[DesignDecision(decision=FORGED, why=FORGED, alternative=FORGED, tradeoff=FORGED)],
                   opportunities=[Opportunity(text=FORGED, leverage=Leverage.high, modules=[FORGED])])
    stories = Stories(stories=[{"id": FORGED, "title": FORGED, "as_a": FORGED, "i_want": FORGED, "so_that": FORGED,
                                "acceptance": [FORGED], "slots": ["problem"]}])
    estimate = EstimateDraft(items=[{"story_id": "S1", "title": FORGED, "complexity": "M", "days_low": 1, "days_high": 2,
                                     "drives": [FORGED]}], risks=[FORGED])
    renders = {
        "render_turn": _render(render_turn, forged_model),
        "render_brief": _render(render_brief, forged_model, brief),
        "render_stories": _render(render_stories, stories),
        "render_estimate": _render(render_estimate, estimate, ["problem"], "low"),
        "render_dependency_map": _render(render_dependency_map, forged_model),
        "render_impact": _render(render_impact, propagate(forged_model, ["problem"])),
        # Both arms print a decision: the forged one rests on `problem`.
        "render_evidence": _render(render_evidence, thinner_evidence(_thinner(forged_model), _with_unreviewable(forged_model))),
        "render_session_cost": _render(render_session_cost, [_revision(1, "2026-01-01"), _revision(2, FORGED)]),
        "render_grounding": _render(render_grounding, [FORGED]),
        "render_docs_menu": _render(render_docs_menu, docs_menu_rows({"prd": ArtifactStatus(
            revision=1, filename=FORGED, updated_at="2026-01-01T00:00:00Z", stale=False)})),
        "render_context_judgment": _render(render_context_judgment, Grounding(ContextJudgment(decision="uncovered", reason=FORGED), "")),
    }
    assert set(renders) == _SWEPT_RENDERERS
    for name, text in renders.items():
        assert not _forged_lines(text), f"{name} let LLM text start a line: {_forged_lines(text)}"
        assert _raw_controls(text) == "", f"{name} emitted a raw control character"
        # Must fire: neutralized means escaped and still readable, never dropped (some readouts wrap their prose).
        assert "FORGEDATCOLUMNZERO\\x1b[2J" in re.sub(r"\s+", "", text), f"{name} dropped the forged field: {text}"
        assert _ALSO_RENDERED.get(name, "") in text, f"{name} rendered nothing to be forged through"


def test_the_forged_sweep_covers_every_prose_renderer_in_the_module():
    """Derived, not enumerated (#331): the scan set used to be a fixed tuple of six imported names."""
    from requivo.render import terminal as terminal_module

    declared = {name for name, fn in inspect.getmembers(terminal_module, inspect.isfunction)
                if name.startswith("render_") and fn.__module__ == terminal_module.__name__}
    missing = declared - (_SWEPT_RENDERERS | set(_NON_PROSE_RENDERERS))
    assert not missing, (f"{sorted(missing)} are covered by neither the forged sweep nor _NON_PROSE_RENDERERS -- add a "
                         "forged-args entry to `renders`, or name it in _NON_PROSE_RENDERERS with a reason")
    assert "render_something_that_does_not_exist" not in declared  # the control for the control


def test_ordinary_prose_renders_byte_for_byte_unchanged():
    """The control, and the half a security fix ships without."""
    text = _render(render_turn, _model_with_question("How are approvals routed today?"))
    assert "1. How are approvals routed today?" in text and "\\" not in text
    brief_text = _render(render_brief, out({"problem": slot(80, "explicit", "high")}), _brief())
    for expected in ("A problem.", "A solution.", "A reason.", "A driver.", "A risk.", "A step."):
        assert expected in brief_text
    assert "\\" not in brief_text
    assert "Product context" in _render(render_grounding, None), "the unnarrowed branch renders its own line"


def test_the_four_grounding_outcomes_read_as_four_different_answers():
    """The control for `render_context_judgment`, and the reason it exists at all (#492)."""
    texts = {
        "not asked": _render(render_context_judgment, Grounding(None, "this provider cannot")),
        "none": _render(render_context_judgment, Grounding(ContextJudgment(decision="none", reason="ordinary software"), "")),
        "installed": _render(render_context_judgment, Grounding(ContextJudgment(decision="installed", reason="finance",
                                                                                cards=["financial-reporting"]), "")),
        "uncovered": _render(render_context_judgment, Grounding(ContextJudgment(decision="uncovered", reason="dentistry"), "")),
    }
    assert len(set(texts.values())) == 4, texts
    assert "not checked" in texts["not asked"] and "financial-reporting" in texts["installed"]
    assert "⚠" in texts["uncovered"] and "⚠" not in texts["none"], "the one outcome a reader must act on reads like the others"


# ── #331: a static sweep whose scan set is a file tree, not a list of modules ─────
# Does any code under `src/requivo/` outside core/providers/services read a `Question`'s `q` or `why`
# other than as the direct argument of `display_text`/`display_token`? Every read, not only one inside print().


def _question_bound_names(tree: ast.Module) -> set:
    """Every local name a `for` loop binds to one element of `<something>.questions`."""
    names: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate" and it.args:
            it = it.args[0]
        if not (isinstance(it, ast.Attribute) and it.attr == "questions"):
            continue
        target = node.target
        name_node = target.elts[-1] if isinstance(target, ast.Tuple) and target.elts else target
        if isinstance(name_node, ast.Name):
            names.add(name_node.id)
    return names


def _question_prose_leaks_in_file(path: Path) -> list:
    """Every read of a Question-bound name's `.q`/`.why` in `path` not wrapped in `display_text`/`display_token` (#330)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    question_vars = _question_bound_names(tree)
    violations: list = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr in ("q", "why")
                and isinstance(node.value, ast.Name) and node.value.id in question_vars):
            continue
        parent = parents.get(id(node))
        wrapped = (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                   and parent.func.id in ("display_text", "display_token"))
        if not wrapped:
            violations.append(f"{path}:{node.lineno}: unescaped {node.value.id}.{node.attr} (a Question field) "
                              "-- not the direct argument of display_text/display_token")
    return violations


def _question_prose_leaks(root: Path) -> list:
    """`_question_prose_leaks_in_file` over every `.py` under `root`; an empty scan proves nothing (#10)."""
    if not root.is_dir():
        raise AssertionError(f"scan could not read {root}: no such directory")
    found = sorted(root.rglob("*.py"))
    if not found:
        raise AssertionError(f"scan of {root} found no Python files -- an empty scan proves nothing")
    return [v for path in found for v in _question_prose_leaks_in_file(path)]


SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "requivo"
_TERMINAL_SURFACE_PACKAGES = ("render", "deterministic", "web")


def test_no_question_field_reaches_a_terminal_call_unescaped_anywhere_in_the_surface_tree():
    """The real scan: three packages plus every top-level module, derived rather than listed (#550)."""
    modules = sorted(p.name for p in SRC_ROOT.glob("*.py"))
    assert modules, f"scan of {SRC_ROOT} found no top-level modules -- 'could not look'"
    violations: list = []
    for entry in _TERMINAL_SURFACE_PACKAGES + tuple(modules):
        target = SRC_ROOT / entry
        violations += _question_prose_leaks(target) if target.is_dir() else _question_prose_leaks_in_file(target)
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("tui_body, expect_violation", [
    pytest.param('def show(out):\n    for i, q in enumerate(out.questions, 1):\n        print(f"{i}. {q.q}")\n',
                 True, id="direct-raw-read-must-fire"),
    pytest.param('from requivo.core.selectors import display_text\n\n\ndef show(out):\n'
                 '    for i, q in enumerate(out.questions, 1):\n        safe_q = display_text(q.q)\n        print(f"{i}. {safe_q}")\n',
                 False, id="escaped-through-display-text-must-not-fire"),
    pytest.param('def show(out):\n    for i, q in enumerate(out.questions, 1):\n        msg = q.q\n        print(f"{i}. {msg}")\n',
                 True, id="local-variable-indirection-must-fire"),
])
def test_the_question_scan_tells_a_raw_read_from_an_escaped_one(tmp_path, tui_body, expect_violation):
    """MUST-FIRE: the three defining shapes of the scan, by id."""
    pkg = tmp_path / "requivo"
    pkg.mkdir()
    (pkg / "tui.py").write_text(tui_body, encoding="utf-8")
    violations = _question_prose_leaks(pkg)
    if expect_violation:
        assert len(violations) == 1 and "tui.py" in violations[0] and ".q" in violations[0]
    else:
        assert violations == []


def test_the_question_scan_refuses_an_empty_or_missing_root(tmp_path):
    """The #10 discipline: `Path.rglob` on a directory that does not exist returns `[]` and raises nothing."""
    with pytest.raises(AssertionError, match="no such directory"):
        _question_prose_leaks(tmp_path / "does-not-exist")
    (tmp_path / "empty").mkdir()
    with pytest.raises(AssertionError, match="no Python files"):
        _question_prose_leaks(tmp_path / "empty")


# ── #330: the interactive loop's own `input()` prompt, not a renderer ────────────


def _converse_with(question: str) -> tuple[StubProvider, list]:
    provider = StubProvider(_model_with_question(question), out({"problem": slot(80, "explicit", "high")}))
    return provider, _drive_converse(DiscoveryService(provider), "a request")


@pytest.mark.parametrize("question, forged", [(FORGED, True), ("How are approvals routed today?", False)],
                         ids=["forged-question", "ordinary-question-control"])
def test_a_forged_question_cannot_write_a_line_at_column_zero_of_the_input_prompt(question, forged):
    """A forged `Question.q` reaches `input()`'s prompt through the real `converse()` path, not through `render_turn`."""
    _, prompts = _converse_with(question)
    prompt = prompts[0]
    if forged:
        assert not [ln for ln in prompt.splitlines() if ln.startswith("FORGED")], prompt
        assert _raw_controls(prompt) == "", prompt
        assert "FORGED AT COLUMN ZERO" in prompt and "\\x1b[2J" in prompt, "neutralized means still readable, never dropped"
    else:
        assert "[1/1] How are approvals routed today?" in prompt and "\\" not in prompt


def test_a_forged_question_cannot_break_the_answer_folded_back_to_the_provider():
    """`cli.py` folds `q.q` into the `[slot: ...] Q: ... -> A: ...` string sent back as the next turn's `answers`."""
    provider, _ = _converse_with(FORGED)
    assert provider.analyze_calls == 2, "the loop did not make a refinement turn to inspect"
    folded = provider.analyze_kwargs[1]["answers"]
    assert not [ln for ln in folded.splitlines() if ln.startswith("FORGED")], folded
    assert _raw_controls(folded) == "" and "FORGED AT COLUMN ZERO" in folded, folded
    assert folded.startswith("[slot: problem] Q: "), "the folded structure itself must survive"
