"""The dependency DAG, and the staleness it drives (#72)."""
import json
import shutil

import pytest
from _fakes import FakeClient, _model_in_out, _run_app, full_slots, out, slot

from requivo.core import persistence as store
from requivo.core.contracts import Challenge, DesignDecision, EngineOutput, Exclusion, Threshold, schema_slot_ids
from requivo.core.dependencies import (
    _ARTIFACT_SLOTS_RAW,
    ARTIFACT_FILENAMES,
    artifact_slots,
    diff_models,
    propagate,
    resolve_slots,
)
from requivo.providers.anthropic.generators import _GENERATORS, _OP_PROMPTS
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import _WRITERS, GENERATABLE
from requivo.web.viewmodels.labels import ARTIFACT_LABELS


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    """Every test in this module writes sessions/artifacts into an isolated temp workspace."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


# ── Tier 2: the dependency DAG (impact propagation) ───────────────────────────
# Pure logic — no API.


def _out_with_decisions(*decisions):
    return EngineOutput.model_validate({
        "model": {
            "current_process": slot(80, "explicit", "high"),
            "permissions": slot(60, "inferred", "high"),
            "workflow": slot(70, "inferred", "high"),
            "business_objects": slot(50, "inferred", "medium"),
        },
        "questions": [], "summary": {},
        "decisions": [d.model_dump() for d in decisions],
    })


def test_propagate_flags_dependent_decisions_and_artifacts():
    out_ = _out_with_decisions(
        DesignDecision(decision="Draft-first invoices reviewed by Finance",
                       derived_from=["permissions", "workflow"]),
        DesignDecision(decision="Amount sourced from the Contract",
                       derived_from=["business_objects"]),
    )
    rep = propagate(out_, ["permissions"])
    # only the decision resting on permissions, and it names the changed slot it rests on
    assert [d.decision for d in rep.decisions] == ["Draft-first invoices reviewed by Finance"]
    assert rep.decisions[0].rests_on == ["Permissions"]
    # artifacts consuming permissions go stale; release (no permissions) does not
    assert "prd" in rep.artifacts and "criteria" in rep.artifacts
    assert "release" not in rep.artifacts
    assert not rep.empty


def test_propagate_flags_dependent_exclusions():
    """#599: an excluded option rests_on a slot exactly like a decision's derived_from."""
    out_ = EngineOutput.model_validate({
        "model": {"permissions": slot(60, "inferred", "high"),
                  "workflow": slot(70, "inferred", "high")},
        "questions": [], "summary": {},
        "exclusions": [Exclusion(option="Bulk import", reason="Out of scope for v1",
                                 rests_on=["permissions"]).model_dump()],
    })
    rep = propagate(out_, ["permissions"])
    assert [e.option for e in rep.exclusions] == ["Bulk import"]
    assert rep.exclusions[0].rests_on == ["Permissions"]
    assert rep.reasoning_hit and not rep.empty
    assert propagate(out_, ["workflow"]).exclusions == []   # not rested on -- no hit


def test_propagate_flags_dependent_thresholds():
    """#604 acceptance criterion: a threshold rests_on a slot exactly like a decision's derived_from."""
    out_ = EngineOutput.model_validate({
        "model": {"permissions": slot(60, "inferred", "high"),
                  "workflow": slot(70, "inferred", "high")},
        "questions": [], "summary": {},
        "thresholds": [Threshold(condition="CAC exceeds the stated budget ceiling",
                                 measure="cost per paid signup", action="stop the paid channel",
                                 rests_on=["permissions"]).model_dump()],
    })
    rep = propagate(out_, ["permissions"])
    assert [t.condition for t in rep.thresholds] == ["CAC exceeds the stated budget ceiling"]
    assert rep.thresholds[0].rests_on == ["Permissions"]
    assert rep.reasoning_hit and not rep.empty
    assert propagate(out_, ["workflow"]).thresholds == []   # not rested on -- no hit


def test_propagate_reaches_only_the_assessment_for_an_otherwise_isolated_slot():
    # current_process feeds no buildable deliverable and no decision rests on it.
    rep = propagate(_out_with_decisions(), ["current_process"])
    assert rep.artifacts == ["brief"]
    assert not rep.decisions and not rep.challenges and not rep.empty


