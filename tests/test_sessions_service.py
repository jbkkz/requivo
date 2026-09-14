"""`SessionService`/`ArtifactService`: proposal validation, the store, the apply pipeline and its
dependency-graph staleness, re-scoping context cards (#168), the pre-flight discovery guards
(#152, #421, #133), and the artifact/impact read paths. Split by #555 from `test_sessions.py`;
`test_discovery_provider_seam.py` and `test_session_format_compat.py` cover the rest of it.

All offline -- no API, no provider. A temp workspace via REQUIVO_WORKSPACE (conftest.py).
"""
from __future__ import annotations

import json
import threading

import pytest

from conftest import CountingProvider as _CountingProvider
from conftest import FakeProvider as _FakeProvider
from conftest import RacingClient as _RacingClient
from conftest import full_model as _full_model
from conftest import slot as _slot
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput
from requivo.core.errors import MissingRequiredSlotError, RequivoError, SessionNotFoundError, UnknownSlotError
from requivo.core.validation import validate_proposal
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService

# ── validation ────────────────────────────────────────────────────────────────


def test_validate_accepts_a_complete_model():
    out = validate_proposal(_full_model())
    assert isinstance(out, EngineOutput)


def test_validate_rejects_unknown_slot():
    bad = _full_model()
    bad["model"]["not_a_real_slot"] = _slot()
    with pytest.raises(UnknownSlotError) as e:
        validate_proposal(bad)
    assert e.value.code == "unknown_slot"
    assert "not_a_real_slot" in e.value.details["slots"]


def test_validate_rejects_missing_required_slot():
    partial = _full_model()
    a_required = next(iter(partial["model"]))
    del partial["model"][a_required]
    with pytest.raises(MissingRequiredSlotError) as e:
        validate_proposal(partial)
    assert e.value.code == "missing_required_slot"
    assert a_required in e.value.details["slots"]


def test_validate_rejects_a_complete_model_with_no_objective():
    """Completeness is the full slot set *and* an objective. The provider's retry hook required both;
    the deterministic path required only the slots, so the same model was complete when Anthropic
    produced it and complete-enough when Claude Code applied it — and a session of fifteen filled
    slots with nothing naming what they are for renders as a blank heading in every view. Both
    boundaries now read the one definition (`completeness_gap`)."""
    from requivo.core.errors import InvalidModelError

    with pytest.raises(InvalidModelError) as e:
        validate_proposal({**_full_model(), "summary": {"objective": "   "}})
    assert e.value.path == "summary.objective"
    # A projection is a different claim — it never promised completeness in the first place.
    validate_proposal({**_full_model(), "summary": {}}, require_complete=False)


def test_validate_allows_partial_when_not_required():
    partial = _full_model()
    del partial["model"][next(iter(partial["model"]))]
    out = validate_proposal(partial, require_complete=False)  # no raise
    assert isinstance(out, EngineOutput)


def test_validate_rejects_non_json_string():
    with pytest.raises(RequivoError) as e:
        validate_proposal("{not json")
    assert e.value.code == "invalid_model"


def test_error_to_dict_is_serializable():
    err = UnknownSlotError("bad", path="model.x", details={"slots": ["x"]})
    d = err.to_dict()
    assert d == {"code": "unknown_slot", "message": "bad", "path": "model.x", "details": {"slots": ["x"]}}
    json.dumps(d)  # must round-trip


# ── store: revisions + artifacts ────────────────────────────────────────────────


def test_store_creates_session_and_revisions(workspace):
    store.create_session("s1", "Build a leave system.", provider="claude-code")
    assert store.read_meta("s1").current_revision == 0
    out = EngineOutput.model_validate(_full_model())
    rev1, _ = store.save_revision("s1", out)
    rev2, meta = store.save_revision("s1", out)
    assert (rev1, rev2, meta.current_revision) == (1, 2, 2)
    d = store.canonical_dir("s1")
    assert (d / "model.json").exists()
    assert (d / "revisions" / "0001-model.json").exists()
    assert (d / "revisions" / "0002-model.json").exists()
    assert store.list_session_slugs() == ["s1"]


def test_store_migrate_session_rejects_a_future_format(workspace):
    from requivo.core.errors import InvalidSessionError
    with pytest.raises(InvalidSessionError):
        store.migrate_session({"format_version": 999, "session_id": "x", "slug": "s",
                               "created_at": "t", "updated_at": "t"})


