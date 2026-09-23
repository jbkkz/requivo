"""`SessionService`/`ArtifactService`: validation, the store, the apply pipeline and its staleness, re-scoping
(#168), the pre-flight discovery guards (#152, #421, #133), the read paths, and artifact provenance (#6)."""
from __future__ import annotations

import json
import threading

import pytest
from _fakes import full_model, slot

from conftest import CountingProvider, FakeProvider, RacingClient
from requivo.core import errors as E
from requivo.core import persistence as store
from requivo.core.contracts import Brief, Challenge, DesignDecision, EngineOutput, Exclusion, Threshold
from requivo.core.dependencies import propagate
from requivo.core.validation import validate_proposal
from requivo.services.artifacts import ArtifactService, UnstatedSourceRevisionError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

MOVED = full_model(workflow=slot(90, "explicit", "high", "new flow"))
REFRAMED = full_model(problem=slot(80, "explicit", "high", "reframed"))
CHALLENGE = {"headline": "Archive vs delete", "premise": "p", "alternative": "a", "consequence": "c",
             "recommendation": "r", "contests": ["workflow"]}


def _session(*models, slug="s", cards=None) -> SessionService:
    """A session at revision `len(models)`, each model applied in order."""
    svc = SessionService()
    svc.create_session("Something.", slug=slug, context_cards=cards)
    for m in models:
        svc.update_model(slug, m)
    return svc


def _with_reasoning(**slots) -> dict:
    """A full model whose one decision rests on `permissions` and whose one challenge contests `workflow`."""
    return {**full_model(**slots), "decisions": [{"decision": "Draft-first", "derived_from": ["permissions"]}],
            "challenges": [CHALLENGE]}


@pytest.fixture
def art() -> ArtifactService:
    return ArtifactService()


def _revision_file(slug: str, revision: int):
    return store.canonical_dir(slug) / "revisions" / f"{revision:04d}-model.json"


# ── validation ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("damage, error, code, named", [
    (lambda m: m["model"].__setitem__("not_a_real_slot", slot()), E.UnknownSlotError, "unknown_slot", "not_a_real_slot"),
    (lambda m: m["model"].pop("problem"), E.MissingRequiredSlotError, "missing_required_slot", "problem"),
], ids=["unknown-slot", "missing-required-slot"])
def test_validate_rejects_unknown_slot(damage, error, code, named):
    bad = full_model()
    damage(bad)
    with pytest.raises(error) as e:
        validate_proposal(bad)
    assert e.value.code == code and named in e.value.details["slots"]
    with pytest.raises(E.RequivoError) as e:
        validate_proposal("{not json")
    assert e.value.code == "invalid_model"


def test_validate_rejects_a_complete_model_with_no_objective():
    """Completeness is the full slot set *and* an objective; a projection never promised completeness."""
    with pytest.raises(E.InvalidModelError) as e:
        validate_proposal({**full_model(), "summary": {"objective": "   "}})
    assert e.value.path == "summary.objective"
    assert isinstance(validate_proposal(full_model()), EngineOutput)
    partial = {**full_model(), "summary": {}}
    partial["model"].pop("problem")
    assert isinstance(validate_proposal(partial, require_complete=False), EngineOutput)


def test_error_to_dict_is_serializable():
    d = E.UnknownSlotError("bad", path="model.x", details={"slots": ["x"]}).to_dict()
    assert d == {"code": "unknown_slot", "message": "bad", "path": "model.x", "details": {"slots": ["x"]}}
    json.dumps(d)


# ── store: revisions ────────────────────────────────────────────────────────────


def test_store_creates_session_and_revisions():
    store.create_session("s1", "Build a leave system.", provider="claude-code")
    assert store.read_meta("s1").current_revision == 0
    out = EngineOutput.model_validate(full_model())
    rev1, _ = store.save_revision("s1", out)
    rev2, meta = store.save_revision("s1", out)
    assert (rev1, rev2, meta.current_revision) == (1, 2, 2)
    assert (store.canonical_dir("s1") / "model.json").exists() and _revision_file("s1", 2).exists()
    assert store.list_session_slugs() == ["s1"]


def test_store_migrate_session_rejects_a_future_format():
    with pytest.raises(E.InvalidSessionError):
        store.migrate_session({"format_version": 999, "session_id": "x", "slug": "s", "created_at": "t", "updated_at": "t"})


# ── the apply pipeline, and its dependency-graph staleness ─────────────────────


