"""Every prompt's `# Output format` example must validate against the Pydantic contract its operation parses replies with, checked offline (`contract_for` derives the contract from the generator's own source, never a hand-synced table). Contracts are `extra="forbid"` (invariant 4): a drift makes `_complete()`
retry twice then raise `EngineError` -- up to 3x the call cost, invisible elsewhere in the suite (#266). `scan_prompts()` treats an empty or missing prompt root as an error, not a silent all-clear (#10). Not checked: an optional field the example omits, prose outside the fence, empty-list contracts, or
semantic quality (golden harness's job)."""
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
from requivo.paths import PROMPTS
from requivo.providers.anthropic import generators
from requivo.providers.anthropic.generators import _GENERATORS, _OP_PROMPTS

# analyze is the discovery turn, not a generator -- no _GENERATORS entry; reached via run(), a stated exception.
ANALYZE_OP = "analyze"
ANALYZE_ENTRY = generators.run

# Anchors whose absence means the scan isn't looking at real prompt assets (a moved/renamed tree).
PROMPT_ANCHORS = ("engine.md", "brief.md")

# Token a prompt writes for a real value, substituted before validation; checked for deadness below.
_PLACEHOLDERS = ("<slot_id>",)

_HEADING = re.compile(r"^# Output format[ \t]*$", re.M)
_FENCE = re.compile(r"^```json[^\n]*\n(.*?)^```", re.S | re.M)

def scan_prompts() -> list[Path]:
    """Every prompt asset, sorted. An empty result is an error rather than an answer (#10)."""
    if not PROMPTS.is_dir():
        raise AssertionError(
            f"the prompt-contract guard could not scan {PROMPTS}: no such directory. This is 'could not look', not " f"'looked and found nothing' -- fix the path, never the assertion."
        )
    found = sorted(PROMPTS.glob("*.md"))
    if not found:
        raise AssertionError(
            f"the prompt-contract guard scanned {PROMPTS} and found no prompt assets. An empty scan set cannot support " f"a 'no drift' verdict."
        )
    return found

def generator_for(op: str):
    """The function that issues `op`'s one provider call."""
    return ANALYZE_ENTRY if op == ANALYZE_OP else _GENERATORS[op]

def contract_for(op: str) -> type[BaseModel]:
    """`op`'s contract, read from the generator's own source -- never a hand-synced table (drift risk)."""
    fn = generator_for(op)
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_complete"
    ]
    if len(calls) != 1:
        raise AssertionError(
            f"{op}: expected exactly one `_complete(...)` call in {getattr(fn, '__name__', fn)}(), found {len(calls)}. " f"The contract cannot be derived, which is 'could not look' -- fix the derivation or the generator, never " f"this assertion."
        )
    args = calls[0].args
    if len(args) < 4 or not isinstance(args[3], ast.Name):
        raise AssertionError(
            f"{op}: {getattr(fn, '__name__', fn)}() does not pass its contract as the fourth positional argument to " f"`_complete(...)`, so this guard cannot tell which contract the reply is parsed with."
        )
    contract = getattr(generators, args[3].id, None)
    if not (isinstance(contract, type) and issubclass(contract, BaseModel)):
        raise AssertionError(
            f"{op}: {getattr(fn, '__name__', fn)}() passes {args[3].id!r} to `_complete(...)`, which is not a Pydantic " f"contract reachable from the generators module."
        )
    return contract

def output_format_example(op: str, text: str) -> str:
    """Three distinct refusals, not one -- each names a different repair; the must-fire control #266 asks for."""
    heading = _HEADING.search(text)
    if heading is None:
        raise AssertionError(
            f"{_OP_PROMPTS[op]} has no `# Output format` section, so {op}'s reply shape is documented nowhere this " f"guard can read. A prompt that stops carrying an example stops being checked against its contract, which " f"is the drift #266 is about."
        )
    fence = _FENCE.search(text[heading.end():])
    if fence is None:
        raise AssertionError(
            f"{_OP_PROMPTS[op]} has an `# Output format` section with no ```json fence under it. The example is what " f"this guard validates; prose alone cannot be parsed."
        )
    return fence.group(1)

