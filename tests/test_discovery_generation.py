"""`DiscoveryService.generate()` and the paid call behind it: a paid result is never discarded (#208, #467),
a session with no model is refused before paying (#152), the estimate is a saved two-call artifact (#519),
and a provider-backed revision carries its usage (#292)."""
from __future__ import annotations

import json

import pytest
from _fakes import FakeClient, StubProvider, _model_in_out, out, run_cli, slot

from requivo.core.contracts import Brief, EstimateDraft, EstimateItem, Stories, Story
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import ArtifactWriteFailedError, RevisionConflictError
from requivo.core.persistence import RevisionRecord
from requivo.providers.errors import EngineError
from requivo.render.markdown import estimate_markdown, stories_markdown
from requivo.render.terminal import render_session_cost
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import GENERATABLE, DiscoveryService
from requivo.services.sessions import SessionService
from requivo.usage import CallRecord, record_call, track_usage

pytestmark = pytest.mark.usefixtures("workspace")

REQUEST = "a leave approval system"
_BRIEF = Brief(complexity="low", solution="S")
_PRICED = CallRecord(model="stub-model", input_tokens=1000, output_tokens=200, cache_read_tokens=50,
                     cache_write_tokens=10, rate_per_mtok=(2.0, 10.0), priced_as_of="2026-08-29")


def _seeded(sessions: SessionService, **slots) -> str:
    """A session already carrying a model (revision 1)."""
    meta = sessions.create_session(REQUEST)
    sessions.update_model(meta.slug, out({"problem": slot(80, "explicit", "high"), **slots}).model_dump_json())
    return meta.slug


def _disco(provider, sessions=None):
    sessions = sessions or SessionService()
    return sessions, DiscoveryService(provider=provider, sessions=sessions)


def _no_space(*a, **k):
    raise OSError(28, "No space left on device")


# ── a paid brief lost to a revision conflict is saved stale, not discarded (#208) ──


class _ConflictingBrief(StubProvider):
    """`generate("brief", ...)` lands a competing write on the session while "in flight"."""

    def __init__(self, sessions: SessionService, slug: str):
        super().__init__(artifacts={"brief": _BRIEF})
        self.sessions, self.slug = sessions, slug

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self.sessions.update_model(self.slug, out({"problem": slot(95, "inferred", "high")}).model_dump_json())
        return super().generate(artifact_type, model, only=only, **kwargs)


def test_a_brief_lost_to_a_revision_conflict_is_still_saved_stale_not_discarded():
    sessions = SessionService()
    slug = _seeded(sessions)
    provider = _ConflictingBrief(sessions, slug)
    with pytest.raises(RevisionConflictError) as exc_info:
        DiscoveryService(provider=provider, sessions=sessions).generate(slug, "brief")
    assert provider.generate_calls == 1  # not about retrying it

    # The model was NOT modified by the losing apply.
    assert sessions.repo.read_meta(slug).current_revision == 2
    current = sessions.load_model(slug)
    assert current.decisions == [] and current.challenges == [] and current.opportunities == []

    # The document exists anyway, flagged stale, tied to the revision it was reasoned from.
    artifacts = ArtifactService(sessions.repo)
    listed = artifacts.list(slug)
    assert listed["brief"]["stale"] is True and listed["brief"]["revision"] == 1
    assert "S" in artifacts.show(slug, "brief")

    message = str(exc_info.value)
    assert "brief" in message.lower() and "saved" in message.lower()
    assert "not" in message.lower() and "absorbed" in message.lower()
    assert f"requivo brief {slug}" in message
    assert exc_info.value.details["artifact_saved"] is True and exc_info.value.details["artifact_stale"] is True
    assert "re-apply The decision brief" not in message and ". The decision brief" in message


def test_a_brief_with_no_conflict_is_byte_identical_to_today():
    """Must-fire control: a service that always saved-and-refused would pass the test above for the wrong reason."""
    sessions = SessionService()
    slug = _seeded(sessions)
    provider = StubProvider(artifacts={"brief": _BRIEF})
    result = DiscoveryService(provider=provider, sessions=sessions).generate(slug, "brief")
    assert provider.generate_calls == 1
    assert sessions.repo.read_meta(slug).current_revision == 2  # the brief's own absorb-and-apply
    assert result.model.decisions == [] and result.status.stale is False


