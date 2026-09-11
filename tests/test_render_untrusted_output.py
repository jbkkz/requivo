"""LLM-authored prose cannot write a line of the terminal render path (#213).

The sibling of `tests/test_cli_untrusted_output.py`, and the gap it left. That file sweeps the
*diagnostic* verbs -- doctor, session verify, session show, artifact list, impact -- where the
untrusted string is a value read off disk. This one sweeps the **primary** render path, where the
untrusted string is the model's own reply: the questions, the challenges, the opportunities and the
brief prose a first-time user sees on `discover`, `status`, `brief`, `stories` and `estimate`.

The threat is neither hypothetical nor about a hostile model. SECURITY.md frames a *client request*
as untrusted business data the tool runs on, and the engine's whole job is turning that request into
prose. A request that steers the reply carries an embedded newline into `Question.q`, and the line
after it is a sentence Requivo appears to be saying -- a forged `Ready` verdict, a challenge hidden
behind a screen clear.

`streams.py` does not help here, and it is worth saying why, because it looks as though it should:
`errors="backslashreplace"` acts on characters the console *cannot encode*, and ESC is perfectly
encodable in UTF-8. It is emitted verbatim.

Two things are pinned. Each named field, so a regression names itself; and a **sweep** over every
LLM-authored string the terminal renders, which is what catches the field somebody adds next --
`display_text` at fifteen call sites is a discipline, and the sweep is what a discipline needs.

The control test is the other half. An escaper that made ordinary prose unreadable would be a worse
bug than the one it fixed, and it would ship green, because nobody re-reads output that looks busy.
"""
from __future__ import annotations

import ast
import builtins
import inspect
import io
import re
from contextlib import redirect_stdout
from pathlib import Path

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
from requivo.core.dependencies import propagate
from requivo.core.persistence import RevisionRecord
from requivo.render.terminal import (
    render_brief,
    render_dependency_map,
    render_estimate,
    render_grounding,
    render_impact,
    render_session_cost,
    render_stories,
    render_turn,
)
from requivo.services.discovery import DiscoveryService

# A newline, then a claim at column 0, then a screen clear. The three shapes together: the newline is
# what ends Requivo's line, the column-0 text is what reads as Requivo's own output, and the escape
# is what a terminal *executes* rather than displays.
FORGED = "Real text.\nFORGED AT COLUMN ZERO\x1b[2J"

# Every character that can move a cursor or end a line -- the same class `core/selectors.py` guards a
# selector token against, and deliberately no wider.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _render(fn, *args) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


def _forged_lines(text: str) -> list:
    return [ln for ln in text.splitlines() if ln.startswith("FORGED")]


def _raw_controls(text: str) -> str:
    """The literal control characters still present, newlines excluded -- a renderer's own line
    breaks are not the threat. The forged-line check above catches a line; this catches an escape
    sequence that never needed one. A cursor move or a colour change is invisible to a
    `splitlines()` comparison and is the half of the threat that does not announce itself."""
    return "".join(c for c in _CONTROL.findall(text) if c != "\n")


def _model_with_question(q: str) -> EngineOutput:
    d = out({"problem": slot(80, "explicit", "high")}).model_dump()
    d["questions"] = [{"q": q, "slot": "problem", "why": "because"}]
    return EngineOutput.model_validate(d)


class _StubProvider:
    """The minimal `ReasoningProvider` a `converse()` drive needs -- a scripted list of turns and
    nothing else. Self-contained rather than imported from `tests/test_cli_interactive.py`, on the
    same rule that file's own sibling states: reaching into another test module for a helper breaks
    when that module reorganises."""

    name = "stub"

    def __init__(self, *turns: EngineOutput):
        self.turns = list(turns)
        self.analyze_calls: list[dict] = []

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False):
        self.analyze_calls.append({
            "request": request, "current_model": current_model, "answers": answers, "only": only,
        })
        return self.turns.pop(0)

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        raise AssertionError("converse() never generates an artifact")

    def model_name(self) -> str:
        return "stub-model"

    def provenance(self, op, *, only=None) -> dict:
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": "sha256:0"}


