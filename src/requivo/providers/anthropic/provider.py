"""`AnthropicProvider` — the `ReasoningProvider` face over the functions in this package."""

from __future__ import annotations

from requivo.core.contracts import EngineOutput
from requivo.providers.anthropic.client import current_model_name, new_client
from requivo.providers.anthropic.generators import _GENERATORS, answer_turn, prompt_version, run
from requivo.providers.errors import EngineError


class AnthropicProvider:
    """`ReasoningProvider` over the Anthropic SDK. Holds a client so the free functions in
    `generators.py` (which tests still exercise directly with a fake client) stay the single
    implementation — the object is a thin, uniform face over them for callers that want the provider
    seam."""

    name = "anthropic"

    def __init__(self, client=None, model: str | None = None):
        """`model` is an optional fixed model id for this instance. `None` (the default) preserves
        the pre-existing behaviour byte-for-byte via `current_model_name()`'s env chain; set
        explicitly, it wins outright with **no env read at all**, so two providers built with two
        different ids in one process price and record independently (#434). Stored privately
        (`self._model`, not `self.model`) because `generate()`'s own `model` parameter already names
        the *requirements* model, an unrelated concept that happens to share the word. Pinned by
        `test_a_constructed_model_makes_no_env_read`,
        `test_two_constructed_providers_record_and_price_independently` and
        `test_default_construction_is_byte_identical_to_before_434`."""
        self.client = client or new_client()
        self._model = model

    def analyze(self, request: str, *, current_model: EngineOutput | None = None,
                answers: str | None = None, only: list[str] | None = None,
                reuse_system: bool = False) -> EngineOutput:
        """One reasoning turn, on either branch — **one call per operation by default**, hence
        `reuse_system=False`. The caching question is decidable only per *operation*, not per
        function: `DiscoveryService.start`/`run_discovery`/`answer` each reach this once, while the
        one looping caller inside the seam, `DiscoveryService.draft_turn`, passes `reuse_system=True`
        and earns the breakpoint there instead (#58, #77). Pinned by
        `test_the_provider_seam_is_single_call_on_both_analyze_branches`."""
        if current_model is not None and answers is not None:
            return answer_turn(self.client, current_model, request, answers, only=only,
                               reuse_system=reuse_system, model=self._model)
        return run(self.client, [{"role": "user", "content": request}], only=only,
                   reuse_system=reuse_system, model=self._model)

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None,
                 **kwargs):
        """`**kwargs` carries the few per-artifact options a generator takes (release notes accept a
        `version` to stamp); an option a generator does not know is a TypeError, not a silent no-op.

        `model` here is the *requirements* model (this method's own parameter, inherited from the
        protocol) — not to be confused with `self._model`, the constructed LLM id forwarded below as
        the generator functions' own `model=` keyword: `model` binds positionally to the callee's
        requirements-model parameter, `model=self._model` binds by name to its `model: str | None`,
        and the two never collide (#434). Pinned by `test_generate_threads_the_constructed_model_too`."""
        try:
            fn = _GENERATORS[artifact_type]
        except KeyError as e:
            raise EngineError(f"unknown artifact type for the Anthropic provider: {artifact_type!r}") from e
        return fn(self.client, model, only=only, model=self._model, **kwargs)

    def model_name(self) -> str:
        return self._model if self._model is not None else current_model_name()

    def provenance(self, op: str, *, only: list[str] | None = None) -> dict:
        """Who reasoned, with what, against which prompt — the fields a revision records. The service
        asks the provider for this instead of assembling it, so a second provider cannot silently
        stamp revisions as `anthropic`, and the prompt identity comes from the layer that owns it."""
        return {"provider": self.name, "model_name": self.model_name(),
                "prompt_version": prompt_version(op, only)}