def test_a_conflict_plus_a_secondary_write_failure_states_both_not_just_one():
    sessions = SessionService()
    slug = _seeded(sessions)
    disco = DiscoveryService(provider=_ConflictingBrief(sessions, slug), sessions=sessions)
    disco.artifacts.save = _no_space  # type: ignore[method-assign]
    with pytest.raises(ArtifactWriteFailedError) as exc_info:
        disco.generate(slug, "brief")
    message = str(exc_info.value)
    assert "No space left" in message and "revision race" in message.lower()
    assert exc_info.value.details["revision_conflict"] is True
    assert "revision_conflict_message" in exc_info.value.details


def test_an_oserror_writing_a_generated_artifact_is_a_structured_refusal_not_a_traceback():
    """The content was paid for and produced, and only the write failed (#208)."""
    sessions = SessionService()
    slug = _seeded(sessions)
    disco = DiscoveryService(provider=StubProvider(), sessions=sessions)
    disco.artifacts.save = _no_space  # type: ignore[method-assign]
    with pytest.raises(ArtifactWriteFailedError) as exc_info:
        disco._save_generated(slug, "prd", "# PRD\n", 1)
    assert exc_info.value.code == "artifact_write_failed"
    assert exc_info.value.details["slug"] == slug and exc_info.value.details["type"] == "prd"
    assert "No space left" in exc_info.value.details["cause"]
    assert exc_info.value.details["path"].endswith("prd.md") and "prd.md" in str(exc_info.value)


# ── `start(finalize=True)` keeps the paid analyze when the brief fails (#467) ───


def test_a_failed_brief_leaves_the_analyzed_discovery_applied():
    sessions = SessionService()
    provider = StubProvider(generate_error=EngineError("the model is temporarily unavailable"))
    disco = DiscoveryService(provider=provider, sessions=sessions)
    with pytest.raises(EngineError):
        disco.start(REQUEST, finalize=True)
    assert provider.analyze_calls == 1 and provider.generate_calls == 1

    # The paid `analyze()` result is NOT thrown away -- the exception alone would pass against the old ordering.
    slug = sessions.list_sessions()[0].slug
    assert sessions.repo.read_meta(slug).current_revision == 1
    problem = sessions.load_model(slug).model["problem"]
    assert problem.completeness == 80 and problem.confidence == "explicit"

    disco._provider = StubProvider(artifacts={"brief": _BRIEF})  # retryable through the ordinary path
    assert disco.generate(slug, "brief").artifact.solution == "S"
    assert sessions.repo.read_meta(slug).current_revision == 2


def test_a_successful_finalize_still_applies_both_the_discovery_and_the_brief():
    """Must-fire control for the test above."""
    sessions = SessionService()
    provider = StubProvider(artifacts={"brief": Brief(complexity="low", solution="S", decisions=[], challenges=[], opportunities=[])})
    slug = DiscoveryService(provider=provider, sessions=sessions).start(REQUEST, finalize=True)
    assert provider.analyze_calls == 1 and provider.generate_calls == 1
    assert sessions.repo.read_meta(slug).current_revision == 2


# ── generating from a session with no model is refused before paying (#152) ────


def test_generating_from_a_session_with_no_model_is_refused_before_the_provider():
    """A `RevisionConflictError` with the remedy in it, on both entry points (#519), and the provider never reached."""
    provider = StubProvider(artifacts={"brief": _BRIEF})
    sessions, disco = _disco(provider)
    slug = sessions.create_session(REQUEST).slug
    with pytest.raises(RevisionConflictError) as exc:
        disco.generate(slug, "brief")
    with pytest.raises(RevisionConflictError):
        disco.reason(slug, "stories")
    assert provider.generate_calls == 0, "the refusal must happen before anything is paid for"
    assert "no model yet" in exc.value.message and "requivo discover" in exc.value.message
    assert exc.value.details["actual"] == 0


def test_a_session_that_does_have_a_model_still_generates():
    """The must-not-fire control: a guard that refused every session would pass the test above."""
    provider = StubProvider(artifacts={"brief": _BRIEF})
    sessions, disco = _disco(provider)
    result = disco.generate(_seeded(sessions), "brief")
    assert provider.generate_calls == 1 and result.artifact.complexity == "low"


# ── the estimate: writers, registries, one snapshot, staleness, verbs (#519) ────


def _stories() -> Stories:
    return Stories(stories=[
        Story(id="S1", title="Request leave", as_a="employee", i_want="to request leave",
              so_that="my manager can approve it", acceptance=["A request lands in the queue"],
              slots=["workflow", "actors"]),
        Story(id="S2", title="Approve | reject"),
    ])