def _drive_converse(disco, request, *, answer="an answer") -> list:
    """Run `converse()` for real, patching `input()` to record the exact prompt string it was
    handed -- not just to supply an answer, the way every other patch of `input()` in this repo
    does. The prompt string *is* the surface under test for #330: `cli.py:205` builds it from
    `q.q` one statement after `render_turn` neutralizes the same field."""
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
    """`render_turn` is the first thing a user ever sees -- `discover`, `answer`, `status` and the
    offline `demo` all print it -- and `print(f"  {i}. {q.q}")` put the reply straight on the line.
    The readiness verdict it can forge sits three lines above it."""
    text = _render(render_turn, _model_with_question(FORGED))
    assert not _forged_lines(text), text
    assert _raw_controls(text) == ""
    # Must fire: neutralized means escaped and still readable, never dropped. Without this the
    # assertions above are satisfied by a renderer that prints nothing at all.
    assert "FORGED AT COLUMN ZERO" in text
    assert "\\x1b[2J" in text


def test_a_challenge_cannot_forge_a_line_of_the_decision_brief():
    """The brief is the deliverable and a challenge is the part a reader acts on. All five of its
    fields are rendered, so all five are checked -- a fix covering `headline` alone would look
    complete and leave four open."""
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
    """`render_session_cost`'s `usage_priced_as_of` is not model prose -- it is a persisted
    `RevisionRecord` field, read back off `session.json` on every `requivo status`. Invariant 14
    frames `context_cards` this same way: untrusted every time it is read back, regardless of what
    wrote it, and `session import` is the documented channel through which someone else's archive
    -- and its `usage_priced_as_of` -- arrives (#388).

    Two revisions in one fixture, not one: a clean one whose date must actually appear (the
    must-fire half -- without it, the assertions below would pass against a harness that rendered
    the "no price on file" branch and never touched `usage_priced_as_of` at all), and a forged one
    whose date must not open a line of its own at column 0."""
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
    # Must fire, both halves: the clean revision's date proves the "rates as of" stamp actually
    # rendered, and the forged text still showing up (inside the neutralized token, never at
    # column 0) proves it was processed rather than silently dropped.
    assert "2026-01-01" in text
    assert "FORGED AT COLUMN ZERO" in text


def test_a_forged_context_card_name_cannot_write_a_line_of_the_grounding_readout():
    """`render_grounding`'s input is a persisted `context_cards` entry, which invariant 14 names as
    untrusted **every time it is read back**, whatever wrote it -- and #40 is the reproduced
    instance: a stored card name forged a line at column 0 of `doctor`'s own output, on the surface
    whose job is to answer whether an install is sound.

    That is why this renderer is swept rather than exempt even though it prints no model prose.
    `session import` is the documented channel through which someone else's archive, and its
    `context_cards`, arrives; the service layer resolves cards at *creation* and makes no promise
    about the value on disk (invariant 14 again, in its own words).

    Two calls, not one, and the second is the must-fire half: the unnarrowed branch reads the
    install's real card names, so it proves the renderer printed something at all -- without it the
    assertions below would pass against a branch that emitted nothing."""
    text = _render(render_grounding, [FORGED])
    assert not _forged_lines(text), text
    assert _raw_controls(text) == ""
    assert "FORGED AT COLUMN ZERO" in text, "the forged card name was dropped rather than neutralized"

    unnarrowed = _render(render_grounding, None)
    assert "Product context" in unnarrowed, "the other branch rendered nothing to be forged through"


# The renderer names the forged sweep below actually calls. A module-level constant rather than a
# local variable, so `test_the_forged_sweep_covers_every_prose_renderer_in_the_module` can compare
# against it without re-deriving the dict -- see that test for why this is checked rather than just
# trusted (#331).
_SWEPT_RENDERERS = {
    "render_turn", "render_brief", "render_stories", "render_estimate",
    "render_dependency_map", "render_impact",
    # render_session_cost is swept separately, below -- its untrusted field is a persisted
    # RevisionRecord.usage_priced_as_of, not model prose, so it does not fit the model/brief/
    # stories/estimate fixture shape the big sweep below is built from (#388).
    "render_session_cost",
    # render_grounding likewise, and for the same class one field along: its input is a persisted
    # `context_cards` entry, which is the field invariant 14 is actually written about and the one
    # #40 forged a line of `doctor`'s output through. Swept by
    # `test_a_forged_context_card_name_cannot_write_a_line_of_the_grounding_readout` (#492).
    "render_grounding",
}

