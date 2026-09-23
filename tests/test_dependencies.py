"""The dependency DAG and the staleness it drives (#72), thinner evidence (#493), the artifact-type
registries (#270), and an artifact type from a newer Requivo (#260)."""
from __future__ import annotations

import json

import pytest
from _fakes import (
    FakeClient,
    _model_in_out,
    full_model,
    full_slots,
    run_cli,
    run_cli_exit,
    run_cli_json,
    seed_session,
    slot,
)

from requivo.core import persistence as store
from requivo.core.analysis import slot_label
from requivo.core.contracts import Challenge, DesignDecision, EngineOutput, Exclusion, Threshold, schema_slot_ids
from requivo.core.dependencies import (
    _ARTIFACT_SLOTS_RAW,
    ARTIFACT_FILENAMES,
    EvidenceReport,
    ImpactReport,
    artifact_slots,
    diff_models,
    propagate,
    resolve_slots,
    thinner_evidence,
)
from requivo.core.errors import ModelUnreadableError, RevisionConflictError
from requivo.core.integrity import SEVERITY_NOTE, check_session, inspect_session
from requivo.providers.anthropic.generators import _GENERATORS, _OP_PROMPTS
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import _WRITERS, GENERATABLE
from requivo.services.sessions import SessionService
from requivo.web.viewmodels.labels import ARTIFACT_LABELS
from requivo.web.viewmodels.status import evidence_view

pytestmark = pytest.mark.usefixtures("workspace")

_EXCLUSION = Exclusion(option="Bulk import", reason="Out of scope for v1", rests_on=["permissions"])
_THRESHOLD = Threshold(condition="CAC exceeds the stated budget ceiling", measure="cost per paid signup",
                       action="stop the paid channel", rests_on=["permissions"])
_DECISION = DesignDecision(decision="Draft-first invoices", derived_from=["permissions"])


def _out(*decisions, **reasoning) -> EngineOutput:
    """A partial model whose `permissions`/`workflow` slots carry the given reasoning items."""
    return EngineOutput.model_validate({
        "model": {"current_process": slot(80, "explicit", "high"), "permissions": slot(60, "inferred", "high"),
                  "workflow": slot(70, "inferred", "high"), "business_objects": slot(50, "inferred", "medium")},
        "questions": [], "summary": {},
        "decisions": [d.model_dump() for d in decisions],
        **{k: [i.model_dump() for i in v] for k, v in reasoning.items()},
    })


# ── the DAG: propagation and material change ───────────────────────────────────


def test_propagate_flags_dependent_decisions_and_artifacts():
    out_ = _out(DesignDecision(decision="Draft-first invoices reviewed by Finance", derived_from=["permissions", "workflow"]),
                DesignDecision(decision="Amount sourced from the Contract", derived_from=["business_objects"]))
    rep = propagate(out_, ["permissions"])
    assert [d.decision for d in rep.decisions] == ["Draft-first invoices reviewed by Finance"]
    assert rep.decisions[0].rests_on == ["Permissions"]
    assert "prd" in rep.artifacts and "criteria" in rep.artifacts and "release" not in rep.artifacts
    assert not rep.empty


@pytest.mark.parametrize("field, item, key", [
    ("exclusions", _EXCLUSION, "option"), ("thresholds", _THRESHOLD, "condition"),
], ids=["exclusion-599", "threshold-604"])
def test_propagate_flags_dependent_thresholds(field, item, key):
    """#599/#604: an exclusion or a threshold rests_on a slot exactly like a decision's derived_from."""
    out_ = _out(**{field: [item]})
    rep = propagate(out_, ["permissions"])
    hit = getattr(rep, field)
    assert [getattr(i, key) for i in hit] == [getattr(item, key)] and hit[0].rests_on == ["Permissions"]
    assert rep.reasoning_hit and not rep.empty
    assert getattr(propagate(out_, ["workflow"]), field) == []


def test_propagate_reaches_only_the_assessment_for_an_otherwise_isolated_slot():
    rep = propagate(_out(), ["current_process"])   # feeds no buildable deliverable, no decision rests on it
    assert rep.artifacts == ["brief"] and not rep.decisions and not rep.challenges and not rep.empty