# ── services: the apply pipeline, and its dependency-graph staleness ─────────────


def _with_reasoning(model: dict) -> dict:
    """A full model that also carries baked-in reasoning: a decision on `permissions`, a challenge
    contesting `workflow`."""
    model["decisions"] = [{"decision": "Draft-first", "derived_from": ["permissions"]}]
    model["challenges"] = [{
        "headline": "Archive vs delete", "premise": "p", "alternative": "a",
        "consequence": "c", "recommendation": "r", "contests": ["workflow"],
    }]
    return model


def test_session_service_create_and_apply(workspace):
    svc = SessionService()
    meta = svc.create_session("Build a leave approval system.", slug="leave", provider="claude-code")
    assert meta.slug == "leave" and meta.current_revision == 0

    # A high-impact slot left unconfirmed must block readiness.
    result = svc.update_model("leave", _full_model(**{"problem": _slot(0, "empty", "high")}))
    assert result.status == "applied"
    assert result.revision == 1
    assert set(result.changed_slots)  # every slot present counts as changed on the first apply
    assert result.readiness.ready is False
    assert "problem" in result.readiness.blocking_slots


def test_apply_diff_reports_changed_slots_and_readiness(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    # First model: everything empty.
    svc.update_model("s", _full_model())
    # Second: fill one slot explicitly → it should be the changed slot.
    changed_model = _full_model(**{"problem": _slot(90, "explicit", "high", "A real problem")})
    result = svc.update_model("s", changed_model)
    assert result.revision == 2
    assert "problem" in result.changed_slots


def test_diff_does_not_write(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    before = store.read_meta("s").current_revision
    plan = svc.diff("s", _full_model(**{"problem": _slot(90, "explicit", "high", "X")}))
    assert plan.status == "planned"
    assert store.read_meta("s").current_revision == before  # unchanged — no write


def test_apply_flags_generated_artifact_stale(workspace):
    svc = SessionService()
    art = ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    art.save("s", "prd", "# PRD\n", source_revision=1)  # generated at revision 1
    assert art.list("s")["prd"]["stale"] is False
    # Change a slot the PRD consumes (workflow) → PRD goes stale.
    result = svc.update_model("s", _full_model(**{"workflow": _slot(80, "explicit", "high", "new flow")}))
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


def test_propagate_reports_challenges_via_contests():
    from requivo.core.dependencies import propagate
    out = EngineOutput.model_validate(_with_reasoning(_full_model()))
    hit = propagate(out, ["workflow"])
    assert [c.headline for c in hit.challenges] == ["Archive vs delete"]
    assert hit.reasoning_hit is True
    # A change that touches neither derived_from nor contests unseats no reasoning.
    miss = propagate(out, ["success_metrics"])
    assert not miss.challenges and not miss.decisions and not miss.reasoning_hit


def test_apply_flags_assessment_stale_when_reasoning_is_unseated(workspace):
    svc = SessionService()
    art = ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_reasoning(_full_model()))
    art.save("s", "brief", "# Assessment\n", source_revision=1)  # the saved assessment renders that reasoning
    assert art.list("s")["brief"]["stale"] is False

    # Change `workflow` — a challenge contests it → the assessment on disk no longer holds.
    changed = _with_reasoning(_full_model())
    changed["model"]["workflow"] = _slot(80, "explicit", "high", "new flow")
    result = svc.update_model("s", changed)
    assert "Archive vs delete" in result.invalidated_challenges
    assert "brief" in result.stale_artifacts
    assert art.list("s")["brief"]["stale"] is True
    # The decision (on `permissions`) was untouched, so it is not reported.
    assert result.invalidated_decisions == []
    assert "invalidated_challenges" in result.to_dict()


def test_changing_the_problem_marks_a_saved_assessment_stale(workspace):
    # The assessment used to sit outside the artifact→slot map entirely, on the grounds that it was the
    # live analysis layer rather than a deliverable. Once it is saved to disk that stops holding: an
    # assessment whose problem statement has since been rewritten is not "fresh", it is out of date.
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # no decisions, no challenges — nothing to unseat
    art.save("s", "brief", "# Assessment\n", source_revision=1)
    assert art.list("s")["brief"]["stale"] is False

    result = svc.update_model("s", _full_model(**{"problem": _slot(80, "explicit", "high", "reframed")}))
    assert "brief" in result.stale_artifacts
    assert art.list("s")["brief"]["stale"] is True


def test_artifact_cannot_be_recorded_against_an_impossible_revision(workspace):
    # Provenance that cannot be true is worse than none: every freshness answer downstream is read off
    # this number, so a revision from the future is refused rather than stored.
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # session is at revision 1
    with pytest.raises(RequivoError) as ei:
        art.save("s", "prd", "# PRD\n", source_revision=999)
    assert ei.value.code == "artifact_revision_out_of_range"
    with pytest.raises(RequivoError):
        art.save("s", "prd", "# PRD\n", source_revision=0)
    assert "prd" not in art.list("s")            # nothing was recorded


# ── rescoping context cards (#168) ──────────────────────────────────────────────


def test_rescope_before_any_model_only_mutates_metadata(workspace):
    """Before any turn has reasoned against the old selection, there is no provenance to keep honest —
    nothing describes a model produced under it. Revision 0 (no model yet) so a re-scope here is a
    plain metadata write: no revision, no revisions-log entry."""
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])

    result = svc.rescope("s", context_cards=["event-ops"])

    assert result.changed is True
    assert result.revision == 0
    assert svc.cards("s") == ["event-ops"]
    meta = store.read_meta("s")
    assert meta.current_revision == 0
    assert meta.revisions == []