def _draft() -> EstimateDraft:
    return EstimateDraft(
        items=[EstimateItem(story_id="S1", title="Request leave", complexity="S", days_low=1, days_high=2,
                            drives=["workflow"], note="one form"),
               EstimateItem(story_id="S2", title="Approve | reject", complexity="M", days_low=2.5, days_high=5)],
        risks=["The approval chain is not settled"])


def test_stories_markdown_renders_every_story_and_traces_it_in_human_words():
    md = stories_markdown(_stories())
    assert md.startswith("# User stories") and "## S1 — Request leave" in md
    assert "As a employee, I want to request leave, so that my manager can approve it." in md
    assert "- [ ] A request lands in the queue" in md
    # The Voice rule: a story traces to topics, and the reader sees their labels, never the ids.
    assert "Workflow / lifecycle" in md and "Actors" in md
    assert "workflow" not in md and "actors" not in md
    assert "## S2 — Approve | reject" in md and "As a ," not in md  # a title-only story: no empty template
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_estimate_markdown_renders_ranges_totals_and_spread_in_human_words():
    md = estimate_markdown(_draft(), ["workflow", "config_vs_custom"], "medium")
    assert md.startswith("# Estimate") and "**Confidence:** medium" in md
    assert "3.5–7 days" in md  # the totals are arithmetic over the items, both ends
    assert "| S1 — Request leave | S | 1–2 |" in md and "Approve \\| reject" in md  # a `|` in a title survives
    assert "one form" in md and "- The approval chain is not settled" in md
    assert "Workflow / lifecycle" in md and "Config vs customization" in md
    assert "config_vs_custom" not in md and "workflow" not in md
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_estimate_markdown_says_when_nothing_widens_the_range():
    """The must-fire pair for the spread section: a sentence, not an absent heading."""
    md = estimate_markdown(_draft(), [], "high")
    assert "No unresolved topic widens these ranges" in md and "Spread driven by" not in md


def test_a_newline_inside_a_provider_field_cannot_open_a_new_heading():
    """Found in review of #519."""
    stories = Stories(stories=[Story(id="S1", title="Request leave\n# INJECTED", i_want="a\nb")])
    draft = EstimateDraft(items=[EstimateItem(story_id="S1", title="T\n# INJECTED", complexity="S",
                                              days_low=1, days_high=1, note="n\n# INJECTED")],
                          risks=["r\n# INJECTED"])
    for md in (stories_markdown(stories), estimate_markdown(draft, [], "high")):
        assert "\n# INJECTED" not in md and "INJECTED" in md


def test_both_analyses_are_registered_everywhere_a_saveable_type_is():
    """The seven-registry table from the decision, collapsed to what this change owes (#556)."""
    assert "stories" in GENERATABLE and "estimate" in GENERATABLE
    assert ARTIFACT_FILENAMES["stories"] == "stories.md" and ARTIFACT_FILENAMES["estimate"] == "estimate.md"


class _AnalysisProvider(StubProvider):
    """Answers the two calls `estimate` makes, recording each `(type, kwargs)`."""

    def __init__(self):
        super().__init__()
        self.seen: list[tuple[str, dict]] = []
        self.stories = _stories()

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self.seen.append((artifact_type, kwargs))
        if artifact_type == "stories":
            return self.stories
        if artifact_type == "estimate":
            return _draft(), ["workflow"], "high"
        raise AssertionError(f"unexpected generate({artifact_type!r})")


def _session_with_a_model():
    provider = _AnalysisProvider()
    sessions = SessionService()
    slug = _seeded(sessions, workflow=slot(60, "inferred", "high"))
    return slug, sessions, DiscoveryService(provider=provider, sessions=sessions), provider


def test_generating_the_estimate_saves_the_stories_it_was_reasoned_against_from_one_snapshot():
    slug, sessions, disco, provider = _session_with_a_model()
    seen = []
    result = disco.generate(slug, "estimate", on_stories=seen.append)

    # Two calls, in order, and the estimate was read against the very stories object that was saved.
    assert [t for t, _ in provider.seen] == ["stories", "estimate"]
    assert provider.seen[1][1]["stories"] is provider.stories
    assert seen == [provider.stories], "the stories are handed to the caller as soon as they land"

    listed = ArtifactService().list(slug)
    assert listed["stories"]["revision"] == listed["estimate"]["revision"] == 1
    assert listed["stories"]["stale"] is False and listed["estimate"]["stale"] is False
    assert result.status.filename == "estimate.md" and result.status.revision == 1
    assert result.artifact.stories_status.filename == "stories.md" and result.artifact.stories_status.revision == 1
    assert result.artifact.draft.items[0].story_id == "S1"
    assert result.artifact.soft == ["workflow"] and result.artifact.confidence == "high"
    assert sessions.repo.load_artifact(slug, "stories.md") == stories_markdown(provider.stories)
    assert sessions.repo.load_artifact(slug, "estimate.md") == estimate_markdown(_draft(), ["workflow"], "high")