def test_resolve_slots_accepts_ids_and_label_words_and_flags_unknowns():
    assert resolve_slots(["permissions"]) == (["permissions"], [])
    assert resolve_slots(["permission"]) == (["permissions"], [])              # label substring
    assert resolve_slots(["workflow", "zzz"]) == (["workflow"], ["zzz"])
    assert resolve_slots(["risks", "problem"])[0] == ["problem", "risks"]      # schema order


def _wf(**over) -> EngineOutput:
    base = {"completeness": 60, "confidence": "inferred", "impact": "high", "value": "draft → issued", "evidence": ""}
    return EngineOutput.model_validate({"model": {"workflow": {**base, **over}}, "questions": [], "summary": {}})


def test_diff_models_flags_material_change_but_ignores_completeness_noise():
    assert diff_models(_wf(), _wf(completeness=90)) == []
    assert diff_models(_wf(), _wf(value="draft → issued → paid")) == ["workflow"]


def test_diff_models_flags_a_removed_slot():
    """Invariant 1's symmetry: a slot present before and gone after is a change (#286)."""
    old = EngineOutput.model_validate({"model": {**_wf().model_dump()["model"], "permissions": slot(70, "explicit", "high", "HR only")},
                                       "questions": [], "summary": {}})
    assert diff_models(old, _wf()) == ["permissions"]


def _testable(plan, **over) -> dict:
    return {"business_rules": {"completeness": 0, "confidence": "testable", "impact": "high", "value": "",
                               "evidence": "", "test_plan": plan, **over}}


def test_a_settled_testable_slot_propagates_like_any_other_change():
    """#610: a test result is a model change with a blast radius."""
    decision = DesignDecision(decision="Ship a flat monthly price", derived_from=["business_rules"])
    old = EngineOutput.model_validate({"model": _testable("Run a pricing survey."), "questions": [], "summary": {},
                                       "decisions": [decision.model_dump()]})
    settled = EngineOutput.model_validate({
        "model": {"business_rules": slot(90, "explicit", "high", "$29/month, confirmed by the survey")},
        "questions": [], "summary": {}, "decisions": [decision.model_dump()]})
    changed = diff_models(old, settled)
    assert changed == ["business_rules"]
    rep = propagate(old, changed)
    assert [d.decision for d in rep.decisions] == ["Ship a flat monthly price"] and "estimate" in rep.artifacts


def test_a_re_planned_test_is_a_material_change():
    """#610: `test_plan` rides into every generator prompt with the rest of the model."""
    def _m(plan):
        return EngineOutput.model_validate({"model": _testable(plan), "questions": [], "summary": {}})
    assert diff_models(_m("Run a pricing survey."), _m("Run a two-week paid pilot.")) == ["business_rules"]
    assert diff_models(_m("Run a pricing survey."), _m("Run a pricing survey.")) == []


# ── coverage: every slot reaches some artifact (#269) ───────────────────────────

# Slots that genuinely feed no specific artifact, each with its reason.
_SLOTS_WITH_NO_SPECIFIC_ARTIFACT = {
    "current_process": "the as-is process shapes the assessment (brief, via '*'); every buildable artifact describes the target state",
    "reporting": "filters/exports/dashboards surface through workflow, business_rules or acceptance, which are already consumed",
}


def test_artifact_slots_reference_only_real_slot_ids():
    from requivo.core.analysis import slot_meta
    valid = set(slot_meta()[1])
    for name, slots in artifact_slots().items():
        assert slots <= valid, f"{name} references unknown slot ids: {slots - valid}"


def test_every_required_slot_is_consumed_by_a_specific_artifact_or_is_exempted():
    """#269: `schema_slot_ids()` is the single source of the required set; a stale exemption is the mirror (#14)."""
    _, required = schema_slot_ids()
    specific = set().union(*(slots for name, slots in artifact_slots().items() if name != "brief"))
    exempt = set(_SLOTS_WITH_NO_SPECIFIC_ARTIFACT)
    uncovered = required - specific - exempt
    assert not uncovered, f"consumed by no specific artifact and not exempted: {sorted(uncovered)}"
    stale = (exempt - required) | (exempt & specific)
    assert not stale, f"stale exemption(s): {sorted(stale)}"


