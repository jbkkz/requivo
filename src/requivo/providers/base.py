"""The reasoning-provider seam.

A provider turns natural language into a validated model, and a model into an artifact contract. It
is the *only* component allowed to call an LLM. Everything downstream — validation, versioning,
readiness, impact, rendering — is deterministic core and does not care which provider produced the
model.

The protocol is kept deliberately close to the real call shapes already in the code (`analyze` for a
discovery turn, `generate` for an artifact), not a speculative universal LLM abstraction. The
Anthropic implementation, and any future one, conforms to it; the Claude Code surface bypasses it
entirely (Claude reasons, the deterministic CLI applies).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from requivo.core.context import CardSummary
from requivo.core.contracts import ContextJudgment, EngineOutput


@runtime_checkable
class ReasoningProvider(Protocol):
    """The qualitative-reasoning contract. Deterministic core never imports an implementation of this;
    it receives the `EngineOutput` a provider produced and takes over from there."""

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
    ) -> EngineOutput:
        """A discovery turn: request (+ optional prior model and new answers) → a filled model.
        `only` restricts the context cards, held constant across a session's turns.

        `reuse_system` is the caller saying *this call is one of a series that will send the
        identical system prompt* — a drafting loop rather than a one-shot operation. It is a hint
        about repetition, not a directive about mechanism: an implementation with prompt caching
        should place its breakpoint on the strength of it, one without may ignore it entirely. It
        belongs in the contract because only the caller can answer it, and the default is the
        one-shot answer that every service operation but `draft_turn` gives (#9, #58, #77)."""
        ...

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None,
                 **kwargs) -> object:
        """A model → a typed artifact contract (PRD, Stories, Epic, …). The concrete return type is
        the Pydantic contract for `artifact_type`; the caller renders/persists it. `**kwargs` carries
        the few per-artifact options a generator takes (e.g. a `version` to stamp on release notes,
        or the prior `stories` an estimate is read against).

        One operation returns more than its contract and it is said here rather than discovered:
        `estimate` returns `(EstimateDraft, soft_slots, confidence)`, because the two extras are
        deterministic reads of the same model and splitting them across two calls is how they get
        out of step. Everything else returns exactly its contract."""
        ...

    def model_name(self) -> str:
        """The reasoning model this provider will call — recorded on the session it produces."""
        ...

    def provenance(self, op: str, *, only: list[str] | None = None) -> dict:
        """Who reasoned, with what, and against which prompt, for one operation (`analyze` or an
        artifact type). The service records this on the revision rather than assembling it itself —
        provider identity and prompt identity belong to the layer that owns them, so a second provider
        cannot end up stamping its revisions with another's name."""
        ...


@runtime_checkable
class ContextJudge(Protocol):
    """Whether the installed context cards cover a request's domain — asked once, before a first
    discovery, and deliberately **not** part of `ReasoningProvider`.

    Separate because it is a different question. `ReasoningProvider` reasons *over the model*, from
    the shared system prefix every one of its operations shares; this reasons about the *grounding*,
    before a session has a card selection at all, on a prompt that carries neither the schema nor the
    cards (`build_standalone_prompt`). Keeping it out of that protocol also means a provider — or a
    test stub — that cannot answer this simply does not implement it, and the service reports *not
    asked* rather than inventing a verdict. That third state is the point: *no card is needed* and
    *nobody looked* must never render the same, which is the failure #492 refused a status for.

    `decision: the-engine-writes-the-missing-card`.
    """

    def judge_context(self, request: str, *, cards: list[CardSummary]) -> ContextJudgment:
        """One cheap call: the request and one line per installed card, in; a verdict, out.

        `cards` is passed rather than read, because the summaries are a deterministic read of the
        install and `core` already owns it — a provider re-deriving them could answer about a
        different set than the one the session will actually load."""
        ...
