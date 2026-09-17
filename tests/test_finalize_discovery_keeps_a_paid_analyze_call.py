"""#467: `start(finalize=True)` must not discard a paid `analyze()` call when the brief that follows it fails
or is refused."""

from __future__ import annotations

import pytest
from _fakes import out, slot

from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))


class _AnalyzeSucceedsBriefFailsProvider:
    """A stub `ReasoningProvider` whose `analyze()` succeeds and whose `generate("brief", ...)` always."""

    name = "stub"

    def __init__(self):
        self.analyze_calls = 0
        self.generate_calls = 0

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
                perimeter=None):
        self.analyze_calls += 1
        return out({"problem": slot(80, "explicit", "high")})

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self.generate_calls += 1
        assert artifact_type == "brief"
        raise EngineError("the model is temporarily unavailable")

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}


def test_a_failed_brief_leaves_the_analyzed_discovery_applied():
    sessions = SessionService()
    provider = _AnalyzeSucceedsBriefFailsProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions)

    with pytest.raises(EngineError):
        disco.start("a leave approval system", finalize=True)

    # Both calls were attempted exactly once each -- this test is not about retrying anything.
    assert provider.analyze_calls == 1
    assert provider.generate_calls == 1

    # The outcome that matters: the paid `analyze()` result is NOT thrown away.
    # -- asserting only that the exception propagated (as the pre-fix code already did) would pass
    # against the unfixed ordering too (CLAUDE.md's #320 note).
    slug = sessions.list_sessions()[0].slug
    meta = sessions.repo.read_meta(slug)
    assert meta.current_revision == 1
    model = sessions.load_model(slug)
    assert model.model["problem"].completeness == 80
    assert model.model["problem"].confidence == "explicit"

    # The brief is retryable through the ordinary path every other caller of a brief uses.
    provider.generate_calls = 0

    class _NowSucceeds(_AnalyzeSucceedsBriefFailsProvider):
        def generate(self, artifact_type, model, *, only=None, **kwargs):
            self.generate_calls += 1
            from requivo.core.contracts import Brief
            return Brief(complexity="low", solution="S")

    disco._provider = _NowSucceeds()
    gen = disco.generate(slug, "brief")
    assert gen.artifact.solution == "S"
    assert sessions.repo.read_meta(slug).current_revision == 2


def test_a_successful_finalize_still_applies_both_the_discovery_and_the_brief():
    """Must-fire control for the test above: without the reordering fix having actually run the brief step at
    all, this would also report revision 1 and no absorbed reasoning."""
    from requivo.core.contracts import Brief

    sessions = SessionService()

    class _AlwaysSucceeds:
        name = "stub"

        def __init__(self):
            self.analyze_calls = 0
            self.generate_calls = 0

        def analyze(self, request, *, current_model=None, answers=None, only=None,
                   reuse_system=False, perimeter=None):
            self.analyze_calls += 1
            return out({"problem": slot(80, "explicit", "high")})

        def generate(self, artifact_type, model, *, only=None, **kwargs):
            self.generate_calls += 1
            assert artifact_type == "brief"
            return Brief(complexity="low", solution="S", decisions=[], challenges=[],
                        opportunities=[])

        def model_name(self):
            return "stub-model"

        def provenance(self, op, *, only=None, perimeter=None):
            return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}

    provider = _AlwaysSucceeds()
    disco = DiscoveryService(provider=provider, sessions=sessions)

    slug = disco.start("a leave approval system", finalize=True)

    assert provider.analyze_calls == 1
    assert provider.generate_calls == 1
    assert sessions.repo.read_meta(slug).current_revision == 2