def test_rescope_after_a_model_records_a_new_revision_with_unchanged_content(workspace):
    """Once a model exists, every revision already on disk was reasoned under the *old* selection.
    A re-scope is recorded as its own revision — an unchanged model, a provenance entry naming the
    surface as a context switch rather than a reasoning turn — so the history shows exactly where
    the selection changed, instead of silently rewriting what revision 1 was reasoned against."""
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])
    svc.update_model("s", _full_model())          # revision 1, reasoned under b2b-platform

    result = svc.rescope("s", context_cards=["event-ops"])

    assert result.changed is True
    assert result.revision == 2
    assert result.previous_context_cards == ["b2b-platform"]
    assert result.context_cards == ["event-ops"]
    meta = store.read_meta("s")
    assert meta.current_revision == 2
    assert meta.context_cards == ["event-ops"]
    assert len(meta.revisions) == 2
    new_rec = meta.revisions[-1]
    assert new_rec.revision == 2
    assert new_rec.surface == "session-rescope"
    # the model itself did not move — same content, same hash as the revision it succeeds
    assert new_rec.model_hash == meta.revisions[0].model_hash
    assert store.load_revision_model("s", 2).model_dump() == store.load_revision_model("s", 1).model_dump()


def test_rescope_resolves_and_normalizes_cards_like_creation(workspace):
    """Invariant 14's second door: `create_session` resolves the caller's selection rather than
    trusting it, and a re-scope is a second entrance onto the same persisted value — an unknown name
    must be refused here too, not recorded and discovered on the next turn."""
    from requivo.core.errors import UnknownContextCardError

    svc = SessionService()
    svc.create_session("Something.", slug="s")

    with pytest.raises(UnknownContextCardError):
        svc.rescope("s", context_cards=["made-up"])
    assert svc.cards("s") is None  # refused before anything was written


def test_rescope_to_the_current_selection_is_a_no_op(workspace):
    """Re-scoping to the selection a session already has changes nothing — order aside, since the
    selection is a set. No new revision, no rewritten metadata: repeating the command is safe."""
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform", "event-ops"])
    svc.update_model("s", _full_model())           # revision 1

    result = svc.rescope("s", context_cards=["event-ops", "b2b-platform"])  # same set, other order

    assert result.changed is False
    assert result.revision == 1
    meta = store.read_meta("s")
    assert meta.current_revision == 1
    assert len(meta.revisions) == 1


def test_rescope_to_every_card_resets_the_selection_to_none(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])

    result = svc.rescope("s", context_cards=None)

    assert result.changed is True
    assert result.context_cards is None
    assert svc.cards("s") is None


