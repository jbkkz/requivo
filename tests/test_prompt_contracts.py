"""Every prompt's `# Output format` example validates against the contract its operation parses replies
with, derived from the generator's own `_complete(...)` call (#266): a drift costs three paid calls."""
from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError, create_model

from requivo.core.contracts import StrictModel, schema_slot_ids
from requivo.core.perimeters import GO_TO_MARKET, SOFTWARE
from requivo.paths import PERIMETERS, PROMPTS
from requivo.providers.anthropic import generators
from requivo.providers.anthropic.generators import _GENERATORS, _OP_PROMPTS, _STANDALONE_PROMPTS

# The perimeter each example is written against; an op absent here is software (#608, #609).
_OP_PERIMETER: dict[str, str] = {"gtm_plan": GO_TO_MARKET}
ANALYZE_OP = "analyze"  # the discovery turn: not a generator, reached via run()
ANALYZE_ENTRY = generators.run
PROMPT_ANCHORS = ("engine.md", "brief.md")
_PLACEHOLDERS = ("<slot_id>", "{{REQUEST}}", "{{CARDS}}")  # tokens a prompt writes for a real value
_HEADING = re.compile(r"^# Output format[ \t]*$", re.M)
_FENCE = re.compile(r"^```json[^\n]*\n(.*?)^```", re.S | re.M)


def perimeter_for(op: str) -> str:
    return _OP_PERIMETER.get(op, SOFTWARE)


def scan_prompts() -> list[Path]:
    """Every prompt asset; an empty result is an error rather than an answer (#10)."""
    if not PROMPTS.is_dir():
        raise AssertionError(f"the prompt-contract guard could not scan {PROMPTS}: no such directory -- fix the path, never the assertion.")
    found = sorted(PROMPTS.glob("*.md"))
    if not found:
        raise AssertionError(f"the prompt-contract guard scanned {PROMPTS} and found no prompt assets.")
    return found


def generator_for(op: str):
    return ANALYZE_ENTRY if op == ANALYZE_OP else _GENERATORS[op]


def contract_for(op: str) -> type[BaseModel]:
    """`op`'s contract, read from the generator's own source rather than a hand-synced table."""
    fn = generator_for(op)
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_complete"]
    name = getattr(fn, "__name__", fn)
    if len(calls) != 1:
        raise AssertionError(f"{op}: expected exactly one `_complete(...)` call in {name}(), found {len(calls)} -- the contract cannot be derived.")
    args = calls[0].args
    if len(args) < 4 or not isinstance(args[3], ast.Name):
        raise AssertionError(f"{op}: {name}() does not pass its contract as the fourth positional argument to `_complete(...)`.")
    contract = getattr(generators, args[3].id, None)
    if not (isinstance(contract, type) and issubclass(contract, BaseModel)):
        raise AssertionError(f"{op}: {name}() passes {args[3].id!r} to `_complete(...)`, which is not a Pydantic contract.")
    return contract


def output_format_example(op: str, text: str) -> str:
    """The JSON fence under `# Output format`; two distinct refusals, each naming a different repair."""
    heading = _HEADING.search(text)
    if heading is None:
        raise AssertionError(f"{_OP_PROMPTS[op]} has no `# Output format` section, so {op}'s reply shape is checked nowhere.")
    fence = _FENCE.search(text[heading.end():])
    if fence is None:
        raise AssertionError(f"{_OP_PROMPTS[op]} has an `# Output format` section with no ```json fence under it.")
    return fence.group(1)


def substitute_placeholders(value: Any, slot_id: str) -> Any:
    if isinstance(value, dict):
        return {substitute_placeholders(k, slot_id): substitute_placeholders(v, slot_id) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_placeholders(v, slot_id) for v in value]
    if isinstance(value, str):
        for token in _PLACEHOLDERS:
            value = value.replace(token, slot_id)
    return value


def sample_slot_id() -> str:
    _, required = schema_slot_ids()
    return sorted(required)[0]


def example_for(op: str, *, normalised: bool = True) -> Any:
    """`op`'s example, parsed, with placeholders resolved unless asked otherwise."""
    text = (PROMPTS / _OP_PROMPTS[op]).read_text(encoding="utf-8")
    raw = output_format_example(op, text)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{_OP_PROMPTS[op]}'s Output-format example is not valid JSON ({exc}).") from exc
    return substitute_placeholders(parsed, sample_slot_id()) if normalised else parsed

# ---- the scan set ----


def test_the_guard_scans_the_real_prompt_assets():
    names = sorted(p.name for p in scan_prompts())
    missing = [anchor for anchor in PROMPT_ANCHORS if anchor not in names]
    assert not missing, f"the prompt-contract guard scanned {PROMPTS} and did not find {missing}; scanned: {names}"


@pytest.mark.parametrize("make_dir, match", [(False, "no such directory"), (True, "no prompt assets")], ids=["missing", "empty"])
def test_the_guard_refuses_a_scan_it_could_not_make(monkeypatch, tmp_path, make_dir, match):
    """MUST-FIRE for #10: an absent or empty directory errors rather than reading as clean."""
    renamed_away = tmp_path / "prompts"
    if make_dir:
        renamed_away.mkdir()
    monkeypatch.setattr(f"{__name__}.PROMPTS", renamed_away)
    with pytest.raises(AssertionError, match=match):
        scan_prompts()


