"""Prompt-cache breakpoints: paid for only where the prefix is actually re-read (#9, #258), and
the generator/operation coverage that keeps every registered generator honest about it.

Split out of `test_provider.py` (#555) once that file grew past the module ceiling.
"""
import json

import pytest
from _fakes import _ENGINE_REPLY, FakeClient, out, slot

from requivo.core.contracts import Brief, Stories, Story
from requivo.providers.anthropic import advise, answer_turn, run
from requivo.providers.anthropic.completion import _complete
from requivo.providers.anthropic.pricing import price_call
from requivo.usage import CallRecord, UsageLedger

# ── Prompt-cache breakpoints: paid for only where the prefix is re-read (#9, #258) ──
#
# `cache_control` costs 1.25x input to write and pays back at 0.1x on a read, so it is a saving only
# when the *same* prefix is sent again inside the cache TTL. The system prompt is two blocks since
# #258, and they answer that question differently. The first is the schema + context block every
# template opens with (`SHARED_PROMPT_HEAD`) — byte-identical across operations, so it *is* re-read
# by the next verb in a sitting and carries the breakpoint on every call; that half is pinned in
# `tests/test_prompt_cache_prefix.py`. The second is the operation's own remainder, which is
# re-sent only across the calls of one operation — a JSON retry, converse()'s turns, a golden
# capture's K runs — and never across operations. A one-shot generator writing a breakpoint on it was
# writing a cache nothing could read, a flat ~25% premium on that part of its input; `reuse_system`
# is the caller's declaration, and these tests read the remainder block to check it is honoured.
#
# Every "must not fire" assertion below is paired with a "must fire" control in the same fixture: a
# fix that strips the directive everywhere breaks the operations where caching genuinely pays, and
# these tests fail on that too.


def _specific_block(fake, i: int) -> dict:
    """The op-specific remainder — the block `reuse_system` governs. It is the *last* system block,
    so a system sent as one plain string (no shared block) reads the same way."""
    return fake.calls[i]["system"][-1]


def _shared_block(fake, i: int) -> dict:
    return fake.calls[i]["system"][0]


_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})


def test_cache_breakpoint_rides_a_reused_prefix_and_not_a_single_call():
    # Both halves in ONE fixture. Discovery keeps the breakpoint (converse() loops it, the golden
    # harness loops it, a retry re-sends it); a one-shot generator does not.
    fake = FakeClient(_ENGINE_REPLY, _BRIEF_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}])
    advise(fake, out({"problem": slot(80, "explicit", "high")}))
    assert _specific_block(fake, 0)["cache_control"] == {"type": "ephemeral"}  # must fire
    assert "cache_control" not in _specific_block(fake, 1)                     # must not fire
    # The shared block is the other half of the same fixture: cached on both, and the same bytes, so
    # the brief's call is a cache *read* of the discovery's write (#258).
    assert _shared_block(fake, 0)["cache_control"] == _shared_block(fake, 1)["cache_control"] == {"type": "ephemeral"}
    assert _shared_block(fake, 0)["text"] == _shared_block(fake, 1)["text"]


def test_the_provider_seam_is_single_call_on_both_analyze_branches():
    """#58: `AnthropicProvider.analyze` is one call per service operation and must not write a
    cache entry nothing reads, on either branch. Only the `run()` arm is pinned here (dropping
    `analyze`'s explicit `reuse_system=False` makes this red); the neighbouring
    `test_a_looping_caller_can_still_ask_for_the_breakpoint_back` pins the `answer_turn` arm instead.
    The control in the same fixture is the point: `run()` called directly must still carry it."""
    from requivo.providers.anthropic import AnthropicProvider

    model = out({"problem": slot(80, "explicit", "high")})
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY, _ENGINE_REPLY)
    provider = AnthropicProvider(fake)

    provider.analyze("leave approval")                                     # first discovery
    provider.analyze("leave approval", current_model=model, answers="A")   # a refinement turn
    run(fake, [{"role": "user", "content": "leave approval"}])             # the multi-call caller

    assert "cache_control" not in _specific_block(fake, 0), "a first discovery pays for a cache nothing reads"
    assert "cache_control" not in _specific_block(fake, 1), "a refinement turn pays for a cache nothing reads"
    assert _specific_block(fake, 2)["cache_control"] == {"type": "ephemeral"}, "converse() lost its breakpoint"
    # MUST FIRE: all three sent the same engine prompt, so the assertions above are about the
    # directive and not about three different system blocks.
    assert _specific_block(fake, 0)["text"] == _specific_block(fake, 1)["text"] == _specific_block(fake, 2)["text"]


