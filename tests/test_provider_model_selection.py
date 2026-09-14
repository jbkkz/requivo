"""Which model id a call actually uses: the env-chain resolution (`current_model_name`, #268) and
the constructor-level override (#434) that lets one process run two models concurrently.

Split out of `test_provider.py` (#555) once that file grew past the module ceiling.
"""
import json

from _fakes import _ENGINE_REPLY, FakeClient, out, slot

from requivo.providers.anthropic import current_model_name

_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})


def test_current_model_name_reads_env_override(monkeypatch):
    monkeypatch.delenv("REQUIVO_MODEL", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    assert current_model_name() == "claude-sonnet-5"


def test_current_model_name_prefers_requivo_model_over_the_default(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"


def test_current_model_name_falls_back_to_bare_model(monkeypatch):
    # #268: the bare MODEL name predates REQUIVO_MODEL and existing setups must keep working --
    # it is read only when REQUIVO_MODEL is unset.
    monkeypatch.delenv("REQUIVO_MODEL", raising=False)
    monkeypatch.setenv("MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"


def test_current_model_name_prefers_requivo_model_when_both_are_set(monkeypatch):
    # The precedence case #268 exists to pin: a shell that already exports a generic MODEL for some
    # other tool must not silently steer Requivo once REQUIVO_MODEL is set alongside it.
    monkeypatch.setenv("MODEL", "some-other-tools-model")
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"



# ── #434: a constructor-level model id ────────────────────────────────────────
# `AnthropicProvider` used to resolve the model ambiently, per call, through
# `current_model_name()` -- an env-chain read -- so one process could not run two models
# concurrently, and per-tenant/per-session model choice meant mutating process env, which races
# across concurrent calls. An optional `model=` on the constructor threads a fixed id into every
# completion call, `model_name()` and provenance; `model=None` (the default) stays byte-identical
# to the env-chain resolution.


def test_two_constructed_providers_record_and_price_independently(monkeypatch):
    """Acceptance criterion 1, first half: two `AnthropicProvider`s in one process, each built with
    its own model id, make independent calls and are billed at their own model's rate -- no state
    shared between them."""
    from requivo.providers.anthropic import AnthropicProvider
    from requivo.usage import track_usage

    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")  # ambient value neither provider should use
    sonnet = AnthropicProvider(FakeClient(_ENGINE_REPLY), model="claude-sonnet-5")
    haiku = AnthropicProvider(FakeClient(_ENGINE_REPLY), model="claude-haiku-4-5")

    with track_usage() as ledger:
        sonnet.analyze("leave approval")
        haiku.analyze("leave approval")

    assert [c.model for c in ledger.calls] == ["claude-sonnet-5", "claude-haiku-4-5"]
    sonnet_rate, haiku_rate = ledger.calls[0].rate_per_mtok, ledger.calls[1].rate_per_mtok
    assert sonnet_rate is not None and haiku_rate is not None and sonnet_rate != haiku_rate, (
        "each call must be priced at its own model's rate, independently of the other"
    )


def test_a_constructed_model_makes_no_env_read(monkeypatch):
    """Acceptance criterion 1, second half, with its required positive control in the same fixture:
    a provider constructed with a model id must not consult `REQUIVO_MODEL`/`MODEL` at all -- proven
    by setting the environment to a third, distinct value the constructed provider must never
    produce. The default-constructed provider alongside it DOES read the environment (the must-fire
    control), so a broken plumbing that ignores the constructor argument entirely cannot pass."""
    from requivo.providers.anthropic import AnthropicProvider

    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")

    constructed = AnthropicProvider(FakeClient(_ENGINE_REPLY), model="claude-haiku-4-5")
    constructed.analyze("leave approval")
    assert constructed.client.calls[0]["model"] == "claude-haiku-4-5", (
        "a constructed model id must win over the ambient REQUIVO_MODEL, not merely agree with it"
    )
    assert constructed.model_name() == "claude-haiku-4-5"

    # MUST FIRE: the positive control -- default construction still reads the environment.
    ambient = AnthropicProvider(FakeClient(_ENGINE_REPLY))
    ambient.analyze("leave approval")
    assert ambient.client.calls[0]["model"] == "claude-opus-4-8"
    assert ambient.model_name() == "claude-opus-4-8"


def test_default_construction_is_byte_identical_to_before_434(monkeypatch):
    """Acceptance criterion 2: `AnthropicProvider()`/`AnthropicProvider(client)` with no `model=`
    keyword is pinned to the pre-#434 behaviour -- every call and `model_name()` still resolve
    through `current_model_name()`'s env chain, unchanged."""
    from requivo.providers.anthropic import AnthropicProvider

    monkeypatch.setenv("REQUIVO_MODEL", "claude-haiku-4-5")
    fake = FakeClient(_ENGINE_REPLY)
    provider = AnthropicProvider(fake)
    provider.analyze("leave approval")
    assert fake.calls[0]["model"] == current_model_name() == "claude-haiku-4-5"
    assert provider.model_name() == current_model_name()


def test_generate_threads_the_constructed_model_too():
    """The issue's own direction says 'threaded into the completion calls' (plural): `generate()`
    reaches `_complete` exactly like `analyze()` does, through a different registry entry, and must
    carry the same constructed model id -- not only the discovery turn."""
    from requivo.providers.anthropic import AnthropicProvider

    fake = FakeClient(_BRIEF_REPLY)
    provider = AnthropicProvider(fake, model="claude-haiku-4-5")
    model = out({"problem": slot(80, "explicit", "high")})
    provider.generate("brief", model)
    assert fake.calls[0]["model"] == "claude-haiku-4-5"