def test_every_reachable_operation_is_covered_by_this_guard():
    """`_OP_PROMPTS` and `_GENERATORS` + analyze must name the same op set, or one side goes unscanned."""
    assert set(_OP_PROMPTS) == {ANALYZE_OP} | set(_GENERATORS), (
        f"_OP_PROMPTS covers {sorted(_OP_PROMPTS)}; the generators plus {ANALYZE_OP!r} cover {sorted({ANALYZE_OP} | set(_GENERATORS))}")


def test_every_prompt_asset_belongs_to_an_operation():
    """A file on disk is claimed by an operation or a standalone prompt (#593), or it is dead weight."""
    on_disk = {p.name for p in scan_prompts()}
    registered = set(_OP_PROMPTS.values()) | set(_STANDALONE_PROMPTS.values())
    assert on_disk == registered, f"unclaimed: {sorted(on_disk - registered)}, missing: {sorted(registered - on_disk)}"


def test_every_declared_placeholder_still_appears_in_a_prompt():
    text = "".join(p.read_text(encoding="utf-8") for p in scan_prompts())
    dead = [token for token in _PLACEHOLDERS if token not in text]
    assert not dead, f"these placeholders are declared here and written by no prompt: {dead}"

# ---- the guard ----


@pytest.mark.parametrize("op", sorted(_OP_PROMPTS))
def test_the_output_format_example_validates_against_its_contract(op):
    """#266: a drift here costs three paid calls and an `EngineError`, invisible elsewhere in the suite."""
    contract = contract_for(op)
    try:
        contract.model_validate(example_for(op), context={"perimeter": perimeter_for(op)})
    except ValidationError as exc:
        raise AssertionError(
            f"{_OP_PROMPTS[op]}'s Output-format example is refused by {contract.__name__}, the contract {op!r} replies "
            f"are parsed with. Fix whichever of the two moved; a prompt edit also owes a golden-harness capture.\n{exc}") from exc


def test_every_operation_resolves_to_a_contract_an_llm_may_fill():
    """Invariant 4: an LLM-filled contract is a StrictModel, so a drift is a refusal rather than a trim."""
    resolved = {op: contract_for(op) for op in sorted(_OP_PROMPTS)}
    permissive = sorted(op for op, contract in resolved.items() if not issubclass(contract, StrictModel))
    assert not permissive, f"these operations parse replies with a contract that is not a StrictModel: {permissive}"

# ---- positive controls ----


def test_a_renamed_key_in_an_example_is_refused():
    """MUST-FIRE (#266): renaming `headline` in the brief example goes red."""
    example = example_for("brief")
    challenge = example["challenges"][0]
    challenge["header"] = challenge.pop("headline")
    with pytest.raises(ValidationError):
        contract_for("brief").model_validate(example)


def test_a_required_contract_field_the_example_never_fills_is_refused():
    extended = create_model("ReleaseNotesWithNewRequiredField", __base__=contract_for("release"), audience=(str, ...))
    with pytest.raises(ValidationError):
        extended.model_validate(example_for("release"))


def test_the_placeholder_substitution_is_load_bearing():
    contract = contract_for(ANALYZE_OP)
    with pytest.raises(ValidationError):
        contract.model_validate(example_for(ANALYZE_OP, normalised=False))
    contract.model_validate(example_for(ANALYZE_OP))


@pytest.mark.parametrize("text, match", [
    ("# Role\n\nAdvise on the model.\n", "no `# Output format` section"),
    ("# Output format\n\nReply with a JSON object.\n", "no ```json fence"),
], ids=["no-heading", "no-fence"])
def test_the_extractor_refuses_a_prompt_whose_example_it_cannot_find(text, match):
    with pytest.raises(AssertionError, match=match):
        output_format_example("brief", text)


def test_the_extractor_reads_the_fence_under_the_heading_not_the_first_in_the_file():
    text = '# Role\n\n```json\n{"illustration": true}\n```\n\n# Output format\n\n```json\n{"title": "x"}\n```\n'
    assert json.loads(output_format_example("release", text)) == {"title": "x"}


def test_the_strictness_check_fires_on_a_permissive_contract(monkeypatch):
    permissive = create_model("PermissiveBrief", __base__=BaseModel, problem=(str, ""))
    monkeypatch.setattr(generators, "Brief", permissive)
    with pytest.raises(AssertionError, match="not a StrictModel"):
        test_every_operation_resolves_to_a_contract_an_llm_may_fill()


def test_the_derivation_refuses_a_generator_it_cannot_read(monkeypatch):
    def not_a_generator(client, out):
        return None

    monkeypatch.setitem(_GENERATORS, "brief", not_a_generator)
    with pytest.raises(AssertionError, match="expected exactly one"):
        contract_for("brief")


def test_the_confidence_grading_in_the_schema_and_the_prompt_agree():
    """#611: the grading lives in the software `model_schema.json` and is restated in `engine.md`; the two must not drift."""
    schema_confidence = json.dumps(json.loads((PERIMETERS / SOFTWARE / "model_schema.json").read_text(encoding="utf-8"))["confidence"])
    engine_md = (PROMPTS / "engine.md").read_text(encoding="utf-8")
    for value in ("explicit", "inferred", "empty", "testable", "belief about the world", "test_plan"):
        assert value in schema_confidence, f"{value!r} missing from model_schema.json's confidence grading"
        assert value in engine_md, f"{value!r} missing from engine.md's restatement"