# ── `requivo impact`: a pure DAG query, offline ──────────────────────────────────


@pytest.mark.parametrize("reasoning, targeted, mapped", [
    ({"decisions": [_DECISION]}, "Draft-first invoices", "DEPENDENCY MAP"),
    ({"exclusions": [_EXCLUSION]}, "Bulk import", "exclusions: Bulk import"),
    ({"thresholds": [_THRESHOLD]}, "CAC exceeds the stated budget ceiling", "thresholds: CAC exceeds the stated budget ceiling"),
], ids=["decision", "exclusion-599", "threshold-604"])
def test_pc_impact_names_every_kind_of_reasoning_item(reasoning, targeted, mapped):
    """The targeted form and the no-args map (`render_dependency_map`) both name each kind (#599, #604)."""
    with _model_in_out("clitest-impact") as p:
        store.save_revision(p.parent.name, _out(*reasoning.pop("decisions", []), **reasoning))
        text = run_cli(["impact", str(p), "permissions"])
        assert targeted in text and ("prd" in text if "DEPENDENCY" in mapped else True)
        assert mapped in run_cli(["impact", str(p)])


# ── change-detection: stale artifacts on disk ───────────────────────────────────


@pytest.mark.parametrize("slot_name, before, after", [
    ("success_metrics", slot(40, "inferred", "high"), slot(90, "explicit", "high")),
    ("workflow", slot(50, "explicit", "high"), slot(95, "explicit", "high")),
], ids=["unrelated-slot", "completeness-only-on-consumed-slot"])
def test_a_non_material_change_keeps_the_artifact_fresh(slot_name, before, after):
    slug = seed_session("s", **{slot_name: before})
    ArtifactService().save(slug, "criteria", "# criteria", source_revision=1)
    SessionService().update_model(slug, full_model(**{slot_name: after}))
    assert ArtifactService().list(slug)["criteria"]["stale"] is False


def test_related_slot_change_marks_artifact_stale():
    """Invariant 1: an artifact is stale when something it rests on changed (#286)."""
    slug = seed_session("s", workflow=slot(50, "inferred", "high"))
    ArtifactService().save(slug, "criteria", "# criteria", source_revision=1)   # criteria consumes workflow
    SessionService().update_model(slug, full_model(workflow=slot(95, "explicit", "high", "draft → issued → archived")))
    assert ArtifactService().list(slug)["criteria"]["stale"] is True


def test_first_apply_does_not_invalidate_its_own_reasoning():
    svc = SessionService()
    svc.create_session("req", slug="s")
    model = {**full_model(workflow=slot(80, "explicit", "high"), permissions=slot(75, "explicit", "high")),
             "decisions": [DesignDecision(decision="Draft-first invoices reviewed by Finance",
                                          derived_from=["workflow", "permissions"]).model_dump()],
             "challenges": [Challenge(headline="Invoice at signature", premise="p", alternative="a", consequence="c",
                                      recommendation="r", contests=["workflow"]).model_dump()]}
    result = svc.update_model("s", model)
    assert result.invalidated_decisions == [] and result.invalidated_challenges == []


def test_second_apply_invalidates_prior_reasoning_a_change_unseats():
    svc = SessionService()
    svc.create_session("req", slug="s")
    svc.update_model("s", {**full_model(workflow=slot(80, "inferred", "high")),
                           "decisions": [DesignDecision(decision="Draft-first invoices reviewed by Finance",
                                                        derived_from=["workflow"]).model_dump()]})
    result = svc.update_model("s", full_model(workflow=slot(95, "explicit", "high", "draft → issued → archived")))
    assert "Draft-first invoices reviewed by Finance" in result.invalidated_decisions


def test_expected_revision_precondition_blocks_a_stale_write():
    svc = SessionService()
    seed_session("s", workflow=slot(60, "inferred", "high"))                # revision 1
    with pytest.raises(RevisionConflictError):                            # a racer still at revision 0
        svc.update_model("s", full_model(workflow=slot(80, "explicit", "high")), expected_revision=0)
    assert svc.update_model("s", full_model(workflow=slot(80, "explicit", "high")), expected_revision=1).revision == 2


