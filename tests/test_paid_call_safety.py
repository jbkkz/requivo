"""#208: a paid decision brief must not be discarded when its model apply hits a revision conflict.

Driven directly against `DiscoveryService` with a stub `ReasoningProvider` -- no CLI, no web, no real
network -- so the provider call's own side effect can simulate the exact race: a write landing on the
session while the (minutes-long) assessment call is "in flight". `_fakes.out`/`slot` build a valid,
complete `EngineOutput` offline.
"""

from __future__ import annotations

import pytest
from _fakes import out, slot

from requivo.core.contracts import Brief
from requivo.core.errors import ArtifactWriteFailedError, RevisionConflictError
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))


class _ConflictingBriefProvider:
    """A stub provider whose generate("brief", ...) applies a competing write to the session as a
    side effect before returning -- standing in for a second tab, the CLI, or a Claude Code turn
    landing a change while the paid assessment call was reasoning."""

    name = "stub"

    def __init__(self, sessions: SessionService, slug: str):
        self.sessions = sessions
        self.slug = slug
        self.generate_calls = 0

    def analyze(self, *a, **k):  # pragma: no cover - unused by generate()
        raise NotImplementedError

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        assert artifact_type == "brief"
        self.generate_calls += 1
        # The race: someone else's write lands while this call is "in flight". Confidence moves
        # (explicit -> inferred), not just completeness -- diff_models treats completeness alone
        # as noise, and this write has to be *material* to actually invalidate the saved brief.
        self.sessions.update_model(
            self.slug,
            out({"problem": slot(95, "inferred", "high")}).model_dump_json())
        return Brief(complexity="low", solution="S")

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None):
        return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}


def _seeded_session(sessions: SessionService) -> str:
    meta = sessions.create_session("a leave approval system")
    sessions.update_model(meta.slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json())
    return meta.slug


def test_a_brief_lost_to_a_revision_conflict_is_still_saved_stale_not_discarded():
    sessions = SessionService()
    slug = _seeded_session(sessions)
    provider = _ConflictingBriefProvider(sessions, slug)
    disco = DiscoveryService(provider=provider, sessions=sessions)

    with pytest.raises(RevisionConflictError) as exc_info:
        disco.generate(slug, "brief")

    # The paid call happened exactly once -- this test is not about retrying it.
    assert provider.generate_calls == 1

    # The model was NOT modified by the losing apply: the concurrent write (revision 2) stands,
    # and the brief's reasoning was never absorbed into it.
    meta = sessions.repo.read_meta(slug)
    assert meta.current_revision == 2
    current = sessions.load_model(slug)
    assert current.decisions == [] and current.challenges == [] and current.opportunities == []

    # The document exists on disk anyway, flagged stale, tied to the revision it was reasoned from.
    artifacts = ArtifactService(sessions.repo)
    listed = artifacts.list(slug)
    assert "brief" in listed
    assert listed["brief"]["stale"] is True
    assert listed["brief"]["revision"] == 1  # source_revision, the one it was actually reasoned from
    content = artifacts.show(slug, "brief")
    assert "S" in content  # the brief's solution text made it to disk

    # The surfaced message states both facts and the remedy -- no special-casing needed on the CLI
    # (except RequivoError: print(e)) or the Web ({{ message }} in errors/_error.html).
    message = str(exc_info.value)
    assert "brief" in message.lower() and "saved" in message.lower()
    assert "not" in message.lower() and "absorbed" in message.lower()
    assert f"requivo brief {slug}" in message
    assert exc_info.value.details["artifact_saved"] is True
    assert exc_info.value.details["artifact_stale"] is True
    # The two sentences must not run together with no separator -- a real defect a first review
    # caught: "...re-apply The decision brief..." with nothing between "re-apply" and "The".
    assert "re-apply The decision brief" not in message
    assert ". The decision brief" in message


def test_a_brief_with_no_conflict_is_byte_identical_to_today():
    """Must-fire control: without it, a service that always saved-and-refused would pass the test
    above for the wrong reason -- because it never actually applies anything."""
    sessions = SessionService()
    slug = _seeded_session(sessions)

    class _CleanBriefProvider(_ConflictingBriefProvider):
        def generate(self, artifact_type, model, *, only=None, **kwargs):
            assert artifact_type == "brief"
            self.generate_calls += 1
            return Brief(complexity="low", solution="S")  # no competing write

    provider = _CleanBriefProvider(sessions, slug)
    disco = DiscoveryService(provider=provider, sessions=sessions)

    result = disco.generate(slug, "brief")

    assert provider.generate_calls == 1
    meta = sessions.repo.read_meta(slug)
    assert meta.current_revision == 2  # the brief's own absorb-and-apply
    assert result.model.decisions == [] and result.status.stale is False


def test_a_conflict_plus_a_secondary_write_failure_states_both_not_just_one():
    """Found in audit: if the fallback save inside the revision-conflict handler ALSO fails at the
    filesystem, the `ArtifactWriteFailedError` it raises must not silently drop the revision-conflict
    context it happened alongside -- a caller reading only `.message` needs to be told the content is
    genuinely lost (a write failure) AND that a race was the reason the model was never absorbed, not
    just one of the two."""
    sessions = SessionService()
    slug = _seeded_session(sessions)
    provider = _ConflictingBriefProvider(sessions, slug)
    disco = DiscoveryService(provider=provider, sessions=sessions)

    def _boom(*a, **k):
        raise OSError(28, "No space left on device")

    disco.artifacts.save = _boom  # type: ignore[method-assign]

    with pytest.raises(ArtifactWriteFailedError) as exc_info:
        disco.generate(slug, "brief")

    message = str(exc_info.value)
    assert "No space left" in message  # the write failure, the more urgent of the two
    assert "revision race" in message.lower()  # the conflict is not silently dropped
    assert exc_info.value.details["revision_conflict"] is True
    assert "revision_conflict_message" in exc_info.value.details


def test_an_oserror_writing_a_generated_artifact_is_a_structured_refusal_not_a_traceback():
    """The parallel, rarer case (#208): the content was paid for and produced, and only the write to
    the filesystem failed. Driven at _save_generated directly -- the one seam every generated
    artifact's write goes through -- with the repository's own save() stubbed to fail the way a full
    disk or a permissions error would."""
    sessions = SessionService()
    slug = _seeded_session(sessions)
    disco = DiscoveryService(provider=_ConflictingBriefProvider(sessions, slug), sessions=sessions)

    def _boom(*a, **k):
        raise OSError(28, "No space left on device")

    disco.artifacts.save = _boom  # type: ignore[method-assign]

    with pytest.raises(ArtifactWriteFailedError) as exc_info:
        disco._save_generated(slug, "prd", "# PRD\n", 1)

    assert exc_info.value.code == "artifact_write_failed"
    assert exc_info.value.details["slug"] == slug
    assert exc_info.value.details["type"] == "prd"
    assert "No space left" in exc_info.value.details["cause"]
    # Names the target path, not just the type -- the acceptance criterion's own wording (#208).
    assert exc_info.value.details["path"].endswith("prd.md")
    assert "prd.md" in str(exc_info.value)