def test_rescope_does_not_mark_existing_artifacts_stale(workspace):
    """Question 2, decided: context is not a fifth kind of dependency edge. An artifact already on
    disk still faithfully describes the model it was generated from — nothing in `ARTIFACT_SLOTS` or
    `REASONING_CONSUMERS` names context as an input, and the model itself has not moved."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])
    svc.update_model("s", _full_model())                       # revision 1
    art.save("s", "prd", "# PRD\n", source_revision=1)

    svc.rescope("s", context_cards=["event-ops"])

    assert art.list("s")["prd"]["stale"] is False


def test_a_model_change_still_marks_the_same_artifact_stale(workspace):
    """The positive control for the assertion above: proves the harness can observe staleness at
    all, on the very same artifact, so "rescope leaves it fresh" is not passing on a fixture that
    can never turn STALE regardless of what runs."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())                       # revision 1
    art.save("s", "prd", "# PRD\n", source_revision=1)

    svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "new flow")}))

    assert art.list("s")["prd"]["stale"] is True


def test_rescope_does_not_re_run_anything_the_next_snapshot_reads_the_new_cards(workspace):
    """Question 3, decided: a re-scope re-runs nothing. It only changes what the *next* provider call
    reasons against — proven here without a provider at all, by reading the same snapshot every
    discovery call reads from."""
    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])
    svc.update_model("s", _full_model())

    svc.rescope("s", context_cards=["event-ops"])

    assert svc.snapshot("s").context_cards == ["event-ops"]


def test_a_rescoped_session_with_a_model_still_passes_its_own_integrity_check(workspace):
    """The duplicated revision this produces is a real revision, not a shortcut: `check_session_dir`
    is the same anti-tampering pass `session verify` runs, and it must find nothing wrong with one."""
    from requivo.core.integrity import check_session_dir

    svc = SessionService()
    svc.create_session("Something.", slug="s", context_cards=["b2b-platform"])
    svc.update_model("s", _full_model())
    svc.rescope("s", context_cards=["event-ops"])

    problems = check_session_dir(store.canonical_dir("s"), expected_slug="s")
    assert problems == []


def test_rescope_refuses_a_session_that_does_not_exist(workspace):
    with pytest.raises(SessionNotFoundError):
        SessionService().rescope("ghost", context_cards=["event-ops"])


def test_the_same_request_under_different_cards_is_a_different_session(workspace):
    """Context cards are provenance, not decoration: the same request read against `b2b-platform` and
    against `event-ops` gets different impact estimates, so different questions. Creation keyed on the
    request alone, so the second call silently handed back the first session — with a card selection
    the caller had not asked for and no way to notice."""
    svc = SessionService()
    first = svc.create_session("Same request.", context_cards=["b2b-platform"])
    again = svc.create_session("Same request.", context_cards=["b2b-platform"])
    other = svc.create_session("Same request.", context_cards=["event-ops"])

    assert again.slug == first.slug                       # same discovery: still idempotent
    assert other.slug != first.slug
    assert svc.cards(other.slug) == ["event-ops"]         # and it got the cards it asked for


# ── the pre-flight discovery guards (#152, #421, #133) ───────────────────────────


def test_a_fresh_discovery_refuses_to_replace_a_model_that_already_exists(workspace):
    """Session creation is idempotent, so re-running `discover` on the same request lands on the same
    session — and used to overwrite whatever it held, replacing a model refined over several turns
    with a naive first-turn one. A conflict is recoverable; a silent replacement is not."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    disco = DiscoveryService(_FakeProvider())
    slug = disco.start("A leave approval system.", slug="dup")
    SessionService().update_model(slug, _full_model(**{"workflow": _slot(90, "explicit", "high", "kept")}))

    with pytest.raises(RevisionConflictError):
        disco.start("A leave approval system.", slug="dup")
    assert SessionService().load_model(slug).model["workflow"].value == "kept"


def test_run_discovery_refuses_a_session_that_already_has_a_model(workspace):
    """`run_discovery` reasons from the request alone — it never sees the current model — so on a
refined session it does not improve the understanding, it discards it. The optimistic lock does
not catch this: the call reads revision N and writes against revision N, so the precondition is
satisfied while the content is a regression."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "refined")}))

    provider = _CountingProvider()
    with pytest.raises(RevisionConflictError) as e:
        DiscoveryService(provider).run_discovery("s")

    assert e.value.details["actual"] == 1 and e.value.details["expected"] == 0
    assert provider.calls == 0                                    # refused before the paid call
    assert svc.load_model("s").model["workflow"].value == "refined"