def test_resolve_slots_accepts_ids_and_label_words_and_flags_unknowns():
    assert resolve_slots(["permissions"]) == (["permissions"], [])
    assert resolve_slots(["permission"]) == (["permissions"], [])  # label substring
    ids, unmatched = resolve_slots(["workflow", "zzz"])
    assert ids == ["workflow"] and unmatched == ["zzz"]
    # returned in schema order regardless of input order
    assert resolve_slots(["risks", "problem"])[0] == ["problem", "risks"]


def test_diff_models_flags_material_change_but_ignores_completeness_noise():
    base = {"workflow": {"completeness": 60, "confidence": "inferred", "impact": "high",
                         "value": "draft → issued", "evidence": ""}}
    old = EngineOutput.model_validate({"model": base, "questions": [], "summary": {}})
    # completeness alone moving is not a material change
    bumped = json.loads(json.dumps(base))
    bumped["workflow"]["completeness"] = 90
    same = EngineOutput.model_validate({"model": bumped, "questions": [], "summary": {}})
    assert diff_models(old, same) == []
    # a value change is
    changed = json.loads(json.dumps(base))
    changed["workflow"]["value"] = "draft → issued → paid"
    newv = EngineOutput.model_validate({"model": changed, "questions": [], "summary": {}})
    assert diff_models(old, newv) == ["workflow"]


def test_a_settled_testable_slot_propagates_like_any_other_change():
    """#610: the point of "testable" is that a test result is a model change with a blast radius."""
    old = EngineOutput.model_validate({
        "model": {"business_rules": {"completeness": 0, "confidence": "testable", "impact": "high",
                                     "value": "", "evidence": "", "test_plan": "Run a pricing survey."}},
        "questions": [], "summary": {},
        "decisions": [DesignDecision(decision="Ship a flat monthly price",
                                     derived_from=["business_rules"]).model_dump()],
    })
    settled = EngineOutput.model_validate({
        "model": {"business_rules": {"completeness": 90, "confidence": "explicit", "impact": "high",
                                     "value": "$29/month, confirmed by the survey", "evidence": "survey"}},
        "questions": [], "summary": {},
        "decisions": [d.model_dump() for d in old.decisions],
    })
    changed = diff_models(old, settled)
    assert changed == ["business_rules"]
    rep = propagate(old, changed)
    assert [d.decision for d in rep.decisions] == ["Ship a flat monthly price"]
    assert "estimate" in rep.artifacts


def test_a_re_planned_test_is_a_material_change():
    """#610: `test_plan` rides into every generator prompt with the rest of the model."""
    def _m(plan):
        return EngineOutput.model_validate({
            "model": {"business_rules": {"completeness": 0, "confidence": "testable", "impact": "high",
                                         "value": "", "evidence": "", "test_plan": plan}},
            "questions": [], "summary": {},
        })
    assert diff_models(_m("Run a pricing survey."), _m("Run a two-week paid pilot.")) == ["business_rules"]
    assert diff_models(_m("Run a pricing survey."), _m("Run a pricing survey.")) == []


def test_diff_models_flags_a_removed_slot():
    """Invariant 1's symmetry. Reasoning a turn merely *omits* is not a removal (#286)."""
    # A slot present before and gone after must register as a change.
    both = {"workflow": {"completeness": 60, "confidence": "inferred", "impact": "high",
                         "value": "draft → issued", "evidence": ""},
            "permissions": {"completeness": 70, "confidence": "explicit", "impact": "high",
                            "value": "HR only", "evidence": ""}}
    old = EngineOutput.model_validate({"model": both, "questions": [], "summary": {}})
    dropped = {"workflow": both["workflow"]}  # permissions removed
    new = EngineOutput.model_validate({"model": dropped, "questions": [], "summary": {}})
    assert diff_models(old, new) == ["permissions"]


def test_artifact_slots_reference_only_real_slot_ids():
    from requivo.core.analysis import slot_meta
    valid = set(slot_meta()[1])
    for name, slots in artifact_slots().items():
        assert slots <= valid, f"{name} references unknown slot ids: {slots - valid}"


