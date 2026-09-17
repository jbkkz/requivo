"""#519: the estimate graduates to a saved artifact type, and `stories` is finished with it."""

from __future__ import annotations

import json

import pytest
from _fakes import FakeClient, _model_in_out, _run_app, out, slot

from requivo.core.contracts import EstimateDraft, EstimateItem, Stories, Story
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.render.markdown import estimate_markdown, stories_markdown
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import GENERATABLE, DiscoveryService
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))


# ── the writers ─────────────────────────────────────────────────────────────────


def _stories() -> Stories:
    return Stories(stories=[
        Story(id="S1", title="Request leave", as_a="employee", i_want="to request leave",
              so_that="my manager can approve it", acceptance=["A request lands in the queue"],
              slots=["workflow", "actors"]),
        Story(id="S2", title="Approve | reject"),
    ])


def _draft() -> EstimateDraft:
    return EstimateDraft(
        items=[
            EstimateItem(story_id="S1", title="Request leave", complexity="S", days_low=1,
                         days_high=2, drives=["workflow"], note="one form"),
            EstimateItem(story_id="S2", title="Approve | reject", complexity="M", days_low=2.5,
                         days_high=5),
        ],
        risks=["The approval chain is not settled"],
    )


def test_stories_markdown_renders_every_story_and_traces_it_in_human_words():
    md = stories_markdown(_stories())
    assert md.startswith("# User stories")
    assert "## S1 — Request leave" in md
    assert "As a employee, I want to request leave, so that my manager can approve it." in md
    assert "- [ ] A request lands in the queue" in md
    # The Voice rule: a story traces to topics, and the reader sees their labels, never the ids.
    assert "Workflow / lifecycle" in md and "Actors" in md
    assert "workflow" not in md and "actors" not in md
    # A story with nothing but a title renders a heading and no empty template sentence.
    assert "## S2 — Approve | reject" in md
    assert "As a ," not in md
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_estimate_markdown_renders_ranges_totals_and_spread_in_human_words():
    md = estimate_markdown(_draft(), ["workflow", "config_vs_custom"], "medium")
    assert md.startswith("# Estimate")
    assert "**Confidence:** medium" in md
    # The totals are arithmetic over the items, both ends, formatted as the terminal prints them.
    assert "3.5–7 days" in md
    # The table escapes a `|` in a title so the row survives, and prints each range once.
    assert "| S1 — Request leave | S | 1–2 |" in md
    assert "Approve \\| reject" in md
    assert "one form" in md
    assert "- The approval chain is not settled" in md
    # The Voice rule again: the soft topics and the drivers are labelled, the ids do not appear.
    assert "Workflow / lifecycle" in md and "Config vs customization" in md
    assert "config_vs_custom" not in md and "workflow" not in md
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_a_newline_inside_a_provider_field_cannot_open_a_new_heading():
    """Found in review of #519."""
    stories = Stories(stories=[Story(id="S1", title="Request leave\n# INJECTED", i_want="a\nb")])
    draft = EstimateDraft(items=[EstimateItem(story_id="S1", title="T\n# INJECTED", complexity="S",
                                              days_low=1, days_high=1, note="n\n# INJECTED")],
                          risks=["r\n# INJECTED"])
    for md in (stories_markdown(stories), estimate_markdown(draft, [], "high")):
        assert "\n# INJECTED" not in md
        assert "INJECTED" in md


def test_estimate_markdown_says_when_nothing_widens_the_range():
    """The must-fire pair for the spread section: a solid model renders the section as a sentence rather than
    as an absent heading a reader cannot tell from a rendering bug."""
    md = estimate_markdown(_draft(), [], "high")
    assert "No unresolved topic widens these ranges" in md
    assert "Spread driven by" not in md


# ── the registries ──────────────────────────────────────────────────────────────


def test_both_analyses_are_registered_everywhere_a_saveable_type_is():
    """The seven-registry table from the decision, collapsed to what this change owes (#556)."""
    assert "stories" in GENERATABLE and "estimate" in GENERATABLE
    assert ARTIFACT_FILENAMES["stories"] == "stories.md"
    assert ARTIFACT_FILENAMES["estimate"] == "estimate.md"


# ── the service: one snapshot, two files, one revision ──────────────────────────


class _AnalysisProvider:
    """A `ReasoningProvider` that answers the two calls `estimate` makes."""

    name = "stub"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.stories = _stories()

    def analyze(self, *a, **k):  # pragma: no cover - not the path under test
        raise NotImplementedError

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self.calls.append((artifact_type, kwargs))
        if artifact_type == "stories":
            return self.stories
        if artifact_type == "estimate":
            return _draft(), ["workflow"], "high"
        raise AssertionError(f"unexpected generate({artifact_type!r})")

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}


def _session_with_a_model() -> tuple[str, SessionService, DiscoveryService, _AnalysisProvider]:
    provider = _AnalysisProvider()
    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    sessions.update_model(slug, out({"problem": slot(80, "explicit", "high"),
                                     "workflow": slot(60, "inferred", "high")}).model_dump_json())
    return slug, sessions, DiscoveryService(provider=provider, sessions=sessions), provider