def substitute_placeholders(value: Any, slot_id: str) -> Any:
    """Replace every placeholder token in keys and string values, recursively."""
    if isinstance(value, dict):
        return {
            substitute_placeholders(k, slot_id): substitute_placeholders(v, slot_id)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [substitute_placeholders(v, slot_id) for v in value]
    if isinstance(value, str):
        for token in _PLACEHOLDERS:
            value = value.replace(token, slot_id)
        return value
    return value

def sample_slot_id() -> str:
    """A real, required slot id -- deterministic so a failure message is reproducible."""
    _, required = schema_slot_ids()
    return sorted(required)[0]

def example_for(op: str, *, normalised: bool = True) -> Any:
    """`op`'s Output-format example, parsed, with placeholders resolved unless asked otherwise."""
    text = (PROMPTS / _OP_PROMPTS[op]).read_text(encoding="utf-8")
    raw = output_format_example(op, text)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{_OP_PROMPTS[op]}'s Output-format example is not valid JSON ({exc}). The prompt tells the model to reply " f"with 'only a valid JSON object'; the example has to be one."
        ) from exc
    return substitute_placeholders(parsed, sample_slot_id()) if normalised else parsed

# --------------------------------------------------------------------------------------------------
# The scan set itself: "could not look" must never render as "looked and found nothing".
# --------------------------------------------------------------------------------------------------

def test_the_guard_scans_the_real_prompt_assets():
    """Name what was scanned. Everything below this line rests on the assets actually being here."""
    names = sorted(p.name for p in scan_prompts())
    missing = [anchor for anchor in PROMPT_ANCHORS if anchor not in names]
    assert not missing, (
        f"the prompt-contract guard scanned {PROMPTS} and did not find {missing}; it is not looking at Requivo's " f"prompt assets. Scanned: {names}"
    )

def test_the_guard_refuses_a_scan_it_could_not_make(monkeypatch, tmp_path):
    """Positive control for #10: an absent directory must error, not read as clean -- `glob` returns [] there."""
    renamed_away = tmp_path / "prompts"
    assert list(renamed_away.glob("*.md")) == [], "the shape being guarded against: glob returns [], not an error"
    monkeypatch.setattr(f"{__name__}.PROMPTS", renamed_away)
    with pytest.raises(AssertionError, match="no such directory"):
        scan_prompts()

def test_the_guard_refuses_an_empty_prompt_directory(monkeypatch, tmp_path):
    """The other shape of the same hole: the directory resolves, and holds nothing."""
    empty = tmp_path / "prompts"
    empty.mkdir()
    monkeypatch.setattr(f"{__name__}.PROMPTS", empty)
    with pytest.raises(AssertionError, match="no prompt assets"):
        scan_prompts()

def test_every_reachable_operation_is_covered_by_this_guard():
    """`_OP_PROMPTS` and `_GENERATORS`+analyze must name the same op set, or one side goes unscanned."""
    assert set(_OP_PROMPTS) == {ANALYZE_OP} | set(_GENERATORS), (
        f"_OP_PROMPTS covers {sorted(_OP_PROMPTS)} while the generators (plus {ANALYZE_OP!r}) cover " f"{sorted({ANALYZE_OP} | set(_GENERATORS))}. An operation in only one of them is unscanned."
    )

def test_every_prompt_asset_belongs_to_an_operation():
    """The scan set and `_OP_PROMPTS` must account for each other: a file is dead weight, or a call fails at runtime."""
    on_disk = {p.name for p in scan_prompts()}
    registered = set(_OP_PROMPTS.values())
    assert on_disk == registered, (
        f"prompt assets on disk {sorted(on_disk)} and prompts named by _OP_PROMPTS {sorted(registered)} disagree; " f"unclaimed: {sorted(on_disk - registered)}, missing: {sorted(registered - on_disk)}"
    )

def test_every_declared_placeholder_still_appears_in_a_prompt():
    """A placeholder no prompt writes is dead weight nobody can tell is live."""
    text = "".join(p.read_text(encoding="utf-8") for p in scan_prompts())
    dead = [token for token in _PLACEHOLDERS if token not in text]
    assert not dead, (
        f"these placeholders are declared here and written by no prompt: {dead}. Either a prompt stopped using one " f"(drop it) or it was renamed (update it) -- do not leave it."
    )

