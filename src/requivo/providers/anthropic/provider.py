"""`AnthropicProvider` — the `ReasoningProvider` face over the functions in this package."""

from __future__ import annotations

from requivo.core.context import CardSummary
from requivo.core.contracts import ContextJudgment, EngineOutput, PerimeterJudgment
from requivo.core.perimeters import DEFAULT_PERIMETER, PerimeterSummary
from requivo.providers.anthropic.client import current_model_name, new_client
from requivo.providers.anthropic.generators import (
    _GENERATORS,
    answer_turn,
    judge_context,
    judge_perimeter,
    prompt_version,
    run,
)
from requivo.providers.errors import EngineError


class AnthropicProvider:
    """`ReasoningProvider` over the Anthropic SDK: a thin face over the free functions in `generators.py`."""

    name = "anthropic"

    def __init__(self, client=None, model: str | None = None):
        """`model` is an optional fixed model id: `None` keeps `current_model_name()`'s env chain, an
        explicit id makes no env read at all (#434, `test_a_constructed_model_makes_no_env_read`).
        Stored as `self._model`, since `generate()`'s `model` names the *requirements* model."""
        self.client = client or new_client()
        self._model = model

    def analyze(self, request: str, *, current_model: EngineOutput | None = None,
                answers: str | None = None, only: list[str] | None = None,
                reuse_system: bool = False, perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
        """One reasoning turn on either branch, one call per operation by default (`reuse_system=False`);
        `DiscoveryService.draft_turn` passes True (#58, #77).
        `test_the_provider_seam_is_single_call_on_both_analyze_branches`."""
        if current_model is not None and answers is not None:
            return answer_turn(self.client, current_model, request, answers, only=only,
                               reuse_system=reuse_system, model=self._model, perimeter=perimeter)
        return run(self.client, [{"role": "user", "content": request}], only=only,
                   reuse_system=reuse_system, model=self._model, perimeter=perimeter)

    def judge_context(self, request: str, *, cards: list[CardSummary]) -> ContextJudgment:
        """`ContextJudge`, the second protocol this class satisfies."""
        return judge_context(self.client, request, cards, model=self._model)

    def judge_perimeter(self, request: str, *, perimeters: list[PerimeterSummary]) -> PerimeterJudgment:
        """`PerimeterJudge`, the third protocol this class satisfies (#601)."""
        return judge_perimeter(self.client, request, perimeters, model=self._model)

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None,
                 **kwargs):
        """`**kwargs` carries per-artifact options; an unknown one is a TypeError. `model` is the
        requirements model; `self._model` is forwarded by name as the LLM id (#434,
        `test_generate_threads_the_constructed_model_too`)."""
        try:
            fn = _GENERATORS[artifact_type]
        except KeyError as e:
            raise EngineError(f"unknown artifact type for the Anthropic provider: {artifact_type!r}") from e
        return fn(self.client, model, only=only, model=self._model, **kwargs)

    def model_name(self) -> str:
        return self._model if self._model is not None else current_model_name()

    def provenance(self, op: str, *, only: list[str] | None = None,
                   perimeter: str = DEFAULT_PERIMETER) -> dict:
        """Who reasoned, with what, against which prompt: the fields a revision records, from the layer that owns them."""
        return {"provider": self.name, "model_name": self.model_name(),
                "prompt_version": prompt_version(op, only, perimeter=perimeter)}
