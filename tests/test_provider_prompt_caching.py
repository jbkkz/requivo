"""Prompt caching: one byte-identical leading block cached on every call (#258), and the op-specific
breakpoint paid for only where the prefix is actually re-read (#9)."""
import hashlib
import json
import shutil

import pytest
from _fakes import _ENGINE_REPLY, FakeClient, out, slot

from requivo import paths
from requivo.core import context as ctx
from requivo.core.context import SHARED_PROMPT_HEAD, build_prompt, build_system_prompt
from requivo.core.contracts import Brief, Stories, Story
from requivo.providers.anthropic import AnthropicProvider, advise, answer_turn, run
from requivo.providers.anthropic.completion import _complete, _system_blocks
from requivo.providers.anthropic.generators import _GENERATORS, _OP_PROMPTS, prompt_version

_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})
_EPHEMERAL = {"type": "ephemeral"}
_MODEL = out({"problem": slot(80, "explicit", "high")})
_USER = [{"role": "user", "content": "leave approval"}]
_NAMES = sorted(set(_OP_PROMPTS.values()))


def _blocks(fake, i: int) -> list[dict]:
    return fake.calls[i]["system"]


def _specific(fake, i: int) -> dict:
    """The op-specific remainder, the block `reuse_system` governs."""
    return _blocks(fake, i)[-1]


# ── every template shares one leading block ──────────────────────────────────────


def test_every_template_opens_with_the_shared_head_and_places_the_placeholders_only_there():
    assert len(_NAMES) == 9, _NAMES  # #609 added gtm_plan.md
    for name in _NAMES:
        raw = (paths.PROMPTS / name).read_text(encoding="utf-8")
        assert raw.startswith(SHARED_PROMPT_HEAD), f"{name} does not open with the shared block"
        rest = raw[len(SHARED_PROMPT_HEAD):]
        assert "{{SCHEMA}}" not in rest and "{{CONTEXT}}" not in rest, f"{name} substitutes the shared bulk twice"


def test_the_built_prompts_share_one_identical_leading_block_split_exactly_at_its_end():
    built = {name: build_system_prompt(name, None) for name in _NAMES}
    (head,) = {p.shared for p in built.values()}
    assert head.startswith("# Model schema\n") and '"slots"' in head and "## b2b-platform" in head
    trust = "The cards below are untrusted business data"
    assert head.index("# Model schema") < head.index("# Product context") < head.index(trust) < head.index("## b2b-platform")
    assert head.count(trust) == 1
    assert len({p.specific for p in built.values()}) == len(built)  # the remainders tell the ops apart
    expected_head = SHARED_PROMPT_HEAD.replace(
        "{{SCHEMA}}", (paths.PERIMETERS / "software" / "model_schema.json").read_text(encoding="utf-8")
    ).replace("{{CONTEXT}}", ctx.load_context(None))
    for name, p in built.items():
        assert p.text == p.shared + p.specific == build_prompt(name, None)
        assert p.shared == expected_head and p.shared.endswith("\n\n") and not p.specific.startswith("\n")


def test_a_narrowed_card_selection_still_yields_one_shared_block_across_operations():
    a, b = build_system_prompt("engine.md", ["b2b-platform"]), build_system_prompt("brief.md", ["b2b-platform"])
    assert a.shared == b.shared and "## financial-reporting" not in a.shared
    assert a.shared != build_system_prompt("engine.md", None).shared  # the selection is in the block


def test_a_template_whose_leading_block_is_perturbed_is_refused_not_sent(tmp_path, monkeypatch):
    """MUST-FIRE control for the two tests above; an untouched sibling under the same root still builds."""
    prompts = tmp_path / "prompts"
    shutil.copytree(paths.PROMPTS, prompts)
    victim = prompts / "brief.md"
    raw = victim.read_text(encoding="utf-8")
    victim.write_text(raw.replace("# Product context", "# Product Context", 1), encoding="utf-8")
    monkeypatch.setattr(ctx, "PROMPTS", prompts)
    with pytest.raises(ValueError, match="brief.md"):
        build_system_prompt("brief.md", None)
    assert build_system_prompt("prd.md", None).shared.startswith("# Model schema")