# ── the coverage direction: does every slot reach some artifact? (#269) ───────
#
# The test above only checks the subset direction -- every id an artifact set names is real.
#
# Slots that genuinely feed no specific artifact.
_SLOTS_WITH_NO_SPECIFIC_ARTIFACT = {
    "current_process": (
        "the as-is process shapes the assessment's judgment (brief, via '*') but no buildable "
        "artifact has a field for 'how it's done today' -- prd/stories/estimate/criteria/epic/"
        "release all describe the target state, never the process being replaced."
    ),
    "reporting": (
        "filters/exports/dashboards/audit trails inform the assessment's read of the request but map "
        "onto no dedicated field in any single artifact contract -- a reporting need important enough "
        "to build usually surfaces through workflow, business_rules or acceptance instead, which are "
        "already consumed."
    ),
}


def test_every_required_slot_is_consumed_by_a_specific_artifact_or_is_exempted():
    """#269. `schema_slot_ids()` is the single source of the required set (already excluding `optional: true`
    slots, which requiring here would assert a fact the schema itself does not claim)."""
    _, required = schema_slot_ids()
    amap = artifact_slots()
    specific = set().union(*(slots for name, slots in amap.items() if name != "brief"))
    exempt = set(_SLOTS_WITH_NO_SPECIFIC_ARTIFACT)

    uncovered = required - specific - exempt
    assert not uncovered, (
        f"these required slot(s) are consumed by no specific artifact and are not exempted: "
        f"{sorted(uncovered)} -- add each to an artifact's set in _ARTIFACT_SLOTS_RAW, or to "
        f"_SLOTS_WITH_NO_SPECIFIC_ARTIFACT with a reason.")

    # The mirror direction: a stale exemption (#14).
    stale = (exempt - required) | (exempt & specific)
    assert not stale, (
        f"_SLOTS_WITH_NO_SPECIFIC_ARTIFACT names slot(s) that are gone or now consumed by a specific "
        f"artifact: {sorted(stale)} -- remove the stale exemption(s).")


def test_pc_impact_reports_blast_radius_offline():
    # No client needed — impact is a pure DAG query.
    with _model_in_out("clitest-impact") as p:
        out_ = _out_with_decisions(
            DesignDecision(decision="Draft-first invoices", derived_from=["permissions"]))
        store.save_revision(p.parent.name, out_)
        text = _run_app(["impact", str(p), "permissions"])
        assert "Draft-first invoices" in text and "prd" in text


def test_pc_impact_shows_exclusions_alongside_decisions_and_challenges():
    """#599 acceptance criterion: `requivo impact <slug> <slot>` shows exclusions too."""
    with _model_in_out("clitest-impact-exclusions") as p:
        out_ = EngineOutput.model_validate({
            "model": {"permissions": slot(60, "inferred", "high")},
            "questions": [], "summary": {},
            "exclusions": [Exclusion(option="Bulk import", reason="Out of scope for v1",
                                     rests_on=["permissions"]).model_dump()],
        })
        store.save_revision(p.parent.name, out_)
        text = _run_app(["impact", str(p), "permissions"])
        assert "Bulk import" in text


def test_pc_impact_shows_thresholds_alongside_decisions_and_challenges():
    """#604 acceptance criterion: `requivo impact <slug> <slot>` shows thresholds too."""
    with _model_in_out("clitest-impact-thresholds") as p:
        out_ = EngineOutput.model_validate({
            "model": {"permissions": slot(60, "inferred", "high")},
            "questions": [], "summary": {},
            "thresholds": [Threshold(condition="CAC exceeds the stated budget ceiling",
                                     measure="cost per paid signup", action="stop the paid channel",
                                     rests_on=["permissions"]).model_dump()],
        })
        store.save_revision(p.parent.name, out_)
        text = _run_app(["impact", str(p), "permissions"])
        assert "CAC exceeds the stated budget ceiling" in text


def test_pc_impact_no_slots_prints_the_full_map():
    with _model_in_out("clitest-impact-map") as p:
        store.save_revision(p.parent.name, _out_with_decisions())
        text = _run_app(["impact", str(p)])
        assert "DEPENDENCY MAP" in text