def test_session_service_create_and_apply():
    svc = SessionService()
    meta = svc.create_session("Build a leave approval system.", slug="leave", provider="claude-code")
    assert meta.slug == "leave" and meta.current_revision == 0
    result = svc.update_model("leave", full_model(problem=slot(0, "empty", "high")))
    assert (result.status, result.revision) == ("applied", 1)
    assert set(result.changed_slots)                      # every slot counts as changed on the first apply
    assert result.readiness.ready is False and "problem" in result.readiness.blocking_slots
    plan = svc.diff("leave", REFRAMED)
    assert plan.status == "planned" and store.read_meta("leave").current_revision == 1
    result = svc.update_model("leave", REFRAMED)
    assert result.revision == 2 and "problem" in result.changed_slots


@pytest.mark.parametrize("artifact, change", [("prd", MOVED), ("brief", REFRAMED)], ids=["prd-consumes-workflow", "assessment-is-star"])
def test_apply_flags_generated_artifact_stale(art, artifact, change):
    """The assessment maps to `*`: no decision or challenge is needed to unseat it."""
    svc = _session(full_model())
    art.save("s", artifact, "# doc\n", source_revision=1)
    assert art.list("s")[artifact]["stale"] is False
    result = svc.update_model("s", change)
    assert artifact in result.stale_artifacts and art.list("s")[artifact]["stale"] is True


def test_apply_flags_assessment_stale_when_reasoning_is_unseated(art):
    out = EngineOutput.model_validate(_with_reasoning())
    hit, miss = propagate(out, ["workflow"]), propagate(out, ["success_metrics"])
    assert [c.headline for c in hit.challenges] == ["Archive vs delete"] and hit.reasoning_hit is True
    assert not miss.challenges and not miss.decisions and not miss.reasoning_hit
    svc = _session(_with_reasoning())
    art.save("s", "brief", "# Assessment\n", source_revision=1)
    assert art.list("s")["brief"]["stale"] is False
    result = svc.update_model("s", _with_reasoning(workflow=slot(80, "explicit", "high", "new flow")))
    assert "Archive vs delete" in result.invalidated_challenges
    assert "brief" in result.stale_artifacts and art.list("s")["brief"]["stale"] is True
    assert result.invalidated_decisions == []             # the decision rests on `permissions`, untouched
    assert "invalidated_challenges" in result.to_dict()


@pytest.mark.parametrize("revision, history_lies, code", [
    (999, False, "artifact_revision_out_of_range"), (0, False, "artifact_revision_out_of_range"), (1, True, "unreadable_source_revision"),
], ids=["future", "zero", "unreadable-history"])
def test_an_artifact_is_refused_when_its_freshness_cannot_be_established(art, revision, history_lies, code):
    """`False` is not "I don't know" -- it is the claim that the artifact is up to date."""
    _session(full_model(), MOVED)
    if history_lies:
        _revision_file("s", 1).unlink()
    with pytest.raises(E.RequivoError) as e:
        art.save("s", "prd", "# PRD\n", source_revision=revision)
    assert e.value.code == code and "prd" not in art.list("s")


# ── rescoping context cards (#168) ──────────────────────────────────────────────


def test_rescope_before_any_model_only_mutates_metadata():
    svc = _session(cards=["b2b-platform"])
    result = svc.rescope("s", context_cards=["event-ops"])
    assert (result.changed, result.revision, svc.cards("s")) == (True, 0, ["event-ops"])
    meta = store.read_meta("s")
    assert meta.current_revision == 0 and meta.revisions == []
    result = svc.rescope("s", context_cards=None)          # every card: the selection resets to none
    assert result.changed is True and result.context_cards is None and svc.cards("s") is None


def test_rescope_after_a_model_records_a_new_revision_with_unchanged_content():
    """Every revision on disk was reasoned under the old selection; a re-scope re-runs nothing (#168)."""
    from requivo.core.integrity import check_session_dir

    svc = _session(full_model(), cards=["b2b-platform"])
    result = svc.rescope("s", context_cards=["event-ops"])
    assert (result.changed, result.revision) == (True, 2)
    assert (result.previous_context_cards, result.context_cards) == (["b2b-platform"], ["event-ops"])
    meta = store.read_meta("s")
    assert meta.current_revision == 2 and meta.context_cards == ["event-ops"] and len(meta.revisions) == 2
    assert (meta.revisions[-1].revision, meta.revisions[-1].surface) == (2, "session-rescope")
    assert meta.revisions[-1].model_hash == meta.revisions[0].model_hash
    assert store.load_revision_model("s", 2).model_dump() == store.load_revision_model("s", 1).model_dump()
    assert svc.snapshot("s").context_cards == ["event-ops"]
    assert check_session_dir(store.canonical_dir("s"), expected_slug="s") == []