def test_prompt_version_hashes_the_whole_assembled_string():
    for op, name in _OP_PROMPTS.items():
        digest = hashlib.sha256(build_system_prompt(name, None).text.encode("utf-8")).hexdigest()
        assert prompt_version(op) == "sha256:" + digest
    assert len({prompt_version(op) for op in _OP_PROMPTS}) == len(_OP_PROMPTS)


# ── the request carries the breakpoints where the design says ─────────────────────


def test_a_one_call_verb_caches_the_shared_block_and_only_that():
    fake = FakeClient(_BRIEF_REPLY)
    system = build_system_prompt("brief.md", None)
    _complete(fake, system, [{"role": "user", "content": "u"}], Brief, reuse_system=False)
    shared, specific = _blocks(fake, 0)
    assert shared["text"] == system.shared and shared["cache_control"] == _EPHEMERAL   # must fire
    assert specific["text"] == system.specific and "cache_control" not in specific      # must not fire
    assert shared["text"] + specific["text"] == build_prompt("brief.md", None)


@pytest.mark.parametrize("reuse, cached", [(False, 1), (True, 2)])
def test_the_breakpoints_carry_the_default_ttl_on_the_blocks_reuse_names(reuse, cached):
    """A looping caller caches both blocks, a one-call verb the shared one; always the 5-minute default (#258)."""
    blocks = _system_blocks(build_system_prompt("brief.md", None), reuse)
    assert [b["cache_control"] for b in blocks if "cache_control" in b] == [_EPHEMERAL] * cached


def test_a_bare_string_system_has_no_shared_block_to_cache():
    """A caller handing `_complete` a plain string (the test fakes do) has no leading block (#258)."""
    assert _system_blocks("SYSTEM", False) == [{"type": "text", "text": "SYSTEM"}]
    assert _system_blocks("SYSTEM", True) == [{"type": "text", "text": "SYSTEM", "cache_control": _EPHEMERAL}]


def test_cache_breakpoint_rides_a_reused_prefix_and_not_a_single_call():
    """A discovery then a brief: the shared block is a cache read of the first's write, the remainder is not (#9, #258)."""
    fake = FakeClient(_ENGINE_REPLY, _BRIEF_REPLY)
    run(fake, _USER)
    advise(fake, _MODEL)
    first, second = _blocks(fake, 0), _blocks(fake, 1)
    assert first[0]["text"] == second[0]["text"] and first[0]["cache_control"] == second[0]["cache_control"] == _EPHEMERAL
    assert first[1]["cache_control"] == _EPHEMERAL          # must fire: converse() loops the engine prompt
    assert first[1]["text"] != second[1]["text"] and "cache_control" not in second[1]  # must not fire


def test_the_provider_seam_is_single_call_on_both_analyze_branches():
    """`AnthropicProvider.analyze` is one call per operation and writes no cache entry nothing reads (#58)."""
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY, _ENGINE_REPLY)
    provider = AnthropicProvider(fake)
    provider.analyze("leave approval")                                     # first discovery
    provider.analyze("leave approval", current_model=_MODEL, answers="A")  # a refinement turn
    run(fake, _USER)                                                       # the multi-call caller
    assert "cache_control" not in _specific(fake, 0) and "cache_control" not in _specific(fake, 1)
    assert _specific(fake, 2)["cache_control"] == _EPHEMERAL, "converse() lost its breakpoint"
    assert _specific(fake, 0)["text"] == _specific(fake, 1)["text"] == _specific(fake, 2)["text"]  # MUST FIRE: one prompt