# `render_*` functions in `render/terminal.py` that render no model-authored prose, named with a
# reason rather than silently absent from `_SWEPT_RENDERERS` -- the same discipline
# `_SURFACE_STORAGE_ALLOWLIST` in `tests/test_boundaries.py` already uses.
_NON_PROSE_RENDERERS = {
    "render_understanding": "labels are schema slot ids (via slot_label), not model-authored prose",
    "render_readiness": "a fixed verdict string plus schema slot id labels",
    "render_next_command": "a fixed command template plus a slug and an artifact type, no model text",
    "render_stale": "artifact filenames from ARTIFACT_FILENAMES and schema slot labels, no model text",
    # render_usage's `as_of` comes off the in-process UsageLedger this run's own provider calls
    # built (usage.py's `priced_as_of`, stamped by the provider that made the call) -- it is never
    # written to session.json and never read back off disk, so nothing between the API reply and
    # this renderer is a channel for someone else's input. That is what makes it safe and is why it
    # is exempt rather than swept, unlike its disk-sourced sibling render_session_cost, whose
    # RevisionRecord.usage_priced_as_of is exactly that channel -- session.json, read back on every
    # `requivo status`, forgeable through `session import` (#388).
    "render_usage": "the in-process usage ledger this run itself built -- never persisted, never "
                     "read back off disk, so it carries nothing another process could have forged",
}


def test_every_llm_authored_string_the_terminal_renders_is_neutralized():
    """The sweep, and the reason this file exists rather than five tests beside five renderers.

    Every LLM-authored string field carries the payload at once and every terminal renderer runs, so
    a field added later and printed raw goes red here under its own renderer's name. That is the
    only guard that survives somebody adding a sixth field to `Challenge`."""
    model = out({"problem": slot(80, "explicit", "high")})
    d = model.model_dump()
    d["questions"] = [{"q": FORGED, "slot": "problem", "why": FORGED}]
    d["summary"]["objective"] = FORGED
    # `render_dependency_map` reads the *model's* reasoning layer, not the brief's, so a forged brief
    # alone leaves that renderer with nothing to render -- which the must-fire assertion below
    # correctly refused to call a pass.
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
    }
    for name, text in renders.items():
        assert not _forged_lines(text), f"{name} let LLM text start a line: {_forged_lines(text)}"
        assert _raw_controls(text) == "", f"{name} emitted a raw control character"
        # Must fire: every one of these renderers must actually have printed the payload, or the
        # two assertions above are green on a renderer that emitted nothing.
        assert "FORGED AT COLUMN ZERO" in text, f"{name} rendered none of the forged fields"


def test_the_forged_sweep_covers_every_prose_renderer_in_the_module():
    """Derived, not enumerated (#331). The scan set the sweep above runs against used to be a fixed
    tuple of six imported names -- real for those six, and silent about a seventh. This introspects
    `render/terminal.py` itself for every `render_*` function and requires each one to be named
    either in `_SWEPT_RENDERERS` (covered by the forged sweep) or in `_NON_PROSE_RENDERERS` (exempt,
    with a reason) -- so a renderer that starts touching model text and is added to neither fails
    here, by name, before it ships unguarded the way `cli.py`'s own prompt did."""
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
    """The control, and the half a security fix ships without. An escaper that quoted every string
    would satisfy every assertion above, make the product unreadable, and ship green.

    Short strings on purpose: `textwrap.fill` wraps at 80 columns, so a long line would fail this
    for a reason with nothing to do with escaping."""
    text = _render(render_turn, _model_with_question("How are approvals routed today?"))
    assert "1. How are approvals routed today?" in text
    assert "\\" not in text

    brief_text = _render(render_brief, out({"problem": slot(80, "explicit", "high")}), _brief())
    for expected in ("A problem.", "A solution.", "A reason.", "A driver.", "A risk.", "A step."):
        assert expected in brief_text
    assert "\\" not in brief_text