def test_each_revision_records_its_provenance():
    svc = SessionService()
    svc.create_session("req", slug="s")
    svc.update_model("s", full_model(workflow=slot(60, "inferred", "high")),
                     provenance={"provider": "anthropic", "surface": "cli-discover", "model_name": "claude-x"})
    svc.update_model("s", full_model(workflow=slot(90, "explicit", "high", "a → b")),
                     provenance={"provider": "claude-code", "surface": "cli-apply"})
    revs = store.read_meta("s").revisions
    assert [r.revision for r in revs] == [1, 2]
    assert revs[0].previous_revision is None and revs[1].previous_revision == 1
    assert (revs[0].surface, revs[0].provider, revs[1].surface, revs[1].provider) == ("cli-discover", "anthropic", "cli-apply", "claude-code")
    assert all(r.model_hash.startswith("sha256:") for r in revs)


def test_pc_answer_warns_when_a_turn_makes_a_generated_artifact_stale():
    with _model_in_out("clitest-stale") as p:
        slug = p.parent.name
        store.save_revision(slug, EngineOutput.model_validate(full_model(workflow=slot(60, "inferred", "high", "draft → issued"))))
        ArtifactService().save(slug, "prd", "# stale PRD", source_revision=2)   # `_model_in_out` applied 1, the line above 2 (#6)
        turn2 = json.dumps({"model": full_slots(workflow=slot(95, "explicit", "high", "draft → issued → paid → archived")),
                            "questions": [], "summary": {"objective": "Document lifecycle"}})
        text = run_cli(["answer", slug, "It also has an archived state."], client=FakeClient(turn2))
        assert "STALE" in text and "prd.md" in text and "Workflow" in text


# ── thinner evidence (#493): a decision derived while its slot was thinner than it is now ───

DECISION = "Cut the CI matrix to four legs"
CURRENT_PROCESS = slot_label("current_process")   # the schema's own label, read rather than restated


def _model(*decisions, **slots) -> EngineOutput:
    return EngineOutput.model_validate({"model": full_slots(**slots), "questions": [], "summary": {"objective": "A faster pipeline"},
                                        "decisions": [d.model_dump() for d in decisions]})


def _decision(*derived_from, text=DECISION):
    return DesignDecision(decision=text, derived_from=list(derived_from))


def _walk(*models, slug="ci") -> SessionService:
    svc = SessionService()
    svc.create_session("Speed up CI.", slug=slug)
    for m in models:
        svc.update_model(slug, m.model_dump())
    return svc


EMPTY_THEN_EXPLICIT = (_model(_decision("current_process"), current_process=slot(0, "empty", "high")),
                       _model(_decision("current_process"), current_process=slot(90, "explicit", "high")))


@pytest.mark.parametrize(("completeness", "start_confidence", "test_plan"),
                         [(0, "empty", ""), (40, "inferred", ""), (0, "testable", "Run a two-week paid pilot.")],
                         ids=["empty-to-explicit", "inferred-to-explicit", "testable-to-explicit"])
def test_a_decision_derived_from_a_thin_slot_that_is_now_explicit_is_flagged(completeness, start_confidence, test_plan):
    """The `testable` leg is the case #610 was opened for."""
    then = _model(_decision("current_process"), current_process=slot(completeness, start_confidence, "high", test_plan=test_plan))
    report = thinner_evidence(then, EMPTY_THEN_EXPLICIT[1])
    assert [f.decision for f in report.flagged] == [DECISION] and report.flagged[0].thickened == [CURRENT_PROCESS]
    assert report.could_not_tell == [] and report.reviewed == 1