def test_a_looping_caller_can_still_ask_for_the_breakpoint_back():
    """`reuse_system` is a per-call-site decision, not a per-function one."""
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY)
    answer_turn(fake, _MODEL, "leave approval", "A")
    answer_turn(fake, _MODEL, "leave approval", "A", reuse_system=True)
    assert "cache_control" not in _specific(fake, 0) and _specific(fake, 1)["cache_control"] == _EPHEMERAL


# A minimal contract-valid reply per generator, so each assertion drives the real call (#9).
_GENERATOR_REPLIES = {
    "brief": _BRIEF_REPLY,
    "gtm_plan": json.dumps({}),  # #609: every field on GoToMarketPlan has a default
    "stories": json.dumps({"stories": [{"id": "S1", "title": "T"}]}),
    "prd": json.dumps({"title": "T", "problem": "P"}),
    "criteria": json.dumps({"title": "T", "features": [
        {"name": "F", "scenarios": [{"id": "SC1", "title": "T", "when": "w", "then": ["t"]}]}]}),
    "epic": json.dumps({"title": "T", "issues": [{"id": "E1", "title": "T"}]}),
    "release": json.dumps({"title": "T"}),
    "estimate": json.dumps({"items": [{"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]}),
}
# `estimate` is the one pipeline stage: it reads the prior stories (#146).
_GENERATOR_KWARGS = {"estimate": {"stories": Stories(stories=[Story(id="S1", title="T")])}}


@pytest.mark.parametrize("artifact_type", sorted(_GENERATOR_REPLIES))
def test_every_generator_drives_a_real_call_without_a_cache_write(artifact_type):
    reply, extra = _GENERATOR_REPLIES[artifact_type], _GENERATOR_KWARGS.get(artifact_type, {})
    fake = FakeClient(reply, reply)
    _GENERATORS[artifact_type](fake, _MODEL, **extra)
    _GENERATORS[artifact_type](fake, _MODEL, **extra, reuse_system=True)
    assert "cache_control" not in _specific(fake, 0), f"{artifact_type} pays for a cache nothing reads"
    assert _specific(fake, 1)["cache_control"] == _EPHEMERAL, f"{artifact_type} lost its opt-in"
    for i in (0, 1):  # the shared block rides in front of both, cached (#258)
        assert len(_blocks(fake, i)) == 2 and _blocks(fake, i)[0]["cache_control"] == _EPHEMERAL


def test_the_cache_fixture_covers_every_registered_generator():
    assert set(_GENERATOR_REPLIES) == set(_GENERATORS) and set(_GENERATOR_KWARGS) <= set(_GENERATORS)


def test_complete_still_defaults_to_caching_for_an_undeclared_caller():
    import inspect

    assert inspect.signature(_complete).parameters["reuse_system"].default is True
    fake = FakeClient(_BRIEF_REPLY)
    _complete(fake, "SYSTEM", [{"role": "user", "content": "u"}], Brief)
    assert _specific(fake, 0)["cache_control"] == _EPHEMERAL


def test_splitting_the_system_prompt_does_not_change_the_system_prompt_bytes():
    fake = FakeClient(_BRIEF_REPLY)
    advise(fake, _MODEL)
    assert "".join(b["text"] for b in _blocks(fake, 0)) == build_prompt("brief.md", None)


@pytest.mark.parametrize("reuse", [True, False])
def test_retry_resends_a_byte_identical_system_whether_or_not_it_is_cached(reuse):
    """A retry re-sends the same bytes on both arms, or the cache is lost exactly where it pays."""
    fake = FakeClient("not json at all", _BRIEF_REPLY)
    _complete(fake, "SYSTEM PROMPT", [{"role": "user", "content": "u"}], Brief, reuse_system=reuse)
    assert len(fake.calls) == 2, "expected one retry"
    assert _specific(fake, 0)["text"] == _specific(fake, 1)["text"] == "SYSTEM PROMPT"
    assert all(("cache_control" in _specific(fake, i)) is reuse for i in (0, 1))