# --------------------------------------------------------------------------------------------------
# The guard itself: every example, against the contract its own generator parses with.
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(_OP_PROMPTS))
def test_the_output_format_example_validates_against_its_contract(op):
    """The whole point: a drift here costs three paid calls and an `EngineError`, invisible elsewhere in the suite."""
    contract = contract_for(op)
    try:
        contract.model_validate(example_for(op))
    except ValidationError as exc:
        raise AssertionError(
            f"{_OP_PROMPTS[op]}'s Output-format example is refused by {contract.__name__}, the contract {op!r} replies " f"are parsed with. A model obeying this example produces a reply `_complete()` retries twice and then "
            f"fails on -- three paid calls per invocation. Fix whichever of the two moved; a prompt edit also owes a " f"golden-harness capture.\n{exc}"
        ) from exc

def test_every_operation_resolves_to_a_contract_an_llm_may_fill():
    """Invariant 4: an LLM-filled contract is a StrictModel; extra="forbid" turns a drift into a refusal, not a trim."""
    resolved = {op: contract_for(op) for op in sorted(_OP_PROMPTS)}
    permissive = sorted(op for op, contract in resolved.items() if not issubclass(contract, StrictModel))
    assert not permissive, (
        f"these operations parse replies with a contract that is not a StrictModel: {permissive}. Without extra=forbid " f"a key the prompt no longer asks for is dropped rather than refused, so validating an example against it " f"proves much less than it appears to. Resolved: "
        f"{ {op: contract.__name__ for op, contract in resolved.items()} }"
    )

# --------------------------------------------------------------------------------------------------
# Positive controls. "No drift" also passes when the check could not fire.
# --------------------------------------------------------------------------------------------------

def test_a_renamed_key_in_an_example_is_refused():
    """#266's positive control: renaming `headline` in the brief example must go red, not pass silently."""
    example = example_for("brief")
    challenge = example["challenges"][0]
    challenge["header"] = challenge.pop("headline")
    with pytest.raises(ValidationError):
        contract_for("brief").model_validate(example)

def test_a_required_contract_field_the_example_never_fills_is_refused():
    """Reverse drift: a required field the prompt never mentions -- a subclass, not an edit to contracts.py."""
    extended = create_model(
        "ReleaseNotesWithNewRequiredField",
        __base__=contract_for("release"),
        audience=(str, ...),
    )
    with pytest.raises(ValidationError):
        extended.model_validate(example_for("release"))

def test_the_placeholder_substitution_is_load_bearing():
    """`<slot_id>` isn't a real slot id: the raw example must be refused, the substituted one accepted."""
    contract = contract_for(ANALYZE_OP)
    with pytest.raises(ValidationError):
        contract.model_validate(example_for(ANALYZE_OP, normalised=False))
    contract.model_validate(example_for(ANALYZE_OP))

def test_the_extractor_refuses_a_prompt_with_no_output_format_section():
    """A prompt restructured so its example moves must go red rather than stop being checked."""
    with pytest.raises(AssertionError, match="no `# Output format` section"):
        output_format_example("brief", "# Role\n\nAdvise on the model.\n")

def test_the_extractor_refuses_an_output_format_section_with_no_json_fence():
    """Prose under the heading is not an example; a different repair from a missing heading, so a separate test."""
    with pytest.raises(AssertionError, match="no ```json fence"):
        output_format_example("brief", "# Output format\n\nReply with a JSON object.\n")

def test_the_extractor_reads_the_fence_under_the_heading_not_the_first_in_the_file():
    """Anchored on the heading, not a whole-file search, so an illustrative fence can't be mistaken for the example."""
    text = (
        '# Role\n\n```json\n{"illustration": true}\n```\n\n'
        '# Output format\n\n```json\n{"title": "x"}\n```\n'
    )
    assert json.loads(output_format_example("release", text)) == {"title": "x"}

def test_the_strictness_check_fires_on_a_permissive_contract(monkeypatch):
    """Positive control: a contract that dropped `StrictModel` must fail this assertion, not pass unnoticed."""
    permissive = create_model("PermissiveBrief", __base__=BaseModel, problem=(str, ""))
    monkeypatch.setattr(generators, "Brief", permissive)
    with pytest.raises(AssertionError, match="not a StrictModel"):
        test_every_operation_resolves_to_a_contract_an_llm_may_fill()

def test_the_derivation_refuses_a_generator_it_cannot_read(monkeypatch):
    """`contract_for` must fail loudly, not guess, when a generator stops passing its contract positionally."""
    def not_a_generator(client, out):
        return None

    monkeypatch.setitem(_GENERATORS, "brief", not_a_generator)
    with pytest.raises(AssertionError, match="expected exactly one"):
        contract_for("brief")
