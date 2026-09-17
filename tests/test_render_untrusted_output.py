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
from _fakes import full_slots, out, slot

from requivo.cli import converse
from requivo.core.contracts import (
    Brief,
    Challenge,
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
from requivo.services.discovery import DiscoveryService

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


class _StubProvider:
    """The minimal `ReasoningProvider` a `converse()` drive needs."""

    name = "stub"

    def __init__(self, *turns: EngineOutput):
        self.turns = list(turns)
        self.analyze_calls: list[dict] = []

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
                perimeter=None):
        self.analyze_calls.append({
            "request": request, "current_model": current_model, "answers": answers, "only": only,
        })
        return self.turns.pop(0)

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        raise AssertionError("converse() never generates an artifact")

    def model_name(self) -> str:
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=None) -> dict:
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": "sha256:0"}


def _drive_converse(disco, request, *, answer="an answer") -> list:
    """Run `converse()` for real, patching `input()` to record the exact prompt string it was handed -- not
    just to supply an answer, the way every other patch of `input()` in this repo does (#330)."""
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
        challenges=[Challenge(headline="H", premise="P", alternative="A", consequence="C",
                              recommendation="R")],
        decisions=[DesignDecision(decision="D", why="W", alternative="Alt", tradeoff="T")],
        opportunities=[Opportunity(text="O", leverage=Leverage.high, modules=["m"])],
    )
    base.update(overrides)
    return Brief(**base)


def test_a_question_cannot_forge_a_line_of_the_turn_view():
    """`render_turn` is the first thing a user ever sees."""
    text = _render(render_turn, _model_with_question(FORGED))
    assert not _forged_lines(text), text
    assert _raw_controls(text) == ""
    # Must fire: neutralized means escaped and still readable, never dropped.
    assert "FORGED AT COLUMN ZERO" in text
    assert "\\x1b[2J" in text


def test_a_challenge_cannot_forge_a_line_of_the_decision_brief():
    """The brief is the deliverable and a challenge is the part a reader acts on."""
    model = out({"problem": slot(80, "explicit", "high")})
    for field in ("headline", "premise", "alternative", "consequence", "recommendation"):
        fields = {"headline": "H", "premise": "P", "alternative": "A", "consequence": "C",
                  "recommendation": "R", field: FORGED}
        challenge = Challenge(**fields)
        text = _render(render_brief, model, _brief(challenges=[challenge]))
        assert not _forged_lines(text), field
        assert _raw_controls(text) == "", field
        assert "FORGED AT COLUMN ZERO" in text, field


def test_a_persisted_usage_priced_as_of_cannot_forge_a_line_of_the_session_cost_view():
    """`render_session_cost`'s `usage_priced_as_of` is a persisted `RevisionRecord` field (#388)."""
    clean = RevisionRecord(
        revision=1, created_at="2026-01-01T00:00:00Z",
        usage_input_tokens=1000, usage_output_tokens=200,
        usage_cache_read_tokens=0, usage_cache_write_tokens=0,
        usage_rate_per_mtok=(2.0, 10.0), usage_priced_as_of="2026-01-01",
    )
    forged = RevisionRecord(
        revision=2, created_at="2026-01-02T00:00:00Z", previous_revision=1,
        usage_input_tokens=500, usage_output_tokens=100,
        usage_cache_read_tokens=0, usage_cache_write_tokens=0,
        usage_rate_per_mtok=(2.0, 10.0), usage_priced_as_of=FORGED,
    )
    text = _render(render_session_cost, [clean, forged])
    assert not _forged_lines(text), text
    assert _raw_controls(text) == ""
    # Must fire, both halves: the clean revision's date proves the "rates as of" stamp actually rendered, and the forged text still showing up (inside the neutralized token, never at column 0) proves it was processed rather than silently dropped.
    assert "2026-01-01" in text
    assert "FORGED AT COLUMN ZERO" in text


def test_a_forged_artifact_filename_cannot_write_a_line_of_the_docs_menu():
    """`render_docs_menu`'s filename comes off a persisted `ArtifactStatus` (#544)."""
    forged = ArtifactStatus(revision=1, filename=FORGED, updated_at="2026-01-01T00:00:00Z", stale=False)
    text = _render(render_docs_menu, docs_menu_rows({"prd": forged}))
    assert not _forged_lines(text), text
    assert _raw_controls(text) == ""
    assert "FORGED AT COLUMN ZERO" in text, "the forged filename was dropped rather than neutralized"
    assert "DOCUMENTS" in text, "the other rows rendered nothing to be forged through"