def test_rescope_to_the_current_selection_is_a_no_op():
    svc = _session(full_model(), cards=["b2b-platform", "event-ops"])
    result = svc.rescope("s", context_cards=["event-ops", "b2b-platform"])   # same set, other order
    assert (result.changed, result.revision) == (False, 1)
    assert store.read_meta("s").current_revision == 1 and len(store.read_meta("s").revisions) == 1


def test_rescope_does_not_mark_existing_artifacts_stale(art):
    """Context is not a fifth kind of dependency edge; a model change on the same artifact is the control."""
    svc = _session(full_model(), cards=["b2b-platform"])
    art.save("s", "prd", "# PRD\n", source_revision=1)
    svc.rescope("s", context_cards=["event-ops"])
    assert art.list("s")["prd"]["stale"] is False
    svc.update_model("s", MOVED)
    assert art.list("s")["prd"]["stale"] is True


@pytest.mark.parametrize("call", [
    lambda svc: svc.rescope("ghost", context_cards=["event-ops"]), lambda svc: svc.update_model("ghost", full_model()),
], ids=["rescope", "update_model"])
def test_a_missing_session_is_refused_by_name(call):
    with pytest.raises(E.SessionNotFoundError):
        call(SessionService())


def test_the_same_request_under_different_cards_is_a_different_session():
    """Context cards are provenance, not decoration."""
    svc = SessionService()
    first = svc.create_session("Same request.", context_cards=["b2b-platform"])
    again = svc.create_session("Same request.", context_cards=["b2b-platform"])
    other = svc.create_session("Same request.", context_cards=["event-ops"])
    assert again.slug == first.slug and other.slug != first.slug and svc.cards(other.slug) == ["event-ops"]


# ── the pre-flight discovery guards (#152, #421, #133) ───────────────────────────


@pytest.mark.parametrize("again", [
    lambda d: d.start("A leave approval system.", slug="dup"), lambda d: d.run_discovery("dup"),
], ids=["start", "run_discovery"])
def test_a_repeat_discovery_is_refused_before_the_provider_is_paid(again):
    """`start()` used to reason first and discover the conflict afterwards (#133)."""
    provider = CountingProvider()
    disco = DiscoveryService(provider)
    slug = disco.start("A leave approval system.", slug="dup")
    SessionService().update_model(slug, full_model(workflow=slot(90, "explicit", "high", "kept")))
    with pytest.raises(E.RevisionConflictError) as e:
        again(disco)
    assert e.value.details["actual"] == 2 and e.value.details["expected"] == 0   # the discovery, then the refinement
    assert provider.calls == 1 and SessionService().load_model(slug).model["workflow"].value == "kept"


@pytest.mark.parametrize("call", [
    lambda d: d.answer("s", "here are my answers"), lambda d: d.generate("s", "brief"), lambda d: d.generate("s", "prd"),
    lambda d: d.reason("s", "stories"),
], ids=["answer", "generate-brief", "generate-prd", "reason-stories"])
def test_answer_refuses_a_session_that_has_no_model_yet(call):
    """The mirror of the rule above (#152); `answer()` was the one write verb it missed, and its remedy named itself (#421)."""
    _session()
    provider = CountingProvider()
    with pytest.raises(E.RevisionConflictError) as e:
        call(DiscoveryService(provider))
    assert e.value.details["actual"] == 0 and e.value.details["expected"] == 1
    assert provider.calls == 0 and "discover" in str(e.value) and "requivo answer" not in str(e.value)


# ── freshness, impact, and locked reads ──────────────────────────────────────────


def test_the_artifact_service_defaults_to_the_session_service_s_storage():
    from requivo.services.repository import FileSessionRepository

    repo = FileSessionRepository()
    assert DiscoveryService(FakeProvider(), sessions=SessionService(repo)).artifacts.repo is repo
    assert DiscoveryService(FakeProvider(), repo=repo).sessions.repo is repo


def test_the_service_refuses_a_context_card_that_does_not_exist():
    """Invariant 14: creation and re-scope both resolve the selection rather than trusting it."""
    with pytest.raises(E.UnknownContextCardError):
        SessionService().create_session("Something.", context_cards=["made-up"])
    assert SessionService().create_session("Something.", context_cards=["b2b-platform"]).context_cards == ["b2b-platform"]
    svc = _session()
    with pytest.raises(E.UnknownContextCardError):
        svc.rescope("s", context_cards=["made-up"])
    assert svc.cards("s") is None