def test_generating_the_stories_alone_saves_them_like_every_other_artifact():
    slug, sessions, disco, provider = _session_with_a_model()
    result = disco.generate(slug, "stories")
    assert [t for t, _ in provider.seen] == ["stories"]
    assert result.status.filename == "stories.md" and result.status.revision == 1
    assert sessions.repo.load_artifact(slug, "stories.md") == stories_markdown(provider.stories)


def test_the_estimate_generation_refuses_a_caller_supplied_stories_draft():
    """The stories an estimate is saved against are the ones this generation reasoned and saved."""
    slug, _, disco, provider = _session_with_a_model()
    with pytest.raises(TypeError):
        disco.generate(slug, "estimate", stories=_stories())
    assert provider.seen == [], "the refusal must happen before anything is paid for"


def test_a_saved_estimate_goes_stale_when_a_topic_it_rests_on_moves():
    """Invariant 1, on the artifact this decision exists for."""
    slug, sessions, disco, _ = _session_with_a_model()
    disco.generate(slug, "estimate")
    sessions.update_model(slug, out({"problem": slot(90, "explicit", "high"), "workflow": slot(60, "inferred", "high")}).model_dump_json())
    listed = ArtifactService().list(slug)  # a topic the estimate does not consume: the control
    assert listed["estimate"]["stale"] is False and listed["stories"]["stale"] is False
    sessions.update_model(slug, out({"problem": slot(90, "explicit", "high"), "workflow": slot(95, "explicit", "high")}).model_dump_json())
    listed = ArtifactService().list(slug)  # a topic both consume: the finding
    assert listed["estimate"]["stale"] is True and listed["stories"]["stale"] is True
    assert listed["estimate"]["revision"] == 1, "staleness is the graph, never the revision number"