def test_pc_impact_full_map_lists_exclusions_per_slot():
    """#599: the no-args dependency map (render_dependency_map) names exclusions per slot the same way it
    names decisions and challenges — the sibling of the targeted-slot test above."""
    with _model_in_out("clitest-impact-map-exclusions") as p:
        out_ = EngineOutput.model_validate({
            "model": {"permissions": slot(60, "inferred", "high")},
            "questions": [], "summary": {},
            "exclusions": [Exclusion(option="Bulk import", reason="Out of scope for v1",
                                     rests_on=["permissions"]).model_dump()],
        })
        store.save_revision(p.parent.name, out_)
        text = _run_app(["impact", str(p)])
        assert "exclusions: Bulk import" in text


def test_pc_impact_full_map_lists_thresholds_per_slot():
    """#604: the no-args dependency map (render_dependency_map) names thresholds per slot the same way it
    names decisions, challenges and exclusions — the sibling of the targeted-slot test above."""
    with _model_in_out("clitest-impact-map-thresholds") as p:
        out_ = EngineOutput.model_validate({
            "model": {"permissions": slot(60, "inferred", "high")},
            "questions": [], "summary": {},
            "thresholds": [Threshold(condition="CAC exceeds the stated budget ceiling",
                                     measure="cost per paid signup", action="stop the paid channel",
                                     rests_on=["permissions"]).model_dump()],
        })
        store.save_revision(p.parent.name, out_)
        text = _run_app(["impact", str(p)])
        assert "thresholds: CAC exceeds the stated budget ceiling" in text


# ── Tier 2 (B): change-detection — stale artifacts on disk ────────────────────