@pytest.mark.parametrize("call", [
    lambda d: d.generate("s", "brief"),
    lambda d: d.generate("s", "prd"),
    lambda d: d.reason("s", "stories"),
], ids=["generate-brief", "generate-prd", "reason-stories"])
def test_generation_refuses_a_session_that_has_no_model_yet(workspace, call):
    """The mirror of the rule above, and it was missing (#152). `SessionSnapshot.model` is `None` before
the first model — the field says so — and `generate`/`reason` unpacked it and handed it to the
provider unchecked. Nothing was lost and nothing was spent: every generator builds its user
message as `out.model_dump_json(...)`, so it died assembling the prompt, before the client was
touched."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    SessionService().create_session("Something.", slug="s")     # created, never analysed
    provider = _CountingProvider()

    with pytest.raises(RevisionConflictError) as e:
        call(DiscoveryService(provider))

    assert e.value.details["actual"] == 0 and e.value.details["expected"] == 1
    assert provider.calls == 0                                    # refused before reaching the provider
    assert "discover" in str(e.value)                             # the refusal names the remedy


def test_answer_refuses_a_session_that_has_no_model_yet(workspace):
    """`answer()` is the one write verb `_require_a_model` did not cover (#421) — the mirror of #152,
one write verb over."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    SessionService().create_session("Something.", slug="s")     # created, never analysed
    provider = _CountingProvider()

    with pytest.raises(RevisionConflictError) as e:
        DiscoveryService(provider).answer("s", "here are my answers")

    assert e.value.details["actual"] == 0 and e.value.details["expected"] == 1
    assert provider.calls == 0                                    # refused before reaching the provider
    assert "discover" in str(e.value)                             # the refusal names the remedy
    # The co-requisite half of #421: before this fix the remedy text itself suggested `requivo answer`
    # "if a discovery is in progress" — i.e. it routed a reader straight back into the ungated path.
    # Since #202 an interrupted discovery lands at revision 1, so `answer` is never the right verb at
    # revision 0; naming it here would be self-contradictory the moment this very gate exists.
    assert "requivo answer" not in str(e.value)


def test_a_repeat_discovery_is_refused_before_the_provider_is_paid(workspace):
    """Same rule, the other entry point. `start()` used to reason first and discover the conflict
    afterwards, so an accidental re-run bought a discovery turn — and, when finalizing, an assessment
    too — purely to throw both away."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    provider = _CountingProvider()
    disco = DiscoveryService(provider)
    disco.start("A leave approval system.", slug="dup")
    assert provider.calls == 1

    with pytest.raises(RevisionConflictError):
        disco.start("A leave approval system.", slug="dup")
    assert provider.calls == 1                                    # the second run never reasoned


# ── artifact freshness, impact, and locked reads ──────────────────────────────────


def test_the_artifact_service_defaults_to_the_session_service_s_storage(workspace):
    """Two services, one backing. On files the default and the injected repository resolve to the same
    workspace, so a split was invisible — but `DiscoveryService(sessions=SessionService(postgres))`
    sent sessions to Postgres and artifacts to the local filesystem, and every call succeeded. This is
    the shape an external deployment constructs, so the default has to follow the session service."""
    from requivo.services.discovery import DiscoveryService
    from requivo.services.repository import FileSessionRepository

    repo = FileSessionRepository()
    disco = DiscoveryService(_FakeProvider(), sessions=SessionService(repo))
    assert disco.artifacts.repo is repo
    assert DiscoveryService(_FakeProvider(), repo=repo).sessions.repo is repo


def test_the_service_refuses_a_context_card_that_does_not_exist(workspace):
    """The CLI and the Web both resolve cards before they get here, which made the service look safe.
    It is not a boundary until it holds the rule itself: an unknown card recorded on a session is read
    back by every later turn, and an empty resolved selection means *every* card — so a bad name
    silently widens the context instead of narrowing it. An external consumer calls exactly this layer."""
    from requivo.core.errors import UnknownContextCardError

    with pytest.raises(UnknownContextCardError):
        SessionService().create_session("Something.", context_cards=["made-up"])
    assert SessionService().create_session(
        "Something.", context_cards=["b2b-platform"]).context_cards == ["b2b-platform"]


def test_impact_reports_what_a_named_slot_reaches(workspace):
    """`SessionService.impact` -- the XS addition #425's HTTP API `/impact` route is built on -- is
    exactly `propagate(load_model(slug), resolve_slots(...))` behind the service seam: a decision
    derived from the named slot, and a challenge contesting it, both come back."""
    from requivo.core.contracts import Challenge, DesignDecision

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    model = EngineOutput.model_validate({
        **_full_model(**{"workflow": _slot(80, "explicit", "high")}),
        "decisions": [DesignDecision(decision="Draft-first invoices",
                                     derived_from=["workflow"]).model_dump()],
        "challenges": [Challenge(headline="Invoice at signature", premise="p", alternative="a",
                                 consequence="c", recommendation="r",
                                 contests=["workflow"]).model_dump()],
    })
    svc.update_model("s", model.model_dump())

    report = svc.impact("s", ["workflow"])
    assert any(d.decision == "Draft-first invoices" for d in report.decisions)
    assert any(c.headline == "Invoice at signature" for c in report.challenges)
    assert report.to_dict()["decisions"][0]["decision"] == "Draft-first invoices"


def test_impact_refuses_an_unknown_slot_naming_it_in_details(workspace):
    """Unlike the CLI's own `_cmd_impact`, which prints a warning for an unmatched token and keeps
    rendering whatever did match, the service raises -- a caller over HTTP gets one structured
    refusal rather than a partial report with no signal that something was left out."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    with pytest.raises(UnknownSlotError) as e:
        svc.impact("s", ["not-a-real-slot"])
    assert e.value.details["unmatched"] == ["not-a-real-slot"]


def test_impact_with_no_slots_named_is_an_empty_report_not_a_refusal(workspace):
    """An empty list is "no slots named", the same reading `resolve_slots([])` already gives it
    (`normalize_tokens`'s own docstring) -- not the same thing as a token that matched nothing."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    report = svc.impact("s", [])
    assert report.empty


def test_show_with_status_reads_content_and_freshness_together(workspace):
    """`ArtifactService.show_with_status` -- the coherent read the HTTP API's artifact envelope needs
    (invariant 12, one layer over from the provider-snapshot case it was written for): both facts
    come from one locked read rather than two separate calls that could disagree."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    art.save("s", "brief", "# Brief\n", source_revision=1)

    content, row = art.show_with_status("s", "brief")
    assert content == "# Brief\n"
    assert row == {"revision": 1, "filename": "solution-assessment.md",
                   "updated_at": row["updated_at"], "stale": False}


def test_show_with_status_404s_when_nothing_was_ever_saved(workspace):
    from requivo.core.errors import SessionNotFoundError

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    with pytest.raises(SessionNotFoundError):
        art.show_with_status("s", "brief")


def test_show_with_status_is_not_interleaved_by_a_concurrent_save(workspace):
    """The must-fire proof behind the claim in `show_with_status`'s own docstring (found in review,
    #425): a single-threaded test that only checks content and status agree when nothing else is
    writing would pass identically whether the lock were there or not. This drives a real second
    thread through `save()` while the read is paused *inside* the held lock, and shows it is
    genuinely blocked -- not merely usually-fast-enough -- until the read completes."""

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    art.save("s", "brief", "V1", source_revision=1)

    reader_inside_lock = threading.Event()
    reader_may_finish = threading.Event()
    real_load_artifact = art.repo.load_artifact

    def paused_load_artifact(slug, filename):
        content = real_load_artifact(slug, filename)
        reader_inside_lock.set()
        assert reader_may_finish.wait(timeout=5), "the writer thread below never released the reader"
        return content

    art.repo.load_artifact = paused_load_artifact

    result: dict = {}

    def do_show():
        result["content"], result["row"] = art.show_with_status("s", "brief")

    reader = threading.Thread(target=do_show, daemon=True)
    reader.start()
    assert reader_inside_lock.wait(timeout=5), "the reader never reached its locked read"

    writer_finished = threading.Event()

    def do_save():
        art.save("s", "brief", "V2", source_revision=1)
        writer_finished.set()

    writer = threading.Thread(target=do_save, daemon=True)
    writer.start()

    # Must genuinely be waiting on the reader's held lock, not racing ahead of it -- the same
    # "not merely usually fast enough" assertion `test_persistence_lock.py`'s own lock tests make.
    assert not writer_finished.wait(timeout=0.2), (
        "a concurrent save() proceeded while show_with_status still held the lock -- reverting to "
        "two separate unlocked calls (show() then list()) would let this assertion fail")

    reader_may_finish.set()
    reader.join(timeout=5)
    assert writer_finished.wait(timeout=5), "the writer never finished once the reader released"

    # Because the read was atomic, content and status describe the SAME save -- the first one,
    # since the reader's locked read ran to completion before the writer's save() could start.
    assert result["content"] == "V1"
    assert result["row"]["revision"] == 1


def test_an_artifact_is_refused_when_its_freshness_cannot_be_established(workspace):
    """`False` is not "I don't know" — it is the claim that the artifact is up to date. It was being
    returned for a session whose history could not be read at all, which is the one case where the
    answer is genuinely unavailable. Refusing the save is the honest outcome: the provenance it would
    record cannot be verified."""
    from requivo.core.errors import RequivoError

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())                                   # revision 1
    svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "moved")}))  # 2
    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()  # the history is now a lie

    with pytest.raises(RequivoError) as e:
        art.save("s", "prd", "# PRD\n", source_revision=1)
    assert e.value.code == "unreadable_source_revision"
    assert "prd" not in art.list("s")                                      # nothing was recorded