_STORIES_REPLY = json.dumps({"stories": [{"id": "S1", "title": "T"}]})
_ESTIMATE_REPLY = json.dumps({"items": [{"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]})


def test_the_estimate_verb_writes_both_files_and_still_prints_both_views():
    with _model_in_out("clitest-estimate-saves") as p:
        text = run_cli(["estimate", p.parent.name], client=FakeClient(_STORIES_REPLY, _ESTIMATE_REPLY))
        assert "=== USER STORIES ===" in text and "=== ESTIMATE" in text
        assert "Wrote user stories" in text and "Wrote estimate" in text
        assert (p.parent / "artifacts" / "stories.md").exists() and (p.parent / "artifacts" / "estimate.md").exists()
        listed = ArtifactService().list(p.parent.name)
        assert listed["stories"]["revision"] == listed["estimate"]["revision"] == 1


def test_the_stories_verb_writes_the_file_like_every_other_generator():
    with _model_in_out("clitest-stories-saves") as p:
        text = run_cli(["stories", p.parent.name], client=FakeClient(_STORIES_REPLY))
        assert "=== USER STORIES ===" in text and "Wrote user stories" in text
        assert (p.parent / "artifacts" / "stories.md").exists()
        assert ArtifactService().list(p.parent.name)["stories"]["stale"] is False


# ── per-call usage stamped into revision provenance (#292) ─────────────────────


class _SpendingProvider(StubProvider):
    """`analyze()` records one `CallRecord` against whatever ledger is active."""

    def __init__(self, *records: CallRecord):
        super().__init__()
        self._records = list(records)

    def analyze(self, request, **kwargs):
        record_call(self._records.pop(0))
        return super().analyze(request, **kwargs)


def _discovered(record: CallRecord, *, tracked: bool = True):
    """A session discovered through a provider that billed `record`; returns its revisions."""
    sessions = SessionService()
    slug = sessions.create_session(REQUEST).slug
    disco = DiscoveryService(provider=_SpendingProvider(record), sessions=sessions)
    if tracked:
        with track_usage():
            disco.run_discovery(slug, surface="test")
    else:
        disco.run_discovery(slug, surface="test")
    return slug, sessions.repo.read_meta(slug).revisions


def test_a_provider_backed_apply_stamps_token_and_rate_provenance_onto_its_revision():
    """The span measured is "however many calls this operation made", not "one call" (#467)."""
    _, revisions = _discovered(_PRICED)
    rec = revisions[-1]
    assert (rec.usage_input_tokens, rec.usage_output_tokens) == (1000, 200)
    assert (rec.usage_cache_read_tokens, rec.usage_cache_write_tokens) == (50, 10)
    assert tuple(rec.usage_rate_per_mtok) == (2.0, 10.0) and rec.usage_priced_as_of == "2026-08-29"


def test_a_deterministic_apply_carries_no_usage_provenance(capsys):
    """Must-fire control: a `model apply` that never touches a provider, and `render_session_cost` prints nothing for it (invariant 6)."""
    sessions = SessionService()
    revisions = sessions.repo.read_meta(_seeded(sessions)).revisions
    rec = revisions[-1]
    assert (rec.usage_input_tokens, rec.usage_output_tokens, rec.usage_cache_read_tokens,
            rec.usage_cache_write_tokens, rec.usage_rate_per_mtok, rec.usage_priced_as_of) == (None,) * 6
    render_session_cost(revisions)
    assert capsys.readouterr().out == ""


def test_a_provider_call_made_with_no_active_ledger_still_leaves_usage_absent():
    """The offline suite's ordinary shape: a provider call with no `track_usage()` scope open."""
    _, revisions = _discovered(CallRecord(model="stub-model", input_tokens=1000, output_tokens=200), tracked=False)
    assert revisions[-1].usage_input_tokens is None and revisions[-1].usage_rate_per_mtok is None


def test_a_revision_record_with_no_usage_keys_round_trips_unchanged():
    """An old session.json, written before #292, carries no usage_* keys at all."""
    rec = RevisionRecord.model_validate_json(
        '{"revision": 1, "created_at": "2026-01-01T00:00:00Z", "provider": "anthropic", '
        '"model_name": "claude-sonnet-5", "surface": "cli-discover", "model_hash": "sha256:abc"}')
    assert rec.usage_input_tokens is None and rec.usage_rate_per_mtok is None
    assert RevisionRecord.model_validate_json(rec.model_dump_json()).usage_input_tokens is None


def test_requivo_status_prints_the_cumulative_cost_line():
    """`render_session_cost` over priced revisions, reached through `requivo status`."""
    slug, revisions = _discovered(CallRecord(model="stub-model", input_tokens=1000, output_tokens=200,
                                             rate_per_mtok=(2.0, 10.0), priced_as_of="2026-08-29"))
    render_session_cost(revisions)  # smoke: must not raise
    printed = run_cli(["status", slug])
    assert "SESSION COST" in printed and ("1,000" in printed or "1000" in printed)


def test_render_session_cost_reads_its_arithmetic_from_usage_py_and_nowhere_else(capsys):
    """#389: `render_session_cost` used to re-implement `UsageLedger.cost_usd()` locally."""
    render_session_cost([RevisionRecord(
        revision=1, created_at="2026-01-01T00:00:00Z",
        usage_input_tokens=1_000_000, usage_output_tokens=1_000_000,
        usage_cache_read_tokens=1_000_000, usage_cache_write_tokens=1_000_000,
        usage_rate_per_mtok=(2.0, 10.0), usage_priced_as_of="2026-01-01")])
    # 2.0 input + 0.2 cache read (0.1x) + 2.5 cache write (1.25x) + 10.0 output = 14.700
    assert "~$14.700" in capsys.readouterr().out


def test_render_session_cost_does_not_stamp_a_dangling_separator_for_an_empty_priced_as_of(capsys):
    """Found in review (#388/#389): the old code filtered `as_of` on truthiness."""
    common = dict(usage_cache_read_tokens=0, usage_cache_write_tokens=0, usage_rate_per_mtok=(2.0, 10.0))
    dated = RevisionRecord(revision=1, created_at="2026-01-01T00:00:00Z", usage_input_tokens=1000,
                           usage_output_tokens=200, usage_priced_as_of="2026-01-01", **common)
    undated = RevisionRecord(revision=2, created_at="2026-01-02T00:00:00Z", previous_revision=1,
                             usage_input_tokens=500, usage_output_tokens=100, usage_priced_as_of="", **common)
    render_session_cost([dated, undated])
    text = capsys.readouterr().out
    assert "rates as of 2026-01-01" in text
    assert "· " not in text and " ·" not in text, text
