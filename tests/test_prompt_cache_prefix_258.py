"""Cross-operation prompt caching (#258): one byte-identical leading block, cached on every call.

Every prompt template opens with the same block -- the model schema and the product context, the
~9k tokens every operation shares -- and `build_system_prompt` splits the assembled system prompt
exactly at that block's end. `_system_blocks` sends it as its own text block with a
`cache_control` breakpoint on **every** call, so the second operation in a sitting reads the block
at 0.1x instead of re-sending it at full price; the op-specific remainder carries a breakpoint only
under the existing `reuse_system=True` contract. `prompt_version()` still hashes the whole string.

Three claims, three guards, and each negative half has its positive control in the same fixture:
the eight templates really share the block (and a perturbed template is refused rather than sent
with a shorter or empty prefix); the request carries the breakpoint where the design says; and the
split is exactly the block's end, so what the model reads is byte for byte what `build_prompt()`
assembled before the split existed.
"""
import hashlib
import json
import shutil

import pytest
from _fakes import FakeClient, out, slot

from requivo import paths
from requivo.core import context as ctx
from requivo.core.context import SHARED_PROMPT_HEAD, SystemPrompt, build_prompt, build_system_prompt
from requivo.core.contracts import Brief
from requivo.providers.anthropic.completion import _complete, _system_blocks
from requivo.providers.anthropic.generators import _OP_PROMPTS, prompt_version

_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})
_EPHEMERAL = {"type": "ephemeral"}


def _names() -> list[str]:
    return sorted(set(_OP_PROMPTS.values()))


# ── The eight templates share one leading block ──────────────────────────────


def test_every_template_opens_with_the_shared_head_and_places_the_placeholders_only_there():
    names = _names()
    assert len(names) == 8, names
    for name in names:
        raw = (paths.PROMPTS / name).read_text(encoding="utf-8")
        assert raw.startswith(SHARED_PROMPT_HEAD), f"{name} does not open with the shared block"
        rest = raw[len(SHARED_PROMPT_HEAD):]
        assert "{{SCHEMA}}" not in rest and "{{CONTEXT}}" not in rest, (
            f"{name} substitutes the shared bulk a second time outside the leading block"
        )


def test_the_eight_built_prompts_share_one_identical_leading_block():
    built = {name: build_system_prompt(name, None) for name in _names()}
    shared = {p.shared for p in built.values()}
    assert len(shared) == 1, "the leading block differs between templates, so no cross-op cache hit"
    (head,) = shared
    # It carries the bulk it exists to cache, in the order the head declares it.
    assert head.startswith("# Model schema\n")
    assert '"slots"' in head                        # framework/model_schema.json
    assert "## b2b-platform" in head                # a context card
    assert head.index("# Model schema") < head.index("# Product context") < head.index("## b2b-platform")
    # The remainders are what tell the operations apart.
    assert len({p.specific for p in built.values()}) == len(built)


def test_a_narrowed_card_selection_still_yields_one_shared_block_across_operations():
    a = build_system_prompt("engine.md", ["b2b-platform"])
    b = build_system_prompt("brief.md", ["b2b-platform"])
    assert a.shared == b.shared
    assert "## financial-reporting" not in a.shared
    assert a.shared != build_system_prompt("engine.md", None).shared  # the selection is in the block


def test_a_template_whose_leading_block_is_perturbed_is_refused_not_sent(tmp_path, monkeypatch):
    """The must-fire control for the two tests above: a template that stops opening with the exact
    block is a defect in the asset, and the answer is a refusal -- never a call that quietly sends a
    shorter prefix (a cache miss on every other operation) or an empty shared block."""
    prompts = tmp_path / "prompts"
    shutil.copytree(paths.PROMPTS, prompts)
    victim = prompts / "brief.md"
    raw = victim.read_text(encoding="utf-8")
    assert raw.startswith(SHARED_PROMPT_HEAD)
    victim.write_text(raw.replace("# Product context", "# Product Context", 1), encoding="utf-8")
    monkeypatch.setattr(ctx, "PROMPTS", prompts)
    with pytest.raises(ValueError, match="brief.md"):
        build_system_prompt("brief.md", None)
    # Positive control in the same fixture: an untouched sibling under the same patched root builds.
    assert build_system_prompt("prd.md", None).shared.startswith("# Model schema")


# ── The split is exactly the block's end ─────────────────────────────────────