def test_impact_reports_what_a_named_slot_reaches():
    """`SessionService.impact` -- what #425's HTTP API `/impact` route is built on."""
    model = {**full_model(workflow=slot(80, "explicit", "high")),
             "decisions": [DesignDecision(decision="Draft-first invoices", derived_from=["workflow"]).model_dump()],
             "challenges": [Challenge(headline="Invoice at signature", premise="p", alternative="a", consequence="c",
                                      recommendation="r", contests=["workflow"]).model_dump()]}
    report = _session(model).impact("s", ["workflow"])
    assert any(d.decision == "Draft-first invoices" for d in report.decisions)
    assert any(c.headline == "Invoice at signature" for c in report.challenges)
    assert report.to_dict()["decisions"][0]["decision"] == "Draft-first invoices"


def test_impact_refuses_an_unknown_slot_naming_it_in_details():
    with pytest.raises(E.UnknownSlotError) as e:
        _session(full_model()).impact("s", ["not-a-real-slot"])
    assert e.value.details["unmatched"] == ["not-a-real-slot"]


def test_impact_with_no_slots_named_is_an_empty_report_not_a_refusal():
    assert _session(full_model()).impact("s", []).empty


def test_show_with_status_is_not_interleaved_by_a_concurrent_save(art):
    """The must-fire proof behind `show_with_status`'s own docstring (#425); the plain read is its control."""
    _session(full_model())
    with pytest.raises(E.SessionNotFoundError):
        art.show_with_status("s", "brief")
    art.save("s", "brief", "V1", source_revision=1)
    inside, may_finish, written = threading.Event(), threading.Event(), threading.Event()
    real_load, shown = art.repo.load_artifact, []

    def paused_load(slug, filename):
        content = real_load(slug, filename)
        inside.set()
        assert may_finish.wait(timeout=5), "the writer thread below never released the reader"
        return content

    art.repo.load_artifact = paused_load
    reader = threading.Thread(target=lambda: shown.append(art.show_with_status("s", "brief")), daemon=True)
    reader.start()
    assert inside.wait(timeout=5), "the reader never reached its locked read"
    threading.Thread(target=lambda: (art.save("s", "brief", "V2", source_revision=1), written.set()), daemon=True).start()
    assert not written.wait(timeout=0.2), "a concurrent save() proceeded while show_with_status held the lock"
    may_finish.set()
    reader.join(timeout=5)
    assert written.wait(timeout=5), "the writer never finished once the reader released"
    content, row = shown[0]
    assert content == "V1"
    assert row == {"revision": 1, "filename": "solution-assessment.md", "updated_at": row["updated_at"], "stale": False}


def test_a_first_discovery_that_races_a_concurrent_write_conflicts():
    svc = _session()
    racing = full_model(risks=slot(70, "explicit", "high", "rollout risk"))
    disco = DiscoveryService(client=RacingClient(json.dumps(full_model()), lambda: svc.update_model("s", racing)))
    with pytest.raises(E.RevisionConflictError):
        disco.run_discovery("s")
    assert svc.load_model("s").model["risks"].value == "rollout risk"


def test_an_artifact_generated_from_a_superseded_revision_is_born_stale(art):
    """Invariant 2: a generation carries the revision it read (#286)."""
    svc = _session(full_model())                          # revision 1, the PRD's actual source
    prd_reply = json.dumps({"title": "PRD", "problem": "Approvals are lost in email."})
    DiscoveryService(client=RacingClient(prd_reply, lambda: svc.update_model("s", MOVED))).generate("s", "prd")
    saved = art.list("s")["prd"]
    assert saved["revision"] == 1 and saved["stale"] is True


@pytest.mark.parametrize("field, id_field, item", [
    ("exclusions", "option", Exclusion(option="A full audit-trail UI", reason="The stated timeline funds the approval workflow only",
                                       rests_on=["constraints"])),
    ("thresholds", "condition", Threshold(condition="CAC exceeds the stated budget ceiling", measure="cost per paid signup",
                                          action="stop the paid channel", rests_on=["constraints"])),
], ids=["exclusion-600", "threshold-604"])
def test_a_generated_briefs_reasoning_items_are_absorbed_into_the_persisted_model(field, id_field, item):
    """#600/#604: `absorb_reasoning` carries `Brief.exclusions`/`Brief.thresholds` into `model.json`."""
    svc = _session(full_model())
    DiscoveryService(FakeProvider(artifacts={"brief": Brief(complexity="low", **{field: [item]})})).generate("s", "brief")
    items = getattr(svc.load_model("s"), field)
    assert [getattr(i, id_field) for i in items] == [getattr(item, id_field)] and items[0].rests_on == ["constraints"]