def test_a_forged_context_card_name_cannot_write_a_line_of_the_grounding_readout():
    """`render_grounding`'s input is a persisted `context_cards` entry (#40)."""
    text = _render(render_grounding, [FORGED])
    assert not _forged_lines(text), text
    assert _raw_controls(text) == ""
    assert "FORGED AT COLUMN ZERO" in text, "the forged card name was dropped rather than neutralized"

    unnarrowed = _render(render_grounding, None)
    assert "Product context" in unnarrowed, "the other branch rendered nothing to be forged through"


# The renderer names the forged sweep below actually calls (#331).
_SWEPT_RENDERERS = {
    "render_turn", "render_brief", "render_stories", "render_estimate",
    "render_dependency_map", "render_impact",
    # Prints the decision text of every flagged and every unreviewable decision (#493).
    "render_evidence",
    # render_session_cost is swept separately, below (#388).
    "render_session_cost",
    # render_grounding likewise, and for the same class one field along (#40).
    "render_grounding",
    # render_docs_menu's untrusted field is a persisted ArtifactStatus.filename (#544).
    "render_docs_menu",
    # render_context_judgment's untrusted field is `ContextJudgment.reason` (#593).
    "render_context_judgment",
}

# `render_*` functions in `render/terminal.py` that render no model-authored prose.
_NON_PROSE_RENDERERS = {
    "render_understanding": "labels are schema slot ids (via slot_label), not model-authored prose",
    "render_readiness": "a fixed verdict string plus schema slot id labels",
    "render_next_command": "a fixed command template plus a slug and an artifact type, no model text",
    "render_stale": "artifact filenames from ARTIFACT_FILENAMES and schema slot labels, no model text",
    # render_usage's `as_of` comes off the in-process UsageLedger this run's own provider calls built (usage.py's `priced_as_of`, stamped by the provider that made the call) -- it is never written to session.json and never read back off disk, so nothing between the API reply and this renderer is a channel for someone else's input (#388).
    "render_usage": "the in-process usage ledger this run itself built -- never persisted, never "
                     "read back off disk, so it carries nothing another process could have forged",
    # render_turn minus its question block (#592).
    "render_turn_state": "render_understanding plus the readiness verdict -- schema slot labels and "
                          "a fixed verdict string, no model-authored prose",
}


def test_every_llm_authored_string_the_terminal_renders_is_neutralized():
    """The sweep, and the reason this file exists rather than five tests beside five renderers."""
    model = out({"problem": slot(80, "explicit", "high")})
    d = model.model_dump()
    d["questions"] = [{"q": FORGED, "slot": "problem", "why": FORGED}]
    d["summary"]["objective"] = FORGED
    # `render_dependency_map` reads the *model's* reasoning layer.
    d["decisions"] = [{"decision": FORGED, "why": FORGED, "alternative": FORGED,
                       "tradeoff": FORGED, "derived_from": ["problem"]}]
    d["challenges"] = [{"headline": FORGED, "premise": FORGED, "alternative": FORGED,
                        "consequence": FORGED, "recommendation": FORGED, "contests": ["problem"]}]
    d["opportunities"] = [{"text": FORGED, "leverage": "high", "modules": [FORGED]}]
    forged_model = EngineOutput.model_validate(d)

    brief = _brief(
        problem=FORGED, solution=FORGED, complexity_reasons=[FORGED], cost_driver=FORGED,
        risks=[FORGED], next_steps=[FORGED], open_decisions=[FORGED],
        challenges=[Challenge(headline=FORGED, premise=FORGED, alternative=FORGED,
                              consequence=FORGED, recommendation=FORGED)],
        decisions=[DesignDecision(decision=FORGED, why=FORGED, alternative=FORGED,
                                  tradeoff=FORGED)],
        opportunities=[Opportunity(text=FORGED, leverage=Leverage.high, modules=[FORGED])],
    )
    stories = Stories(stories=[{"id": FORGED, "title": FORGED, "as_a": FORGED, "i_want": FORGED,
                                "so_that": FORGED, "acceptance": [FORGED], "slots": ["problem"]}])
    estimate = EstimateDraft(
        items=[{"story_id": "S1", "title": FORGED, "complexity": "M", "days_low": 1,
                "days_high": 2, "drives": [FORGED]}],
        risks=[FORGED])

    renders = {
        "render_turn": _render(render_turn, forged_model),
        "render_brief": _render(render_brief, forged_model, brief),
        "render_stories": _render(render_stories, stories),
        "render_estimate": _render(render_estimate, estimate, ["problem"], "low"),
        "render_dependency_map": _render(render_dependency_map, forged_model),
        "render_impact": _render(render_impact, propagate(forged_model, ["problem"])),
        # Both arms print a decision: the forged one rests on `problem`.
        "render_evidence": _render(render_evidence, thinner_evidence(_thinner(forged_model),
                                                                     _with_unreviewable(forged_model))),
    }
    for name, text in renders.items():
        assert not _forged_lines(text), f"{name} let LLM text start a line: {_forged_lines(text)}"
        assert _raw_controls(text) == "", f"{name} emitted a raw control character"
        # Must fire: every one of these renderers must actually have printed the payload.
        assert "FORGED AT COLUMN ZERO" in text, f"{name} rendered none of the forged fields"


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