# -- #331: a static sweep whose *scan set* is a file tree, not a list of modules ---------------------
# `test_every_llm_authored_string_the_terminal_renders_is_neutralized` proved the assertions are real
# by covering every renderer it knows about; it could not prove anything about a call site outside
# `render/terminal.py`, because it never looked. `cli.py`'s own `input()` prompt was exactly that --
# same field (`Question.q`), same threat, a different module, invisible to a sweep keyed by import
# name.
#
# What follows is a static AST scan, the same technique `tests/test_boundaries.py` already uses for
# the core/provider boundary, pointed at a narrower and more tractable question: does any code under
# `src/requivo/` (excluding `core/`, `providers/` and `services/`, which never touch a terminal) *read*
# a `Question`'s `q` or `why` field -- the two fields #330 forged -- other than as the direct argument
# of `display_text`/`display_token`?
#
# The scan checks every read of the field, not only a read that sits directly inside a
# `print()`/`input()` call. An earlier version was scoped to the call site, and review found the gap:
# `msg = q.q` followed by `print(f"{msg}")` several lines later puts the raw attribute access in the
# *assignment*, outside the print call's own AST subtree, so a call-site-gated scan cannot see it
# however far `msg` travels afterward -- and that is exactly the shape a contributor gets by copying
# the fix's own `safe_q = display_text(q.q)` idiom and forgetting the escaping call inside it. Checking
# every read matches what `render/terminal.py`'s own docstring already says about this field: escape
# at the point of read, because there is no chokepoint that can catch an f-string written downstream.
#
# This derives its *file* coverage rather than enumerating modules: a brand-new surface file that
# iterates `<engine_output>.questions` and reads `q.q` raw is caught on the day it is written, with no
# scan-set list to remember to extend. What is still named explicitly is the *vocabulary* -- the two
# prose fields a `Question` actually carries untrusted text in -- because that is fixed by the
# contract in `core/contracts.py`, not by which module happens to render it next.
#
# What this cannot see, stated rather than assumed clean: a `Question` reached through anything other
# than a `for ... in <expr>.questions` loop (`question = out.questions[0]; print(question.q)` would
# not be recognised as a Question-bound name), a value escaped through something other than
# `display_text`/`display_token` by name, and a threat shaped like this one but on a different
# contract (`Challenge`, `DesignDecision`, ...) reached from outside `render/terminal.py` -- the forged
# sweep above is still the guard for those, inside the one module they are currently rendered from.


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
    """Every *read* of a Question-bound name's `.q` or `.why` attribute in `path` that is not the
    direct argument of `display_text`/`display_token`.

    Deliberately not scoped to "inside a `print()`/`input()` call" -- an earlier version was, and a
    reviewer found the gap it leaves: `msg = q.q; print(f"{msg}")` puts the raw attribute access in
    the *assignment*, not inside the print call's own AST subtree, so a scan gated on the call site
    never sees it, however far `msg` travels afterward. Checking every read of the field, regardless
    of where it sits, closes that -- and matches the discipline `render/terminal.py`'s own docstring
    already states for this field: escape at the point of read, because there is no chokepoint that
    can catch an f-string written downstream of it. The `safe_q = display_text(q.q)` idiom the #330
    fix itself uses still passes, because the raw attribute access in that line *is* the direct
    argument of `display_text`."""
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
    """`_question_prose_leaks_in_file`, over every `.py` file under `root`.

    `root` is expected to be a package directory (an empty or missing one is refused, never read as
    'no offenders' -- the same #10 discipline `tests/test_boundaries.py`'s own `scan()` enforces)."""
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
_TERMINAL_SURFACE_TREES = ("render", "cli.py", "deterministic", "web")


def test_no_question_field_reaches_a_terminal_call_unescaped_anywhere_in_the_surface_tree():
    """The real scan, over the real tree. `render/`, `cli.py`, `deterministic/` and `web/` are every
    `src/requivo/` subtree that can touch a terminal (`core/`, `providers/` and `services/` are
    excluded because they are guarded elsewhere never to print or prompt -- `tests/test_boundaries.py`
    for `core/`; `providers/` and `services/` call neither, checked at the time this test was written).
    Passing this does not prove there is no leak anywhere -- see the file-level docstring above for
    what the scan cannot see -- it proves there is none of *this* shape, in *this* tree, today."""
    violations: list = []
    for entry in _TERMINAL_SURFACE_TREES:
        target = SRC_ROOT / entry
        # `cli.py` is a single file, not a directory -- `_question_prose_leaks_in_file` works on
        # either, so both branches of `_TERMINAL_SURFACE_TREES` reach the same one function.
        violations += (_question_prose_leaks(target) if target.is_dir()
                       else _question_prose_leaks_in_file(target))
    assert not violations, "\n".join(violations)


def test_a_new_surface_module_leaking_a_question_field_is_caught_without_being_named(tmp_path):
    """Must-fire: the point of the scan. A brand-new file the guard above has never heard of, in a
    module this test invents on the spot, printing `q.q` raw. If this test could pass against the
    un-fixed code, the scan would not be a scan."""
    pkg = tmp_path / "requivo"
    pkg.mkdir()
    (pkg / "tui.py").write_text(
        """def show(out):
    for i, q in enumerate(out.questions, 1):
        print(f"{i}. {q.q}")
""",
        encoding="utf-8",
    )
    violations = _question_prose_leaks(pkg)
    assert len(violations) == 1
    assert "tui.py" in violations[0]
    assert ".q" in violations[0]