def test_a_first_discovery_that_races_a_concurrent_write_conflicts(workspace):
    """`run_discovery` reasons from revision N and applies; the call takes minutes, so it captures the
    revision it read and holds the write to it — the same precondition every other provider-backed
    operation carries. Without it the concurrent model was replaced by one reasoned from the older
    state, which is exactly the case optimistic locking exists for."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")

    def concurrent_apply():
        svc.update_model("s", _full_model(**{"risks": _slot(70, "explicit", "high", "rollout risk")}))

    reply = {**_full_model(), "summary": {"objective": "A leave approval system"}}
    disco = DiscoveryService(client=_RacingClient(json.dumps(reply), concurrent_apply))
    with pytest.raises(RevisionConflictError):
        disco.run_discovery("s")
    assert svc.load_model("s").model["risks"].value == "rollout risk"


def test_an_artifact_generated_from_a_superseded_revision_is_born_stale(workspace):
    """Invariant 2: a generation carries the revision it read. Provider calls take seconds to minutes
and the session can move underneath them, so `current_revision` is captured before the call and
passed as `expected_revision` on any apply and as `source_revision` on the artifact write. Saving
against an older revision stays legal — `ArtifactService.save` then computes freshness against
the current model rather than assuming it, which is what this test pins. See #286."""
    from requivo.services.discovery import DiscoveryService

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # revision 1 — the PRD's actual source

    def concurrent_answer():
        svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "new flow")}))

    prd_reply = json.dumps({"title": "PRD", "problem": "Approvals are lost in email."})
    DiscoveryService(client=_RacingClient(prd_reply, concurrent_answer)).generate("s", "prd")

    saved = art.list("s")["prd"]
    assert saved["revision"] == 1        # recorded against the revision it was written from…
    assert saved["stale"] is True        # …and the workflow change it never saw makes it stale