def test_generating_the_estimate_saves_the_stories_it_was_reasoned_against_from_one_snapshot():
    slug, sessions, disco, provider = _session_with_a_model()
    seen = []

    result = disco.generate(slug, "estimate", on_stories=seen.append)

    # Two calls, in order, and the estimate was read against the very stories object that was saved.
    assert [t for t, _ in provider.calls] == ["stories", "estimate"]
    assert provider.calls[1][1]["stories"] is provider.stories
    assert seen == [provider.stories], "the stories are handed to the caller as soon as they land"

    listed = ArtifactService().list(slug)
    assert set(listed) >= {"stories", "estimate"}
    assert listed["stories"]["revision"] == listed["estimate"]["revision"] == 1
    assert listed["stories"]["stale"] is False and listed["estimate"]["stale"] is False
    assert result.status.filename == "estimate.md" and result.status.revision == 1
    assert result.artifact.stories_status.filename == "stories.md"
    assert result.artifact.stories_status.revision == 1
    assert result.artifact.draft.items[0].story_id == "S1"
    assert result.artifact.soft == ["workflow"] and result.artifact.confidence == "high"

    # What is on disk is the writers' output, byte for byte.
    assert sessions.repo.load_artifact(slug, "stories.md") == stories_markdown(provider.stories)
    assert sessions.repo.load_artifact(slug, "estimate.md") == estimate_markdown(_draft(), ["workflow"], "high")


def test_generating_the_stories_alone_saves_them_like_every_other_artifact():
    slug, sessions, disco, provider = _session_with_a_model()
    result = disco.generate(slug, "stories")
    assert [t for t, _ in provider.calls] == ["stories"]
    assert result.status.filename == "stories.md" and result.status.revision == 1
    assert sessions.repo.load_artifact(slug, "stories.md") == stories_markdown(provider.stories)


def test_the_estimate_generation_refuses_a_caller_supplied_stories_draft():
    """The provenance argument in one assertion: the stories an estimate is saved against are the ones this
    generation reasoned and saved."""
    slug, _, disco, provider = _session_with_a_model()
    with pytest.raises(TypeError):
        disco.generate(slug, "estimate", stories=_stories())
    assert provider.calls == [], "the refusal must happen before anything is paid for"


# ── staleness: invariant 1, on the artifact this decision exists for ────────────


def test_a_saved_estimate_goes_stale_when_a_topic_it_rests_on_moves():
    slug, sessions, disco, _ = _session_with_a_model()
    disco.generate(slug, "estimate")

    # A topic the estimate does not consume: the control.
    sessions.update_model(slug, out({"problem": slot(90, "explicit", "high"),
                                     "workflow": slot(60, "inferred", "high")}).model_dump_json())
    listed = ArtifactService().list(slug)
    assert listed["estimate"]["stale"] is False and listed["stories"]["stale"] is False

    # A topic both consume: the finding.
    sessions.update_model(slug, out({"problem": slot(90, "explicit", "high"),
                                     "workflow": slot(95, "explicit", "high")}).model_dump_json())
    listed = ArtifactService().list(slug)
    assert listed["estimate"]["stale"] is True, "the estimate rests on the workflow and it moved"
    assert listed["stories"]["stale"] is True
    assert listed["estimate"]["revision"] == 1, "staleness is the graph, never the revision number"


# ── the CLI verbs ───────────────────────────────────────────────────────────────


def _estimate_client() -> FakeClient:
    return FakeClient(
        json.dumps({"stories": [{"id": "S1", "title": "T"}]}),
        json.dumps({"items": [{"story_id": "S1", "title": "T", "complexity": "S",
                               "days_low": 1, "days_high": 2}]}),
    )


def test_the_estimate_verb_writes_both_files_and_still_prints_both_views():
    with _model_in_out("clitest-estimate-saves") as p:
        text = _run_app(["estimate", p.parent.name], client=_estimate_client())
        assert "=== USER STORIES ===" in text and "=== ESTIMATE" in text
        assert "Wrote user stories" in text and "Wrote estimate" in text
        assert (p.parent / "artifacts" / "stories.md").exists()
        assert (p.parent / "artifacts" / "estimate.md").exists()
        listed = ArtifactService().list(p.parent.name)
        assert listed["stories"]["revision"] == listed["estimate"]["revision"] == 1


def test_the_stories_verb_writes_the_file_like_every_other_generator():
    with _model_in_out("clitest-stories-saves") as p:
        reply = json.dumps({"stories": [{"id": "S1", "title": "T"}]})
        text = _run_app(["stories", p.parent.name], client=FakeClient(reply))
        assert "=== USER STORIES ===" in text and "Wrote user stories" in text
        assert (p.parent / "artifacts" / "stories.md").exists()
        assert ArtifactService().list(p.parent.name)["stories"]["stale"] is False