def test_the_forged_sweep_covers_every_prose_renderer_in_the_module():
    """Derived, not enumerated (#331): the scan set used to be a fixed tuple of six imported names."""
    from requivo.render import terminal as terminal_module

    declared = {
        name for name, fn in inspect.getmembers(terminal_module, inspect.isfunction)
        if name.startswith("render_") and fn.__module__ == terminal_module.__name__
    }
    covered = _SWEPT_RENDERERS | set(_NON_PROSE_RENDERERS)
    missing = declared - covered
    assert not missing, (
        f"{sorted(missing)} exist in render/terminal.py and are covered by neither the forged sweep "
        f"nor _NON_PROSE_RENDERERS -- add a forged-args entry to `renders` in "
        f"test_every_llm_authored_string_the_terminal_renders_is_neutralized, or name it in "
        f"_NON_PROSE_RENDERERS with a reason if it renders no model-authored text"
    )
    # Must-fire control for the control: a name this module never declares must not silently pass.
    assert "render_something_that_does_not_exist" not in declared


def test_ordinary_prose_renders_byte_for_byte_unchanged():
    """The control, and the half a security fix ships without."""
    text = _render(render_turn, _model_with_question("How are approvals routed today?"))
    assert "1. How are approvals routed today?" in text
    assert "\\" not in text

    brief_text = _render(render_brief, out({"problem": slot(80, "explicit", "high")}), _brief())
    for expected in ("A problem.", "A solution.", "A reason.", "A driver.", "A risk.", "A step."):
        assert expected in brief_text
    assert "\\" not in brief_text


# -- #331: a static sweep whose *scan set* is a file tree, not a list of modules ---------------------
# `test_every_llm_authored_string_the_terminal_renders_is_neutralized` proved the assertions are real by covering every renderer it knows about; it could not prove anything about a call site outside `render/terminal.py`, because it never looked.
#
# What follows is a static AST scan, the same technique `tests/test_source_form.py` already uses for the core/provider boundary, pointed at a narrower and more tractable question: does any code under `src/requivo/` (excluding `core/`, `providers/` and `services/`, which never touch a terminal) *read* a `Question`'s `q` or `why` field -- the two fields #330 forged -- other than as the direct argument of `display_text`/`display_token`?
#
# The scan checks every read of the field, not only a read that sits directly inside a `print()`/`input()` call.
#
# This derives its *file* coverage rather than enumerating modules.
#
# What this cannot see, stated rather than assumed clean.


def _parse_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _question_bound_names(tree: ast.Module) -> set:
    """Every local name a `for` loop binds to one element of `<something>.questions`."""
    names: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        if (isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
                and it.func.id == "enumerate" and it.args):
            it = it.args[0]
        if not (isinstance(it, ast.Attribute) and it.attr == "questions"):
            continue
        target = node.target
        name_node = target.elts[-1] if isinstance(target, ast.Tuple) and target.elts else target
        if isinstance(name_node, ast.Name):
            names.add(name_node.id)
    return names