def test_the_same_new_module_escaped_through_a_local_variable_does_not_fire(tmp_path):
    """Must-not-fire, in the same fixture as the test above. The fix this scan exists to require
    (`safe_q = display_text(q.q)`, then use `safe_q`) must not itself trip the guard -- a scan that
    flagged the fix would be deleted by the next person to touch this file."""
    pkg = tmp_path / "requivo"
    pkg.mkdir()
    (pkg / "tui.py").write_text(
        """from requivo.core.selectors import display_text


def show(out):
    for i, q in enumerate(out.questions, 1):
        safe_q = display_text(q.q)
        print(f"{i}. {safe_q}")
""",
        encoding="utf-8",
    )
    assert _question_prose_leaks(pkg) == []


def test_a_local_variable_indirection_that_never_calls_display_text_is_still_caught(tmp_path):
    """Must-fire, and the reason the scan checks every *read* of the field rather than only what
    sits inside a `print()`/`input()` call (review finding on the first version of this scan): a
    field assigned to a plain local first, `msg = q.q`, then printed several lines and several
    statements later, `print(f"{i}. {msg}")`. The raw attribute access is in the *assignment*, not
    in the print call's own AST subtree -- a scan gated on the call site cannot see it, however far
    `msg` travels afterward, and this is exactly the shape a contributor gets by copying the fix's
    own `safe_q = ...` idiom and forgetting the `display_text()` call inside it."""
    pkg = tmp_path / "requivo"
    pkg.mkdir()
    (pkg / "tui.py").write_text(
        """def show(out):
    for i, q in enumerate(out.questions, 1):
        msg = q.q
        print(f"{i}. {msg}")
""",
        encoding="utf-8",
    )
    violations = _question_prose_leaks(pkg)
    assert len(violations) == 1
    assert "tui.py" in violations[0]
    assert ".q" in violations[0]


def test_the_question_scan_refuses_an_empty_or_missing_root(tmp_path):
    """The #10 discipline: `Path.rglob` on a directory that does not exist returns `[]` and raises
    nothing, so a scan that read that as 'no offenders' would pass green while checking nothing."""
    import pytest

    with pytest.raises(AssertionError, match="no such directory"):
        _question_prose_leaks(tmp_path / "does-not-exist")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AssertionError, match="no Python files"):
        _question_prose_leaks(empty)


# -- #330: the interactive loop's own `input()` prompt, not a renderer ----------------------------
# `render_turn` neutralizes `q.q` (pinned above), and `cli.py:205` -- one statement later, in the
# same function that just called it -- printed the same field raw into `input()`'s prompt. Driven
# through `converse()` over a stub provider and the real `DiscoveryService`, the way `requivo
# discover` reaches it, because a defect in a call site is not established by a renderer test.


def test_a_forged_question_cannot_write_a_line_at_column_zero_of_the_input_prompt():
    """The reproduction: a forged `Question.q` reaches `input()`'s prompt string through the real
    `converse()` path, not through `render_turn`. Before the fix this fails -- the prompt contains a
    literal newline, "FORGED AT COLUMN ZERO" sits at the start of its own line, and the raw ESC byte
    is still in the string `input()` would have written to the terminal."""
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
    """Must-fire control for the test above, in the same fixture. Escaping every prompt into
    unreadability would satisfy the assertions above and make the product unusable -- the same
    failure mode `test_ordinary_prose_renders_byte_for_byte_unchanged` guards for the renderers."""
    plain = EngineOutput.model_validate({
        "model": full_slots(problem=slot(80, "explicit", "high")),
        "questions": [{"q": "How are approvals routed today?", "slot": "problem", "why": "because"}],
        "summary": {"objective": "o"},
    })
    converged = out({"problem": slot(80, "explicit", "high")})
    disco = DiscoveryService(_StubProvider(plain, converged))

    prompts = _drive_converse(disco, "a request")

    assert "1. How are approvals routed today?" in prompts[0]
    assert "\\" not in prompts[0]


def test_a_forged_question_cannot_break_the_answer_folded_back_to_the_provider():
    """`cli.py:210` folds `q.q` into the `[slot: ...] Q: ... -> A: ...` string sent back as the next
    turn's `answers` -- the same field, lower value. An embedded newline there breaks that structure
    for the provider reading it back on the refinement turn, even though nothing is displayed."""
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