def test_the_split_is_exactly_the_end_of_the_leading_block():
    for name in _names():
        p = build_system_prompt(name, None)
        assert p.text == p.shared + p.specific
        assert p.text == build_prompt(name, None), "build_prompt is the same string, unsplit"
        assert p.shared.endswith("\n\n") and not p.specific.startswith("\n")
        assert p.shared == SHARED_PROMPT_HEAD.replace(
            "{{SCHEMA}}", (paths.FRAMEWORK / "model_schema.json").read_text(encoding="utf-8")
        ).replace("{{CONTEXT}}", ctx.load_context(None))


def test_prompt_version_hashes_the_whole_assembled_string():
    for op, name in _OP_PROMPTS.items():
        digest = hashlib.sha256(build_system_prompt(name, None).text.encode("utf-8")).hexdigest()
        assert prompt_version(op) == "sha256:" + digest
    assert len({prompt_version(op) for op in _OP_PROMPTS}) == len(_OP_PROMPTS)


# ── The request carries the breakpoint where the design says ─────────────────


def _blocks(fake: FakeClient, i: int) -> list[dict]:
    return fake.calls[i]["system"]


def test_a_one_call_verb_caches_the_shared_block_and_only_that():
    fake = FakeClient(_BRIEF_REPLY)
    system = build_system_prompt("brief.md", None)
    _complete(fake, system, [{"role": "user", "content": "u"}], Brief, reuse_system=False)
    shared, specific = _blocks(fake, 0)
    assert shared["text"] == system.shared
    assert shared["cache_control"] == _EPHEMERAL          # must fire: the cross-op saving
    assert specific["text"] == system.specific
    assert "cache_control" not in specific                # must not fire: nothing re-reads it
    assert shared["text"] + specific["text"] == build_prompt("brief.md", None)


def test_a_looping_caller_caches_both_blocks():
    fake = FakeClient(_BRIEF_REPLY)
    system = build_system_prompt("brief.md", None)
    _complete(fake, system, [{"role": "user", "content": "u"}], Brief, reuse_system=True)
    shared, specific = _blocks(fake, 0)
    assert shared["cache_control"] == _EPHEMERAL
    assert specific["cache_control"] == _EPHEMERAL


def test_the_shared_breakpoint_carries_the_default_ttl():
    """Deliberately the 5-minute default, not `ttl: "1h"` -- the arithmetic is in the pull request
    that landed #258. A TTL appearing here is a pricing change (2x write instead of 1.25x), so it
    must fail this test rather than ride in."""
    for reuse in (False, True):
        for block in _system_blocks(build_system_prompt("brief.md", None), reuse):
            if "cache_control" in block:
                assert block["cache_control"] == _EPHEMERAL, block["cache_control"]


def test_a_bare_string_system_has_no_shared_block_to_cache():
    """A caller that hands `_complete` a plain string (the test fakes do) has no leading block, so
    there is nothing to cache across operations: one block, breakpoint per `reuse_system`, exactly
    as before #258. Both arms, so the str path is not silently the SystemPrompt path."""
    assert _system_blocks("SYSTEM", False) == [{"type": "text", "text": "SYSTEM"}]
    assert _system_blocks("SYSTEM", True) == [
        {"type": "text", "text": "SYSTEM", "cache_control": _EPHEMERAL}]


def test_the_shared_block_is_byte_identical_across_two_operations_in_one_sitting():
    """The acceptance criterion, offline: a discovery followed by a brief sends the same first block,
    with the breakpoint on it both times, so the second call is a cache *read* of the first's write.
    Driven through the real generators rather than `_complete`, so a generator that stops threading
    `build_system_prompt` fails here."""
    from _fakes import _ENGINE_REPLY

    from requivo.providers.anthropic import advise, run

    fake = FakeClient(_ENGINE_REPLY, _BRIEF_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}], reuse_system=False)
    advise(fake, out({"problem": slot(80, "explicit", "high")}))
    first, second = _blocks(fake, 0), _blocks(fake, 1)
    assert first[0]["text"] == second[0]["text"]
    assert first[0]["cache_control"] == second[0]["cache_control"] == _EPHEMERAL
    assert first[1]["text"] != second[1]["text"]          # engine.md vs brief.md
    assert "cache_control" not in first[1] and "cache_control" not in second[1]


def test_system_prompt_is_a_plain_two_field_value():
    p = SystemPrompt("A", "B")
    assert p.text == "AB" and p.shared == "A" and p.specific == "B"