def _question_prose_leaks_in_file(path: Path) -> list:
    """Every *read* of a Question-bound name's `.q` or `.why` attribute in `path` that is not the direct
    argument of `display_text`/`display_token` (#330)."""
    tree = _parse_module(path)
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    question_vars = _question_bound_names(tree)
    if not question_vars:
        return []

    violations: list = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr in ("q", "why")
                and isinstance(node.value, ast.Name) and node.value.id in question_vars):
            continue
        parent = parents.get(id(node))
        wrapped = (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                   and parent.func.id in ("display_text", "display_token"))
        if not wrapped:
            violations.append(
                f"{path}:{node.lineno}: unescaped {node.value.id}.{node.attr} (a Question field) "
                f"-- not the direct argument of display_text/display_token"
            )
    return violations


def _question_prose_leaks(root: Path) -> list:
    """`_question_prose_leaks_in_file`, over every `.py` file under `root` (#10)."""
    if not root.is_dir():
        raise AssertionError(f"scan could not read {root}: no such directory")
    found = sorted(root.rglob("*.py"))
    if not found:
        raise AssertionError(f"scan of {root} found no Python files -- an empty scan proves nothing")
    violations: list = []
    for path in found:
        violations += _question_prose_leaks_in_file(path)
    return violations


SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "requivo"
_TERMINAL_SURFACE_PACKAGES = ("render", "deterministic", "web")


def _terminal_surface_entries() -> tuple[str, ...]:
    """The scan set: three packages, plus every top-level module, *derived* rather than listed (#550)."""
    modules = sorted(p.name for p in SRC_ROOT.glob("*.py"))
    if not modules:
        raise AssertionError(f"scan of {SRC_ROOT} found no top-level modules -- 'could not look'")
    return _TERMINAL_SURFACE_PACKAGES + tuple(modules)


def test_no_question_field_reaches_a_terminal_call_unescaped_anywhere_in_the_surface_tree():
    """The real scan, over the real tree."""
    violations: list = []
    for entry in _terminal_surface_entries():
        target = SRC_ROOT / entry
        # A top-level module is a single file, not a directory.
        violations += (_question_prose_leaks(target) if target.is_dir()
                       else _question_prose_leaks_in_file(target))
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "tui_body, expect_violation",
    [
        pytest.param(
            'def show(out):\n'
            '    for i, q in enumerate(out.questions, 1):\n'
            '        print(f"{i}. {q.q}")\n',
            True,
            id="direct-raw-read-must-fire",
        ),
        pytest.param(
            'from requivo.core.selectors import display_text\n\n\n'
            'def show(out):\n'
            '    for i, q in enumerate(out.questions, 1):\n'
            '        safe_q = display_text(q.q)\n'
            '        print(f"{i}. {safe_q}")\n',
            False,
            id="escaped-through-display-text-must-not-fire",
        ),
        pytest.param(
            'def show(out):\n'
            '    for i, q in enumerate(out.questions, 1):\n'
            '        msg = q.q\n'
            '        print(f"{i}. {msg}")\n',
            True,
            id="local-variable-indirection-must-fire",
        ),
    ],
)
def test_the_question_scan_tells_a_raw_read_from_an_escaped_one(tmp_path, tui_body, expect_violation):
    """Three defining shapes of the scan, by id: a direct raw `q.q` read must-fire."""
    pkg = tmp_path / "requivo"
    pkg.mkdir()
    (pkg / "tui.py").write_text(tui_body, encoding="utf-8")
    violations = _question_prose_leaks(pkg)
    if expect_violation:
        assert len(violations) == 1
        assert "tui.py" in violations[0]
        assert ".q" in violations[0]
    else:
        assert violations == []


def test_the_question_scan_refuses_an_empty_or_missing_root(tmp_path):
    """The #10 discipline: `Path.rglob` on a directory that does not exist returns `[]` and raises nothing."""
    import pytest

    with pytest.raises(AssertionError, match="no such directory"):
        _question_prose_leaks(tmp_path / "does-not-exist")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AssertionError, match="no Python files"):
        _question_prose_leaks(empty)


# -- #330: the interactive loop's own `input()` prompt, not a renderer ----------------------------
# `render_turn` neutralizes `q.q` (pinned above), and `cli.py:205`.