@pytest.mark.parametrize("then, now", [
    (_model(_decision("current_process"), current_process=slot(70, "explicit", "high")),
     _model(_decision("current_process"), current_process=slot(95, "explicit", "high"))),
    (_model(_decision("permissions"), workflow=slot(0, "empty", "high"), permissions=slot(60, "explicit", "high")),
     _model(_decision("permissions"), workflow=slot(90, "explicit", "high"), permissions=slot(60, "explicit", "high"))),
    (_model(_decision("workflow"), workflow=slot(70, "explicit", "high")), _model(_decision("workflow"), workflow=slot(40, "inferred", "high"))),
    (_model(_decision("workflow"), workflow=slot(40, "inferred", "high")), _model(_decision("workflow"), workflow=slot(40, "inferred", "high"))),
], ids=["already-explicit", "another-slot-thickened", "thinned", "unchanged"])
def test_a_decision_whose_evidence_did_not_thicken_is_not_flagged(then, now):
    """The must-not-fire half; `reviewed == 1` separates *found nothing* from *never looked*."""
    report = thinner_evidence(then, now)
    assert report.flagged == [] and report.could_not_tell == [] and report.reviewed == 1


def _without_slot(model: EngineOutput, slot_id: str) -> EngineOutput:
    d = model.model_dump()
    del d["model"][slot_id]
    return EngineOutput.model_validate(d)


@pytest.mark.parametrize("then, now, reason", [
    (_model(_decision(), current_process=slot(0, "empty", "high")), _model(_decision(), current_process=slot(90, "explicit", "high")), "no slots"),
    (_model(current_process=slot(0, "empty", "high")), EMPTY_THEN_EXPLICIT[1], ""),
    (_without_slot(EMPTY_THEN_EXPLICIT[0], "current_process"), EMPTY_THEN_EXPLICIT[1], CURRENT_PROCESS),
], ids=["no-derived-from", "absent-before", "slot-missing-before-invariant-8"])
def test_a_decision_that_cannot_be_reviewed_is_could_not_tell_not_clean(then, now, reason):
    """A decision with no `derived_from`, absent earlier, or resting on a slot the earlier model lacked (invariant 8)."""
    report = thinner_evidence(then, now)
    assert report.flagged == [] and [u.decision for u in report.could_not_tell] == [DECISION]
    assert reason in report.could_not_tell[0].reason


def test_the_report_dict_carries_all_three_states_and_the_impact_report_carries_it():
    then = _model(_decision("current_process"), _decision("permissions", text="Keep the matrix"), current_process=slot(0, "empty", "high"))
    now = _model(_decision("current_process"), _decision("permissions", text="Keep the matrix"), current_process=slot(90, "explicit", "high"))
    d = thinner_evidence(then, now).to_dict()
    assert set(d) == {"reviewed", "flagged", "could_not_tell"}
    assert d["reviewed"] == 2 and len(d["flagged"]) == 1 and d["could_not_tell"] == []
    assert set(d["flagged"][0]) == {"decision", "id", "thickened", "derived_at"} and d["flagged"][0]["derived_at"] is None
    assert propagate(now, ["current_process"]).to_dict()["evidence"] is None
    assert ImpactReport(changed=[], evidence=EvidenceReport()).to_dict()["evidence"] == {"reviewed": 0, "flagged": [], "could_not_tell": []}


def test_the_issues_own_shape_fires_on_impact():
    """The session #493 describes, through the service, the targeted verb, the full map and `impact()`."""
    svc = _walk(*EMPTY_THEN_EXPLICIT)
    report = svc.thinner_evidence("ci")
    assert [(f.decision, f.derived_at, f.thickened) for f in report.flagged] == [(DECISION, 1, [CURRENT_PROCESS])]
    assert report.reviewed == 1 and report.could_not_tell == []
    text = run_cli(["impact", "ci", "current_process"])
    assert "THINNER EVIDENCE" in text and DECISION in text and "revision 1" in text and CURRENT_PROCESS in text
    assert "contradict" not in text.lower()
    text = run_cli(["impact", "ci"])
    assert "DEPENDENCY MAP" in text and "THINNER EVIDENCE" in text and DECISION in text
    body = svc.impact("ci", ["workflow"]).to_dict()
    assert body["evidence"]["reviewed"] == 1 and [f["derived_at"] for f in body["evidence"]["flagged"]] == [1]