@pytest.mark.parametrize("slug, slot_name, before, after", [
    ("clitest-fresh-unrelated", "success_metrics",
     slot(40, "inferred", "high"), slot(90, "explicit", "high")),
    ("clitest-fresh-completeness", "workflow",
     slot(50, "explicit", "high"), slot(95, "explicit", "high")),
], ids=["unrelated-slot", "completeness-only-on-consumed-slot"])
def test_a_non_material_change_keeps_the_artifact_fresh(slug, slot_name, before, after):
    # criteria consumes {workflow, business_rules, permissions, edge_cases, acceptance}.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    store.create_session(slug, "req")
    svc.update_model(slug, out({slot_name: before}).model_dump())
    ArtifactService().save(slug, "criteria", "# criteria", source_revision=1)
    try:
        svc.update_model(slug, out({slot_name: after}).model_dump())
        items = ArtifactService().list(slug)
        assert items["criteria"]["stale"] is False
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_related_slot_change_marks_artifact_stale():
    """Invariant 1: an artifact is stale when something it rests on changed (#286)."""
    # The other side: a material change to a slot the artifact DOES consume flags it stale.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-stale-related"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"workflow": slot(50, "inferred", "high")}).model_dump())
    ArtifactService().save(slug, "criteria", "# criteria", source_revision=1)  # criteria consumes workflow
    try:
        wf = {**slot(95, "explicit", "high"), "value": "draft → issued → archived"}
        svc.update_model(slug, out({"workflow": wf}).model_dump())
        items = ArtifactService().list(slug)
        assert items["criteria"]["stale"] is True
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_first_apply_does_not_invalidate_its_own_reasoning():
    # A first apply of a model that already carries decisions/challenges must NOT report them as invalidated.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-first-apply"
    store.create_session(slug, "req")
    model = EngineOutput.model_validate({
        "model": full_slots(workflow=slot(80, "explicit", "high"),
                            permissions=slot(75, "explicit", "high")),
        "questions": [], "summary": {"objective": "Invoice lifecycle"},
        "decisions": [DesignDecision(decision="Draft-first invoices reviewed by Finance",
                                     derived_from=["workflow", "permissions"]).model_dump()],
        "challenges": [Challenge(headline="Invoice at signature", premise="p", alternative="a",
                                 consequence="c", recommendation="r",
                                 contests=["workflow"]).model_dump()],
    })
    try:
        result = svc.update_model(slug, model.model_dump())
        assert result.invalidated_decisions == []      # its own reasoning is fresh, not stale
        assert result.invalidated_challenges == []
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_second_apply_invalidates_prior_reasoning_a_change_unseats():
    # The other side: once reasoning is established, a later change that reaches a slot it rests on DOES invalidate it — the behaviour the first-apply guard must not suppress.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-second-apply"
    store.create_session(slug, "req")
    first = EngineOutput.model_validate({
        "model": full_slots(workflow=slot(80, "inferred", "high")),
        "questions": [], "summary": {"objective": "Invoice lifecycle"},
        "decisions": [DesignDecision(decision="Draft-first invoices reviewed by Finance",
                                     derived_from=["workflow"]).model_dump()],
    })
    svc.update_model(slug, first.model_dump())
    try:
        # a material change to workflow, and the refinement turn drops the decision from its reply
        second = out({"workflow": {**slot(95, "explicit", "high"), "value": "draft → issued → archived"}})
        result = svc.update_model(slug, second.model_dump())
        assert "Draft-first invoices reviewed by Finance" in result.invalidated_decisions
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_expected_revision_precondition_blocks_a_stale_write():
    # Optimistic locking: a writer that expects an out-of-date revision is rejected rather than landing silently on top of another update — the guarantee a concurrent Web service needs.
    from requivo.core.errors import RevisionConflictError
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-lock"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"workflow": slot(60, "inferred", "high")}).model_dump())  # → revision 1
    try:
        with pytest.raises(RevisionConflictError):   # a racer still thinks it is at revision 0
            svc.update_model(slug, out({"workflow": slot(80, "explicit", "high")}).model_dump(),
                             expected_revision=0)
        r = svc.update_model(slug, out({"workflow": slot(80, "explicit", "high")}).model_dump(),
                             expected_revision=1)     # the right expectation applies cleanly
        assert r.revision == 2
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_each_revision_records_its_provenance():
    # Provenance is per-revision: a session's model is moved by more than one surface over its life.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-provenance"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"workflow": slot(60, "inferred", "high")}).model_dump(),
                     provenance={"provider": "anthropic", "surface": "cli-discover", "model_name": "claude-x"})
    svc.update_model(slug, out({"workflow": {**slot(90, "explicit", "high"), "value": "a → b"}}).model_dump(),
                     provenance={"provider": "claude-code", "surface": "cli-apply"})
    try:
        revs = store.read_meta(slug).revisions
        assert [r.revision for r in revs] == [1, 2]
        assert revs[0].previous_revision is None and revs[1].previous_revision == 1
        assert revs[0].surface == "cli-discover" and revs[0].provider == "anthropic"
        assert revs[1].surface == "cli-apply" and revs[1].provider == "claude-code"
        assert all(r.model_hash.startswith("sha256:") for r in revs)
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_pc_answer_warns_when_a_turn_makes_a_generated_artifact_stale():
    with _model_in_out("clitest-stale") as p:
        slug = p.parent.name
        # a real slot an artifact consumes, and an already-generated PRD tracked in the session
        wf = {**slot(60, "inferred", "high"), "value": "draft → issued"}
        store.save_revision(slug, out({"workflow": wf}))
        # `_model_in_out` applied revision 1 and the line above applied revision 2 (#6).
        ArtifactService().save(slug, "prd", "# stale PRD", source_revision=2)
        turn2 = json.dumps({
            "model": full_slots(workflow={"completeness": 95, "confidence": "explicit",
                                          "impact": "high", "value": "draft → issued → paid → archived"}),
            "questions": [], "summary": {"objective": "Document lifecycle"},
        })
        text = _run_app(["answer", slug, "It also has an archived state."],
                        client=FakeClient(turn2))
        assert "STALE" in text and "prd.md" in text
        assert "Workflow" in text  # the changed slot is named in the warning


# ── Tier 3: the artifact-type vocabulary agrees with itself (#270) ────────────
# One concept -- "the artifact types" -- is keyed into _GENERATORS.
#
# Until #556 there were two near-identical filename tables here.
#
# **The shape of the guard, decided rather than defaulted.** Not a registry-of-registries (a ninth table that can itself drift) and not N^2 pairwise assertions (the relationship count grows with every new table, most of them restating the same fact twice).


