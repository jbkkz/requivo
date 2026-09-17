"""The reasoning-provider seam: the only component allowed to call an LLM. The protocol matches the
real call shapes (`analyze` for a discovery turn, `generate` for an artifact); the Claude Code
surface bypasses it (Claude reasons, the deterministic CLI applies).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from requivo.core.context import CardSummary
from requivo.core.contracts import ContextJudgment, EngineOutput, PerimeterJudgment
from requivo.core.perimeters import DEFAULT_PERIMETER, PerimeterSummary


@runtime_checkable
class ReasoningProvider(Protocol):
    """The qualitative-reasoning contract; deterministic core never imports an implementation."""

    name: str
    """Short identity of the implementation (`"anthropic"`), stamped on the session and on every
    revision it produces. Declared here because `DiscoveryService` reads it on the first discovery,
    before it reasons — an implementation without one is not usable, so leaving it out of the contract
    described a seam narrower than the one the code depends on.

    A bare annotation rather than a method, deliberately: `@runtime_checkable` *does* check non-method
    members, so `isinstance` rejects a provider missing this one, and it matches how the attribute is
    already exposed and read (`provider.name`, not `provider.name()`). The cost is that `issubclass`
    against this protocol now raises `TypeError` — Python refuses it for any protocol with a
    non-method member. Use `isinstance`. Note it checks *presence*, not type: an implementation is
    still trusted to make this a `str`."""

    def analyze(
        self,
        request: str,
        *,
        current_model: EngineOutput | None = None,
        answers: str | None = None,
        only: list[str] | None = None,
        reuse_system: bool = False,
        perimeter: str = DEFAULT_PERIMETER,
    ) -> EngineOutput:
        """A discovery turn: request (+ prior model and answers) → a filled model. `only` restricts
        the cards, `perimeter` (#608) the vocabulary. `reuse_system` says this call is one of a series
        sending the identical system prompt: a hint about repetition only the caller can give (#9, #58, #77)."""
        ...

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None,
                 **kwargs) -> object:
        """A model → its typed artifact contract; `**kwargs` carries per-artifact options. `estimate`
        returns `(EstimateDraft, soft_slots, confidence)`, the two extras being deterministic reads of
        the same model; everything else returns exactly its contract."""
        ...

    def model_name(self) -> str:
        """The reasoning model this provider will call, recorded on the session."""
        ...

    def provenance(self, op: str, *, only: list[str] | None = None,
                   perimeter: str = DEFAULT_PERIMETER) -> dict:
        """Who reasoned, with what, against which prompt, for one operation; the service records it
        rather than assembling it. `perimeter` (#608) moves the hash for `analyze`."""
        ...


@runtime_checkable
class ContextJudge(Protocol):
    """Whether the installed context cards cover a request's domain, asked once before a first
    discovery on a prompt carrying neither schema nor cards; not part of `ReasoningProvider`, so a
    provider that cannot answer does not implement it and the service reports *not asked* (#492).
    `decision: the-engine-writes-the-missing-card`."""

    def judge_context(self, request: str, *, cards: list[CardSummary]) -> ContextJudgment:
        """One cheap call: the request and one line per installed card in, a verdict out. `cards` is
        passed, since `core` owns that read."""
        ...


@runtime_checkable
class PerimeterJudge(Protocol):
    """Which installed perimeter a request's shape belongs to (#601): `ContextJudge`'s separation,
    one question over, and the same *not asked* honesty."""

    def judge_perimeter(self, request: str, *,
                        perimeters: list[PerimeterSummary]) -> PerimeterJudgment:
        """One cheap call: the request and one line per installed perimeter in, a verdict out."""
        ...