# ── misc ──────────────────────────────────────────────────────────────────────


def test_update_missing_session_raises(workspace):
    with pytest.raises(SessionNotFoundError):
        SessionService().update_model("ghost", _full_model())


def test_a_legacy_session_is_named_in_the_error_rather_than_migrated_behind_your_back(workspace):
    """`out/` was the store until 0.8.0, and until 0.9.8 every read silently fell back to it and every
mutation migrated one in place. That kept old sessions working without the user knowing, which is
also what was wrong with it: the fallback ran on every read of every session for a layout nothing
has written in two minor versions, and "where does this session live?" had two answers throughout
the code."""
    legacy = store.legacy_dir("old")
    legacy.mkdir(parents=True)
    (legacy / "model.json").write_text(json.dumps(_full_model()))
    (legacy / "request.txt").write_text("Legacy request.")
    (legacy / "prd.md").write_text("# Legacy PRD\n")

    svc = SessionService()
    assert not svc.exists("old")
    with pytest.raises(SessionNotFoundError) as e:
        svc.load_model("old")
    assert e.value.details.get("legacy") is True
    assert "session migrate" in str(e.value)

    # And the explicit migration is intact: the model becomes revision 1, artifacts come with it,
    # and the originals are left where they were.
    store.migrate_legacy("old")
    assert store.session_exists("old")
    assert (legacy / "model.json").exists()
    result = svc.update_model("old", _full_model(**{"problem": _slot(90, "explicit", "high", "P")}))
    assert result.revision == 2
    d = store.canonical_dir("old")
    assert (d / "revisions" / "0001-model.json").exists()
    assert (d / "artifacts" / "prd.md").read_text(encoding="utf-8") == "# Legacy PRD\n"