def test_a_decision_derived_after_the_slot_was_measured_is_not_flagged_on_impact():
    """The must-not-fire control for the test above, through the same verb."""
    svc = _walk(_model(current_process=slot(0, "empty", "high")), _model(current_process=slot(90, "explicit", "high")),
                EMPTY_THEN_EXPLICIT[1])
    report = svc.thinner_evidence("ci")
    assert report.flagged == [] and report.could_not_tell == [] and report.reviewed == 1
    text = run_cli(["impact", "ci", "current_process"])
    assert "THINNER EVIDENCE" not in text and "1 of 1 decision(s) checked, none rests on thinner evidence" in text


def test_the_derivation_revision_is_the_earliest_that_carries_the_decision():
    svc = _walk(EMPTY_THEN_EXPLICIT[0], _model(_decision("current_process"), current_process=slot(40, "inferred", "high")),
                EMPTY_THEN_EXPLICIT[1])
    assert [f.derived_at for f in svc.thinner_evidence("ci").flagged] == [1]


def test_a_reworded_decision_counts_as_newly_derived_at_its_rewording():
    """The accepted limit (invariant 5: ids are content-derived)."""
    svc = _walk(EMPTY_THEN_EXPLICIT[0], _model(_decision("current_process", text="Cut the CI matrix down to four legs"),
                                               current_process=slot(90, "explicit", "high")))
    report = svc.thinner_evidence("ci")
    assert report.flagged == [] and report.reviewed == 1


def test_a_revision_from_an_older_requivo_without_confidence_data_is_could_not_tell():
    """Permissive read, service side (invariant 8)."""
    svc = _walk(*EMPTY_THEN_EXPLICIT)
    frozen = store.canonical_dir("ci") / "revisions" / "0001-model.json"
    payload = json.loads(frozen.read_text(encoding="utf-8"))
    del payload["model"]["current_process"]["confidence"]
    frozen.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ModelUnreadableError):            # positive control: the store refuses that file
        svc.load_revision("ci", 1)
    report = svc.thinner_evidence("ci")
    assert report.flagged == [] and [u.decision for u in report.could_not_tell] == [DECISION]
    assert "revision 1" in report.could_not_tell[0].reason and report.reviewed == 1
    text = run_cli(["impact", "ci", "current_process"])
    assert "Could not check" in text and "revision 1 could not be read" in text and "THINNER EVIDENCE" not in text


def test_a_loose_model_file_never_borrows_the_review_of_a_session_sharing_its_directory_name(tmp_path):
    """`impact` accepts a model.json path, which has no history to walk, even under a directory named like a session."""
    _walk(*EMPTY_THEN_EXPLICIT)
    assert "THINNER EVIDENCE" in run_cli(["impact", "ci", "current_process"])   # positive control
    loose = tmp_path / "elsewhere" / "ci" / "model.json"
    loose.parent.mkdir(parents=True)
    loose.write_text(_model(current_process=slot(90, "explicit", "high")).model_dump_json(), encoding="utf-8")
    text = run_cli(["impact", str(loose), "current_process"])
    assert "not reviewed" in text and "none rests on thinner evidence" not in text
    assert DECISION not in text and "THINNER EVIDENCE" not in text


def test_evidence_view_relabels_the_core_report_without_recomputing_it():
    then = _model(_decision("current_process"), _decision(text="Keep the matrix"), current_process=slot(0, "empty", "high"))
    now = _model(_decision("current_process"), _decision(text="Keep the matrix"), current_process=slot(90, "explicit", "high"))
    report = thinner_evidence(then, now)
    for f in report.flagged:
        f.derived_at = 1
    view = evidence_view(report)
    flagged_id, unknown_id = report.flagged[0].id, report.could_not_tell[0].id
    assert view["reviewed"] is True
    assert "worth re-reading" in view["reread"][flagged_id].lower() and "contradict" not in view["reread"][flagged_id].lower()
    assert CURRENT_PROCESS in view["reread"][flagged_id] and "revision 1" in view["reread"][flagged_id]
    assert unknown_id in view["unchecked"] and unknown_id not in view["reread"]
    assert evidence_view(None) == {"reviewed": False, "reread": {}, "unchecked": {}}


