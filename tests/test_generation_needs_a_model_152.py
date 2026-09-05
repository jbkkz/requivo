"""#152: generating from a session that has no model yet is a structured refusal, not a traceback.

`SessionSnapshot.model` is `None` before the first model — which the field says — and `generate()`
and `reason()` unpacked it and handed it to the provider unchecked. Every generator builds its user
message as `out.model_dump_json(...)`, so the failure landed as an `AttributeError` while assembling
the prompt: no API call, nothing written, and a raw traceback standing where every other refusal in
this codebase is a structured error naming its remedy.

The rule was written once, in `_require_a_model`, rather than at each call site, because it was
already enforced on exactly one surface out of three: the Web renders an "offer to run discovery"
page when `meta.current_revision == 0` (`routes/sessions.py`), the CLI had nothing, and Claude Code
had nothing. Hiding a button is good *on top of* an enforced rule and is not one — the same sentence
invariant 13 makes about the revision-zero gate, one guard along.

`_require_a_model` returns the model rather than returning `None`, so the narrowing is in the type as
well as in the control flow. That is what stops a future call site reintroducing the defect by
forgetting the guard: without it, eight pyright errors come back.

Driven directly against the services with a stub `ReasoningProvider` — no CLI, no web, no network —
the same shape `test_paid_call_safety_208.py` uses.
"""

from __future__ import annotations

import pytest
from _fakes import out, slot

from requivo.core.contracts import Brief
from requivo.core.errors import RevisionConflictError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))


class _CountingProvider:
    """Records whether it was reached at all. The refusal has to happen *before* this."""

    name = "stub"

    def __init__(self):
        self.generate_calls = 0

    def analyze(self, *a, **k):  # pragma: no cover - not the path under test
        raise NotImplementedError

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self.generate_calls += 1
        return Brief(complexity="low", solution="S")

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None):
        return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}


def _service(provider):
    sessions = SessionService()
    return sessions, DiscoveryService(provider=provider, sessions=sessions)


def _model_less_session(sessions: SessionService) -> str:
    """The 'create session only' state: a request captured, no discovery run. Revision 0."""
    meta = sessions.create_session("a leave approval system")
    assert meta.current_revision == 0
    return meta.slug


def test_generating_from_a_session_with_no_model_is_refused_before_the_provider():
    """The finding. A `RevisionConflictError` with the remedy in it, and the provider never reached —
    the call count is the assertion, because an `AttributeError` raised while assembling the prompt
    also never reached the provider, so a test asserting only "it raised" is green on the defect."""
    provider = _CountingProvider()
    sessions, disco = _service(provider)
    slug = _model_less_session(sessions)

    with pytest.raises(RevisionConflictError) as exc:
        disco.generate(slug, "brief")

    assert provider.generate_calls == 0, "the refusal must happen before anything is paid for"
    assert "no model yet" in exc.value.message
    assert "requivo discover" in exc.value.message, "a refusal names the way forward"
    assert exc.value.details["actual"] == 0


def test_the_refusal_is_a_requivo_error_and_not_an_attribute_error():
    """The half that names the defect rather than the fix. `AttributeError` is what a caller got for
    a release: it is not a `RequivoError`, so the CLI's `except RequivoError` handler does not catch
    it and the user sees a traceback instead of a message."""
    sessions, disco = _service(_CountingProvider())
    slug = _model_less_session(sessions)

    with pytest.raises(RevisionConflictError):
        disco.generate(slug, "brief")
    # The same guard, on the other entry point: `reason()` is the terminal-only analyses, which had
    # the identical unchecked unpack.
    with pytest.raises(RevisionConflictError):
        disco.reason(slug, "stories")


def test_a_session_that_does_have_a_model_still_generates():
    """The must-not-fire control. Without it, a guard that refused *every* session would pass the two
    tests above — the failure mode a refusal test cannot see on its own."""
    provider = _CountingProvider()
    sessions, disco = _service(provider)
    slug = _model_less_session(sessions)
    sessions.update_model(slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json())

    result = disco.generate(slug, "brief")

    assert provider.generate_calls == 1
    assert result.artifact.complexity == "low"