def test_a_legacy_session_is_named_in_the_error_rather_than_migrated_behind_your_back():
    """`out/` was the store until 0.8.0, and until 0.9.8 every read silently fell back to it."""
    legacy = store.legacy_dir("old")
    legacy.mkdir(parents=True)
    for name, text in (("model.json", json.dumps(full_model())), ("request.txt", "Legacy request."), ("prd.md", "# Legacy PRD\n")):
        (legacy / name).write_text(text, encoding="utf-8")
    svc = SessionService()
    assert not svc.exists("old")
    with pytest.raises(E.SessionNotFoundError) as e:
        svc.load_model("old")
    assert e.value.details.get("legacy") is True and "session migrate" in str(e.value)
    store.migrate_legacy("old")                           # the explicit migration: the model becomes revision 1
    assert store.session_exists("old") and (legacy / "model.json").exists()
    assert svc.update_model("old", full_model(problem=slot(90, "explicit", "high", "P"))).revision == 2
    assert _revision_file("old", 1).exists()
    assert (store.canonical_dir("old") / "artifacts" / "prd.md").read_text(encoding="utf-8") == "# Legacy PRD\n"


# ── artifact provenance is stated by the caller or the save is refused (#6) ───────


@pytest.fixture
def moved() -> str:
    """A session at revision 2 whose `workflow` slot moved between the two."""
    _session(full_model(workflow=slot(50, "inferred", "high", "draft")),
             full_model(workflow=slot(90, "explicit", "high", "draft -> issued -> archived")), slug="prov-moved")
    return "prov-moved"


def test_a_stated_source_revision_still_records_the_flag_it_always_did(art, moved):
    """MUST FIRE: the positive control for every provenance refusal below."""
    stale = art.save(moved, "prd", "# PRD reasoned from revision 1", source_revision=1)
    fresh = art.save(moved, "criteria", "# criteria reasoned from revision 2", source_revision=2)
    assert (stale.revision, stale.stale, fresh.revision, fresh.stale) == (1, True, 2, False)


def test_an_omitted_source_revision_is_refused_rather_than_read_as_now(art, moved):
    """#6 F1: the save that used to be recorded `revision: 2, stale: false`; it writes nothing and says what to pass."""
    with pytest.raises(UnstatedSourceRevisionError) as e:
        art.save(moved, "prd", "# PRD reasoned from revision 1")
    assert isinstance(e.value, E.RequivoError)
    assert e.value.details["source_revision"] is None and e.value.details["current_revision"] == 2
    assert all(word in e.value.message for word in ("--revision", "source_revision", "1", "2"))
    prd = store.canonical_dir(moved) / "artifacts" / "prd.md"
    assert not prd.exists() and "prd" not in store.read_meta(moved).artifact_status
    art.save(moved, "prd", "# PRD", source_revision=1)    # must fire: stated, both writes land
    assert prd.exists() and "prd" in store.read_meta(moved).artifact_status


def _corrupt(p):
    p.write_text('{"model": {"workflow": ', encoding="utf-8")


def _unreadable(p):
    p.unlink()
    p.mkdir()


@pytest.mark.parametrize("damage", [_corrupt, _unreadable, lambda p: p.unlink()], ids=["corrupt", "unreadable", "missing"])
def test_a_corrupt_revision_file_is_refused_as_a_structured_error(art, moved, damage):
    """#6 F2: `except E.RequivoError` only caught a *missing* revision; the guard is as wide as the failure set."""
    damage(_revision_file(moved, 1))
    with pytest.raises(E.InvalidSessionError) as e:
        art.save(moved, "prd", "# PRD", source_revision=1)
    assert e.value.code == "unreadable_source_revision" and e.value.details["source_revision"] == 1
    if damage is _corrupt:
        assert "ValidationError" in json.dumps(e.value.details), "the cause is not recorded"


def test_the_two_provenance_refusals_carry_two_codes_and_one_details_shape(art, moved):
    """Two refusals share one `details` shape but ride two codes, and neither answers the family base (#57)."""
    with pytest.raises(UnstatedSourceRevisionError) as unstated:
        art.save(moved, "prd", "# PRD")
    _revision_file(moved, 1).write_text("{", encoding="utf-8")
    with pytest.raises(E.InvalidSessionError) as unreadable:
        art.save(moved, "prd", "# PRD", source_revision=1)
    assert (unstated.value.code, unreadable.value.code) == ("unstated_source_revision", "unreadable_source_revision")
    assert isinstance(unstated.value, E.InvalidSessionError) and isinstance(unreadable.value, E.InvalidSessionError)
    assert {"slug", "type", "source_revision", "current_revision", "cause"} == set(unstated.value.details) == set(unreadable.value.details)
    assert unstated.value.details["cause"] is None and unreadable.value.details["cause"] is not None