def test_the_traceability_panel_marks_the_decision_worth_re_reading():
    from fastapi.testclient import TestClient

    from requivo.web.app import create_app
    from requivo.web.security import CSRF_HEADER, csrf_token

    both = (_decision("current_process"), _decision("permissions", text="Keep the matrix"))
    _walk(_model(*both, current_process=slot(0, "empty", "high"), permissions=slot(70, "explicit", "high")),
          _model(*both, current_process=slot(90, "explicit", "high"), permissions=slot(70, "explicit", "high")))
    client = TestClient(create_app(), base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    client.headers[CSRF_HEADER] = csrf_token()
    html = client.get("/sessions/ci").text
    assert DECISION in html and "Keep the matrix" in html and CURRENT_PROCESS in html
    assert html.count("Worth re-reading") == 1


# ── the artifact-type registries agree with themselves (#270) ──────────────────


def _artifact_vocabulary_mismatches(*, slots_raw, generators, op_prompts, writers, generatable,
                                    artifact_filenames, artifact_labels) -> list[str]:
    """Every relationship the real tables must satisfy, over the real tables or a broken fixture copy."""
    canonical = set(slots_raw)
    checks = [
        (set(generators) != canonical, f"_GENERATORS {sorted(set(generators) ^ canonical)} disagrees with _ARTIFACT_SLOTS_RAW"),
        (set(op_prompts) != canonical | {"analyze"}, f"_OP_PROMPTS {sorted(set(op_prompts) ^ (canonical | {'analyze'}))} disagrees with _ARTIFACT_SLOTS_RAW + analyze"),
        (not set(writers) <= canonical, f"_WRITERS {sorted(set(writers) - canonical)} not in _ARTIFACT_SLOTS_RAW"),
        (not set(generatable) <= canonical, f"GENERATABLE {sorted(set(generatable) - canonical)} not in _ARTIFACT_SLOTS_RAW"),
        # The dangerous one: a type saved under a real filename here is never flagged stale (invariant 1).
        (not set(artifact_filenames) <= canonical, f"ARTIFACT_FILENAMES {sorted(set(artifact_filenames) - canonical)} not in _ARTIFACT_SLOTS_RAW"),
        (not set(generatable) <= set(artifact_filenames), f"GENERATABLE {sorted(set(generatable) - set(artifact_filenames))} has no ARTIFACT_FILENAMES entry"),
        (not set(artifact_filenames) <= set(artifact_labels), f"ARTIFACT_FILENAMES {sorted(set(artifact_filenames) - set(artifact_labels))} has no ARTIFACT_LABELS entry"),
    ]
    return [msg for failed, msg in checks if failed]


def _real_tables() -> dict:
    return {"slots_raw": dict(_ARTIFACT_SLOTS_RAW), "generators": dict(_GENERATORS), "op_prompts": dict(_OP_PROMPTS),
            "writers": dict(_WRITERS), "generatable": tuple(GENERATABLE), "artifact_filenames": dict(ARTIFACT_FILENAMES),
            "artifact_labels": dict(ARTIFACT_LABELS)}


def test_the_real_artifact_registries_agree_on_their_key_sets():
    """#270: on the tables actually shipped, every relationship holds."""
    assert _artifact_vocabulary_mismatches(**_real_tables()) == []


@pytest.mark.parametrize("table_name, dummy", [("generators", lambda *a, **k: None), ("writers", lambda a: ""), ("artifact_filenames", "dummy.md")])
def test_a_type_missing_its__ARTIFACT_SLOTS_RAW_entry_is_caught(table_name, dummy):
    """The positive control #270 asks for by name."""
    tables = _real_tables()
    tables[table_name]["dummy"] = dummy
    problems = _artifact_vocabulary_mismatches(**tables)
    assert any("dummy" in p for p in problems), problems


# ── an artifact type this build does not know (#260) ─────────────────────────────


def _future(slug: str = "s", atype: str = "risk-register", filename: str = "risk-register.md", *, write_file: bool = True) -> None:
    """A healthy session at revision 1 with a real `prd`, plus an artifact type a newer Requivo recorded."""
    seed_session(slug)
    ArtifactService().save(slug, "prd", "# PRD", source_revision=1)
    d = store.canonical_dir(slug)
    p = d / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"][atype] = dict(raw["artifact_status"]["prd"], filename=filename)
    p.write_text(json.dumps(raw), encoding="utf-8")
    if write_file:
        (d / "artifacts" / filename).write_text("# Risk register\n", encoding="utf-8")


def _codes(slug: str = "s") -> set:
    return {f.code for f in check_session(slug)}


def _edit_status(slug, atype, **fields):
    p = store.canonical_dir(slug) / "session.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["artifact_status"][atype].update(fields)
    p.write_text(json.dumps(raw), encoding="utf-8")


def test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect():
    """A plausible unknown type blocks nothing and is still *named*; a real mismatch is the positive control."""
    seed_session("s")
    ArtifactService().save("s", "prd", "# PRD", source_revision=1)
    assert check_session("s") == []
    _edit_status("s", "prd", filename="epic.md")
    assert "artifact_filename_mismatch" in _codes()
    _future("t")
    assert check_session("t") == []
    notes = [f for f in inspect_session("t") if f.severity == SEVERITY_NOTE]
    assert [f.code for f in notes] == ["unknown_artifact_type"] and "risk-register" in notes[0].message


def test_a_tolerated_artifact_type_is_held_to_every_other_check():
    """Tolerating is not trusting (invariant 14)."""
    _future(write_file=False)
    assert "missing_artifact_file" in _codes()
    _future("t")
    _edit_status("t", "risk-register", revision=9)
    assert "artifact_revision_out_of_range" in _codes("t")


def test_an_unsafe_artifact_filename_on_an_unknown_type_is_still_refused(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("x\n", encoding="utf-8")
    _future(filename=str(outside), write_file=False)
    codes = _codes()
    assert "unsafe_artifact_filename" in codes and "missing_artifact_file" not in codes   # never stat-ed


@pytest.mark.parametrize("atype", ["Risk-Register", "risk register", "../escape", "risk\nregister", "x" * 200])
def test_an_artifact_type_that_is_not_a_plausible_token_is_still_a_problem(atype):
    """The other half of *tolerating is not trusting* (#260)."""
    _future(atype=atype)
    codes = _codes()
    assert "unsafe_artifact_type" in codes and "unknown_artifact_type" not in codes


def test_session_verify_passes_and_still_names_the_unknown_type():
    _future()
    report = run_cli_json(["session", "verify", "s", "--json"])
    assert report["ok"] is True and report["problems"] == []
    assert [n["code"] for n in report["notes"]] == ["unknown_artifact_type"]
    assert "risk-register" in run_cli(["session", "verify", "s"])
    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()   # must fire: a real inconsistency still exits 1
    assert run_cli_exit(["session", "verify", "s", "--json"])[1] == 1


def test_doctor_names_the_unknown_type_without_calling_the_session_inconsistent():
    _future()
    r = run_cli_json(["doctor", "--json"])["sessions"]
    assert r["inconsistent"] == {} and r["notes"] == {"s": ["unknown_artifact_type"]}
    out = run_cli(["doctor"])
    assert "unknown_artifact_type" in out and "requivo session verify s" in out


def test_a_future_artifact_type_survives_an_export_import_round_trip(tmp_path):
    _future()
    dest = tmp_path / "s.zip"
    run_cli(["session", "export", "s", "-o", str(dest), "--json"])
    run_cli(["session", "import", str(dest), "--force", "--json"])
    meta = json.loads((store.canonical_dir("s") / "session.json").read_text(encoding="utf-8"))
    assert meta["artifact_status"]["risk-register"]["filename"] == "risk-register.md"
    assert (store.canonical_dir("s") / "artifacts" / "risk-register.md").is_file()
    _future("t", atype="../escape")                      # must fire: an implausible type is still refused
    bad = tmp_path / "t.zip"
    run_cli(["session", "export", "t", "-o", str(bad), "--json"])
    assert run_cli_exit(["session", "import", str(bad), "--force", "--json"])[1] != 0