def test_a_looping_caller_can_still_ask_for_the_breakpoint_back():
    """The escape hatch, kept honest. `reuse_system` is a per-call-site decision, not a per-function
    one — the same `run()` is single-call under the provider seam and multi-call under `converse()`.
    A future surface that genuinely loops `answer_turn` passes True and gets the directive."""
    model = out({"problem": slot(80, "explicit", "high")})
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY)
    answer_turn(fake, model, "leave approval", "A")
    answer_turn(fake, model, "leave approval", "A", reuse_system=True)
    assert "cache_control" not in _specific_block(fake, 0)                     # must not fire
    assert _specific_block(fake, 1)["cache_control"] == {"type": "ephemeral"}  # must fire


# A minimal contract-valid reply per generator, so the assertions below can drive the *real* call
# rather than reading a signature. An earlier version of this test checked
# `inspect.signature(fn).parameters["reuse_system"].default is False` and nothing else, which both
# reviewers independently called vacuous and they were right: a generator that declared the parameter
# and then ignored it — passing nothing to `_complete`, falling back to its `True` default — satisfied
# every assertion while writing exactly the cache entry #9 is about. A signature is not a behaviour.
_GENERATOR_REPLIES = {
    "brief": _BRIEF_REPLY,
    "gtm_plan": json.dumps({}),  # #609 -- every field on GoToMarketPlan has a default
    "stories": json.dumps({"stories": [{"id": "S1", "title": "T"}]}),
    "prd": json.dumps({"title": "T", "problem": "P"}),
    "criteria": json.dumps({"title": "T", "features": [
        {"name": "F", "scenarios": [{"id": "SC1", "title": "T", "when": "w", "then": ["t"]}]}]}),
    "epic": json.dumps({"title": "T", "issues": [{"id": "E1", "title": "T"}]}),
    "release": json.dumps({"title": "T"}),
    "estimate": json.dumps({"items": [
        {"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]}),
}

# What a generator takes beyond `(client, model)`. Keyed by generator so the fixture above stays one
# reply per registry entry and the equality below can be exact.
#
# `estimate` is the only one, because it is the only registry entry that is not the plain
# model → contract shape: it is a pipeline stage reading the *prior* stories, and it returns
# `(draft, soft_slots, confidence)` rather than a document. It is nonetheless in `_GENERATORS` and
# reached through the seam like every other operation — `cli.py:_cmd_estimate` calls
# `disco.reason_from(snap, "estimate", stories=stories)`, and `AnthropicProvider.generate` dispatches
# `fn(self.client, model, only=only, **kwargs)`. So `stories` is passed here as a keyword, the way
# the real dispatch passes it. (Until #146 this file said the opposite in both clauses — that
# `estimate` was outside `_GENERATORS` and that the CLI called it directly past the provider seam.
# Both had been false since #77 and #135.)
_GENERATOR_KWARGS = {
    "estimate": {"stories": Stories(stories=[Story(id="S1", title="T")])},
}


@pytest.mark.parametrize("artifact_type", sorted(_GENERATOR_REPLIES))
def test_every_generator_drives_a_real_call_without_a_cache_write(artifact_type):
    # Drives each registered generator for real and reads the request that came out, so a generator
    # that takes `reuse_system` and drops it on the floor fails here. Both arms in one test: the
    # default must not carry the directive, and `reuse_system=True` must — so "deleted it everywhere"
    # fails too, per generator rather than only for `brief`.
    from requivo.providers.anthropic.generators import _GENERATORS

    reply = _GENERATOR_REPLIES[artifact_type]
    extra = _GENERATOR_KWARGS.get(artifact_type, {})
    model = out({"problem": slot(80, "explicit", "high")})
    fake = FakeClient(reply, reply)
    _GENERATORS[artifact_type](fake, model, **extra)
    _GENERATORS[artifact_type](fake, model, **extra, reuse_system=True)
    assert "cache_control" not in _specific_block(fake, 0), f"{artifact_type} pays for a cache nothing reads"
    assert _specific_block(fake, 1)["cache_control"] == {"type": "ephemeral"}, f"{artifact_type} lost its opt-in"
    # And the shared block rides in front of both, cached, so this generator can read what the
    # discovery before it wrote (#258) — a generator that stops threading `build_system_prompt` and
    # sends one plain block fails here.
    for i in (0, 1):
        assert len(fake.calls[i]["system"]) == 2, f"{artifact_type} sent no shared block"
        assert _shared_block(fake, i)["cache_control"] == {"type": "ephemeral"}


def test_the_cache_fixture_covers_every_registered_generator():
    # The parametrization above reads its cases off `_GENERATOR_REPLIES`, so an eighth generator
    # registered tomorrow would ship with no cache assertion and nothing would go red — under a test
    # whose name reads *every generator*. That is a guard that did not run and a guard that found
    # nothing rendering identically, which is this repository's own recurring shape, and it had
    # already happened once: `estimate` joined `_GENERATORS` in v1.1.0 and the fixture stayed at six
    # (#146). Asserted in both directions, like the boundaries allowlist: a registry entry with no
    # reply is an uncovered generator, and a reply for a name the registry does not hold is a case
    # that stopped exercising anything.
    from requivo.providers.anthropic.generators import _GENERATORS

    assert set(_GENERATOR_REPLIES) == set(_GENERATORS)
    assert set(_GENERATOR_KWARGS) <= set(_GENERATORS)


def test_complete_still_defaults_to_caching_for_an_undeclared_caller():
    # The control for every assertion above. "Will this be sent again?" is the caller's question, and
    # a caller that has not considered it should pay the safe answer — 25% once — rather than silently
    # lose a real cache worth up to 90% per repeat. If this default ever flips, the generators' saving
    # stops being a decision and becomes the accident of a global.
    import inspect

    assert inspect.signature(_complete).parameters["reuse_system"].default is True
    fake = FakeClient(_BRIEF_REPLY)
    _complete(fake, "SYSTEM", [{"role": "user", "content": "u"}], Brief)
    assert _specific_block(fake, 0)["cache_control"] == {"type": "ephemeral"}


def test_a_generator_can_opt_back_in_when_its_caller_loops_it():
    # scripts/golden_run.py --brief calls advise() K times with one system prompt, so the harness is a
    # genuine re-reader. The escape hatch has to actually reach the request, not just exist.
    fake = FakeClient(_BRIEF_REPLY, _BRIEF_REPLY)
    model = out({"problem": slot(80, "explicit", "high")})
    advise(fake, model)                       # production: one call
    advise(fake, model, reuse_system=True)    # harness: K calls, same prompt
    assert "cache_control" not in _specific_block(fake, 0)                     # must not fire
    assert _specific_block(fake, 1)["cache_control"] == {"type": "ephemeral"}  # must fire


def test_splitting_the_system_prompt_does_not_change_the_system_prompt_bytes():
    # Two blocks on the wire, one string in the hash. The split (#258) moves where a breakpoint may
    # sit and nothing else: what the model reads, block by block, is still exactly what
    # build_prompt() assembles and prompt_version() hashes. A split that dropped, doubled or
    # re-joined a byte would move the prompt hash on every revision without moving the prompt.
    from requivo.core.context import build_prompt

    fake = FakeClient(_BRIEF_REPLY)
    advise(fake, out({"problem": slot(80, "explicit", "high")}))
    sent = "".join(b["text"] for b in fake.calls[0]["system"])
    assert sent == build_prompt("brief.md", None)


def test_retry_resends_a_byte_identical_system_whether_or_not_it_is_cached():
    # The intra-operation invariant, on both arms: a retry must re-send the same bytes, or the cache
    # is lost exactly where it does pay. Asserted for the cached arm too, so a future edit that makes
    # the directive conditional on the attempt number fails here.
    for reuse, expect_directive in ((True, True), (False, False)):
        fake = FakeClient("not json at all", _BRIEF_REPLY)
        _complete(fake, "SYSTEM PROMPT", [{"role": "user", "content": "u"}], Brief,
                  reuse_system=reuse)
        assert len(fake.calls) == 2, "expected one retry"
        assert _specific_block(fake, 0)["text"] == _specific_block(fake, 1)["text"] == "SYSTEM PROMPT"
        for i in (0, 1):
            assert ("cache_control" in _specific_block(fake, i)) is expect_directive


def test_cost_estimate_bills_a_write_premium_and_plain_input_differently():
    # Not a new-behaviour test — a guard that the fix's whole point survives in the rendered number.
    # The ledger prices what the API *reported*, so dropping the directive moves those tokens from
    # cache_write (1.25x) to input (1.0x) with no arithmetic change here. If these two ever bill the
    # same, the saving becomes invisible and the issue's "the number rendered is correct" stops
    # holding.
    # The ratio, not the two dollar figures, is what this asserts. Stated absolutely it read as a
    # guard on the write premium and behaved as a guard on Sonnet 5's rate, so a rate correction went
    # red here with nothing to say about caching at all (#254).
    from datetime import date

    on = date(2026, 9, 1)
    cached = UsageLedger()
    cached.record(price_call(CallRecord(model="claude-sonnet-5", cache_write_tokens=1_000_000), on))
    plain = UsageLedger()
    plain.record(price_call(CallRecord(model="claude-sonnet-5", input_tokens=1_000_000), on))
    assert cached.cost_usd() == pytest.approx(plain.cost_usd() * 1.25)
    assert cached.cost_usd() > plain.cost_usd()