def test_a_forged_question_cannot_write_a_line_at_column_zero_of_the_input_prompt():
    """The reproduction: a forged `Question.q` reaches `input()`'s prompt string through the real `converse()`
    path, not through `render_turn`."""
    forged = EngineOutput.model_validate({
        "model": full_slots(problem=slot(80, "explicit", "high")),
        "questions": [{"q": FORGED, "slot": "problem", "why": "because"}],
        "summary": {"objective": "o"},
    })
    converged = out({"problem": slot(80, "explicit", "high")})
    disco = DiscoveryService(_StubProvider(forged, converged))

    prompts = _drive_converse(disco, "a request")

    prompt = prompts[0]
    forged_lines = [ln for ln in prompt.splitlines() if ln.startswith("FORGED")]
    assert not forged_lines, prompt
    assert _raw_controls(prompt) == "", prompt
    # Must fire: neutralized means escaped and still readable, never dropped.
    assert "FORGED AT COLUMN ZERO" in prompt
    assert "\\x1b[2J" in prompt


def test_an_ordinary_question_still_reads_at_the_input_prompt():
    """Must-fire control for the test above, in the same fixture."""
    plain = EngineOutput.model_validate({
        "model": full_slots(problem=slot(80, "explicit", "high")),
        "questions": [{"q": "How are approvals routed today?", "slot": "problem", "why": "because"}],
        "summary": {"objective": "o"},
    })
    converged = out({"problem": slot(80, "explicit", "high")})
    disco = DiscoveryService(_StubProvider(plain, converged))

    prompts = _drive_converse(disco, "a request")

    assert "[1/1] How are approvals routed today?" in prompts[0]
    assert "\\" not in prompts[0]


def test_a_forged_question_cannot_break_the_answer_folded_back_to_the_provider():
    """`cli.py:210` folds `q.q` into the `[slot: ...] Q: ... -> A: ...` string sent back as the next turn's
    `answers` -- the same field, lower value."""
    forged = EngineOutput.model_validate({
        "model": full_slots(problem=slot(80, "explicit", "high")),
        "questions": [{"q": FORGED, "slot": "problem", "why": "because"}],
        "summary": {"objective": "o"},
    })
    converged = out({"problem": slot(80, "explicit", "high")})
    provider = _StubProvider(forged, converged)
    disco = DiscoveryService(provider)

    _drive_converse(disco, "a request")

    assert len(provider.analyze_calls) == 2, "the loop did not make a refinement turn to inspect"
    folded = provider.analyze_calls[1]["answers"]
    forged_lines = [ln for ln in folded.splitlines() if ln.startswith("FORGED")]
    assert not forged_lines, folded
    assert _raw_controls(folded) == "", folded
    assert "FORGED AT COLUMN ZERO" in folded
    assert folded.startswith("[slot: problem] Q: "), (
        "the folded structure itself must survive -- a dropped prefix would also satisfy the "
        "assertions above"
    )


# -- #593: the grounding judgment's own prose, and the fourth state it must not collapse ----------


def test_a_forged_grounding_reason_cannot_write_a_line_of_the_judgment_readout():
    """`ContextJudgment.reason` is LLM-authored prose over an untrusted request and is printed verbatim."""
    from requivo.core.contracts import ContextJudgment
    from requivo.render.terminal import render_context_judgment
    from requivo.services.discovery import Grounding

    forged = Grounding(ContextJudgment(decision="uncovered", reason=FORGED), "")
    text = _render(render_context_judgment, forged)

    assert not _forged_lines(text), text
    assert _raw_controls(text) == ""
    # Must fire: neutralized means escaped and still readable, never dropped.
    assert "FORGED" in text and "ZERO" in text, "the reason was dropped rather than neutralized"
    assert "\\n" in text, "the embedded newline was removed instead of being made visible"


def test_the_four_grounding_outcomes_read_as_four_different_answers():
    """The control, and the reason the renderer exists at all (#492)."""
    from requivo.core.contracts import ContextJudgment
    from requivo.render.terminal import render_context_judgment
    from requivo.services.discovery import Grounding

    texts = {
        "not asked": _render(render_context_judgment, Grounding(None, "this provider cannot")),
        "none": _render(render_context_judgment,
                        Grounding(ContextJudgment(decision="none", reason="ordinary software"), "")),
        "installed": _render(render_context_judgment,
                             Grounding(ContextJudgment(decision="installed", reason="finance",
                                                       cards=["financial-reporting"]), "")),
        "uncovered": _render(render_context_judgment,
                             Grounding(ContextJudgment(decision="uncovered", reason="dentistry"), "")),
    }
    assert len(set(texts.values())) == 4, texts
    assert "not checked" in texts["not asked"]
    assert "financial-reporting" in texts["installed"]
    assert "⚠" in texts["uncovered"], "the one outcome a reader must act on reads like the others"
    assert "⚠" not in texts["none"]