def _artifact_vocabulary_mismatches(*, slots_raw, generators, op_prompts, writers, generatable,
                                    artifact_filenames, artifact_labels) -> list[str]:
    """Every relationship the real tables must satisfy, checked through the same argument names whether the
    tables are the real module-level ones or a deliberately broken fixture copy."""
    canonical = set(slots_raw)
    problems = []

    if set(generators) != canonical:
        problems.append(
            f"_GENERATORS {sorted(set(generators) ^ canonical)} disagrees with _ARTIFACT_SLOTS_RAW "
            "-- every generator needs a staleness entry, and every staleness entry needs a generator")
    if set(op_prompts) != canonical | {"analyze"}:
        problems.append(
            f"_OP_PROMPTS {sorted(set(op_prompts) ^ (canonical | {'analyze'}))} disagrees with "
            "_ARTIFACT_SLOTS_RAW + {'analyze'}")
    if not set(writers) <= canonical:
        problems.append(f"_WRITERS {sorted(set(writers) - canonical)} not in _ARTIFACT_SLOTS_RAW")
    if not set(generatable) <= canonical:
        problems.append(f"GENERATABLE {sorted(set(generatable) - canonical)} not in _ARTIFACT_SLOTS_RAW")
    if not set(artifact_filenames) <= canonical:
        problems.append(
            f"ARTIFACT_FILENAMES {sorted(set(artifact_filenames) - canonical)} not in "
            "_ARTIFACT_SLOTS_RAW -- this is the dangerous one: a type saved under a real filename "
            "here is never flagged stale, because _stale_since reads REASONING_CONSUMERS and "
            "propagate() off _ARTIFACT_SLOTS_RAW alone")
    if not set(generatable) <= set(artifact_filenames):
        problems.append(
            f"GENERATABLE {sorted(set(generatable) - set(artifact_filenames))} has no "
            "ARTIFACT_FILENAMES entry -- generate() would produce it with nowhere to save it, and "
            "services/sessions.py's _resolve_stale (which reads ARTIFACT_FILENAMES directly since "
            "#556) would never auto-flag it stale either")
    if not set(artifact_filenames) <= set(artifact_labels):
        problems.append(
            f"ARTIFACT_FILENAMES {sorted(set(artifact_filenames) - set(artifact_labels))} has no "
            "ARTIFACT_LABELS entry -- the Web would show the raw type string instead of a label")
    return problems


def test_the_real_artifact_registries_agree_on_their_key_sets():
    """#270. The must-not-fire half: on the tables actually shipped, every relationship holds."""
    problems = _artifact_vocabulary_mismatches(
        slots_raw=_ARTIFACT_SLOTS_RAW, generators=_GENERATORS, op_prompts=_OP_PROMPTS,
        writers=_WRITERS, generatable=GENERATABLE, artifact_filenames=ARTIFACT_FILENAMES,
        artifact_labels=ARTIFACT_LABELS)
    joined = chr(10).join(problems)
    assert not problems, joined


def _real_tables() -> dict:
    return {
        "slots_raw": dict(_ARTIFACT_SLOTS_RAW), "generators": dict(_GENERATORS),
        "op_prompts": dict(_OP_PROMPTS), "writers": dict(_WRITERS),
        "generatable": tuple(GENERATABLE), "artifact_filenames": dict(ARTIFACT_FILENAMES),
        "artifact_labels": dict(ARTIFACT_LABELS),
    }


def _run_mismatches(tables: dict) -> list[str]:
    return _artifact_vocabulary_mismatches(
        slots_raw=tables["slots_raw"], generators=tables["generators"],
        op_prompts=tables["op_prompts"], writers=tables["writers"],
        generatable=tables["generatable"], artifact_filenames=tables["artifact_filenames"],
        artifact_labels=tables["artifact_labels"])


@pytest.mark.parametrize("table_name", ["generators", "writers", "artifact_filenames"])
def test_a_type_missing_its__ARTIFACT_SLOTS_RAW_entry_is_caught(table_name):
    """The positive control #270 asks for by name: a registry with a deliberately added type that has no
    _ARTIFACT_SLOTS_RAW entry must fail the same check that passes on the real tables above."""
    tables = _real_tables()
    if table_name == "generators":
        tables["generators"]["dummy"] = lambda *a, **k: None
    elif table_name == "writers":
        tables["writers"]["dummy"] = lambda a: ""
    elif table_name == "artifact_filenames":
        tables["artifact_filenames"]["dummy"] = "dummy.md"

    problems = _run_mismatches(tables)
    assert any("dummy" in p for p in problems), (
        f"a 'dummy' type added to {table_name} with no _ARTIFACT_SLOTS_RAW entry must be caught: {problems}")
