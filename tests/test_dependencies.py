"""The dependency DAG, and the staleness it drives.

Split out of `test_engine.py` (#72). One file rather than two because invariant 1 makes them one
subject: *staleness is the dependency graph, never the revision number.* So the pure-logic half
(`propagate`, `diff_models`, `resolve_slots`, `ARTIFACT_SLOTS`) and the on-disk half (what an apply
actually marks stale, and what the `impact` and `answer` verbs report) are the same rule observed at
two depths.

Two tests here are lodgers rather than residents — `test_expected_revision_precondition_blocks_a_stale_write`
(optimistic locking) and `test_each_revision_records_its_provenance` (the revision log). They belong
with `test_integrity.py` and `test_artifact_provenance.py` by subject. They stayed because those files
take an opt-in `workspace` fixture and these tests take no arguments: moving them means adding a
parameter, and #72 moves test bodies unchanged or not at all. Worth doing separately, with the
signature change visible as its own diff.
"""
import json
import shutil

import pytest
from _fakes import FakeClient, _model_in_out, _run_app, full_slots, out, slot

from requivo.core import persistence as store
from requivo.core.contracts import Challenge, DesignDecision, EngineOutput, schema_slot_ids
from requivo.core.dependencies import (
    _ARTIFACT_SLOTS_RAW,
    ARTIFACT_FILENAMES,
    ARTIFACT_FILES,
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
    """Every test in this module writes sessions/artifacts into an isolated temp workspace, never the
    real repo. Points both the canonical root (.requivo/sessions) and the legacy root (out/) at tmp."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


# ── Tier 2: the dependency DAG (impact propagation) ───────────────────────────
# Pure logic — no API. A change to a slot propagates to the decisions that rest on
# it and the artifacts that consume it.


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


def test_propagate_reaches_only_the_assessment_for_an_otherwise_isolated_slot():
    # current_process feeds no buildable deliverable and no decision rests on it. The solution
    # assessment is the exception by design: it is a judgment over the whole model, so it rests on
    # every slot — describing the as-is process differently does change the assessment on disk.
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


def test_diff_models_flags_a_removed_slot():
    """Invariant 1's symmetry. Reasoning a turn merely *omits* is not a removal — but that is
    resolved *before* the diff, by `ModelProposal.resolve` (invariant 10), never inside it. By the
    time two models reach `diff_models`/`diff_reasoning` both are complete, so the diff is
    symmetric: an empty collection facing a populated one is a real deletion, and a slot present
    before and gone after is a real change (moved here from CLAUDE.md by #286)."""
    # A slot present before and gone after must register as a change — otherwise a decision or artifact
    # resting on it could go stale silently. (In practice the completeness invariant prevents a real
    # discovery from dropping a slot, but the diff must not depend on that upstream guarantee.)
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
# The test above only checks the subset direction -- every id an artifact set names is real. Nothing
# checked the other one: that a slot the *schema* defines is named by at least one of them. A slot
# that reaches none of prd/stories/estimate/criteria/epic/release -- only `brief`'s '*' catch-all --
# marks nothing stale for any specific deliverable when it changes. That is invariant 1's exact
# failure shape, and it was reachable by the most routine change the schema will ever see: adding a
# slot and forgetting to add it to `_ARTIFACT_SLOTS_RAW`.
#
# Slots that genuinely feed no specific artifact -- only the assessment's judgment over the whole
# model, via `brief` -- are named here with a reason, the same allowlist idiom `tests/test_boundaries.py`
# already uses for its own two guards. A slot lands in this dict because someone checked *why* no
# artifact needs it, not because the guard below was in the way.
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
    """#269. `schema_slot_ids()` is the single source of the required set (it already excludes
    `optional: true` slots -- `config_vs_custom` is a platform edge some products never populate, and
    requiring it in some artifact's set the way a normal slot is required would assert a fact the
    schema itself does not claim).

    A required slot must appear in some `artifact_slots()` value that is not `brief`'s `*` entry
    (every slot is trivially in that one), or be named in `_SLOTS_WITH_NO_SPECIFIC_ARTIFACT` with a
    reason. A slot in neither is the silent gap #269 found."""
    _, required = schema_slot_ids()
    amap = artifact_slots()
    specific = set().union(*(slots for name, slots in amap.items() if name != "brief"))
    exempt = set(_SLOTS_WITH_NO_SPECIFIC_ARTIFACT)

    uncovered = required - specific - exempt
    assert not uncovered, (
        f"these required slot(s) are consumed by no specific artifact and are not exempted: "
        f"{sorted(uncovered)} -- add each to an artifact's set in _ARTIFACT_SLOTS_RAW, or to "
        f"_SLOTS_WITH_NO_SPECIFIC_ARTIFACT with a reason.")

    # The mirror direction: a stale exemption -- naming a slot that no longer exists, or one an
    # artifact-map edit has since started consuming -- must fail too, or the list silently stops
    # meaning anything (the same reasoning docs/compatibility.md's own #14 gives for a stale allowlist
    # entry: a promise about something that is no longer the case).
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


def test_pc_impact_no_slots_prints_the_full_map():
    with _model_in_out("clitest-impact-map") as p:
        store.save_revision(p.parent.name, _out_with_decisions())
        text = _run_app(["impact", str(p)])
        assert "DEPENDENCY MAP" in text


# ── Tier 2 (B): change-detection — stale artifacts on disk ────────────────────


def test_unrelated_slot_change_keeps_artifact_fresh():
    # The freshness fix: an artifact goes stale only when the change reaches a slot it consumes — not
    # on every revision bump. criteria consumes {workflow, business_rules, permissions, edge_cases,
    # acceptance}; success_metrics is outside that set, so a material change to it leaves criteria fresh.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-fresh-unrelated"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"success_metrics": slot(40, "inferred", "high")}).model_dump())
    ArtifactService().save(slug, "criteria", "# criteria", source_revision=1)  # generated at revision 1
    try:
        # confidence moves inferred → explicit on an UNRELATED slot: a real change, new revision.
        svc.update_model(slug, out({"success_metrics": slot(90, "explicit", "high")}).model_dump())
        items = ArtifactService().list(slug)
        assert items["criteria"]["stale"] is False        # revision advanced, but criteria is untouched
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_completeness_only_change_keeps_artifact_fresh():
    # A completeness-only bump on a CONSUMED slot is not a material change (diff_models ignores
    # completeness), so it must not invalidate the artifact — completeness is progress noise, not signal.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-fresh-completeness"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"workflow": slot(50, "explicit", "high")}).model_dump())
    ArtifactService().save(slug, "criteria", "# criteria", source_revision=1)  # criteria consumes workflow
    try:
        svc.update_model(slug, out({"workflow": slot(95, "explicit", "high")}).model_dump())  # only %
        items = ArtifactService().list(slug)
        assert items["criteria"]["stale"] is False
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_related_slot_change_marks_artifact_stale():
    """Invariant 1: an artifact is stale when something it rests on changed — never because the
    session moved past its source revision, which is *provenance*. Two edge sets feed the verdict:
    the slots an artifact consumes (`ARTIFACT_SLOTS`) and the reasoning layer
    (`REASONING_CONSUMERS` — every generator, since each is prompted with the full model, so
    `diff_reasoning` invalidates on its own; see
    `test_reasoning_that_changes_without_a_slot_moving_still_invalidates`). Report
    `ArtifactStatus.stale`; never infer staleness by comparing revisions — the control for that half
    is `test_an_older_revision_that_missed_the_artifact_leaves_it_fresh` (moved here from
    CLAUDE.md by #286)."""
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
    # A first apply of a model that already carries decisions/challenges must NOT report them as
    # invalidated: they were proposed FOR this state, there is no prior reasoning to unseat. The old
    # code propagated over `new` when there was no `current`, flagging a model's own reasoning stale.
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
    # The other side: once reasoning is established, a later change that reaches a slot it rests on
    # DOES invalidate it — the behaviour the first-apply guard must not suppress.
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
    # Optimistic locking: a writer that expects an out-of-date revision is rejected rather than landing
    # silently on top of another update — the guarantee a concurrent Web service needs.
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
    # Provenance is per-revision: a session's model is moved by more than one surface over its life, so
    # each revision records who produced it, what it succeeded, and a content hash.
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
        # `_model_in_out` applied revision 1 and the line above applied revision 2, so 2 is what this
        # PRD was generated from — stated by the caller rather than assumed by the service (#6).
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
# One concept -- "the artifact types" -- is keyed into _GENERATORS, _OP_PROMPTS
# (providers/anthropic/generators.py), _WRITERS, GENERATABLE (services/discovery.py),
# _ARTIFACT_SLOTS_RAW, ARTIFACT_FILES, ARTIFACT_FILENAMES, REASONING_CONSUMERS
# (core/dependencies.py), and ARTIFACT_LABELS (web/viewmodels/labels.py). Nothing asserted they
# agree, and the dangerous drift is silent: a type present in ARTIFACT_FILENAMES/_GENERATORS/
# _WRITERS but missing from _ARTIFACT_SLOTS_RAW is never flagged stale, because
# services/artifacts.py's _stale_since reads REASONING_CONSUMERS and propagate() off that one map
# alone -- exactly invariant 1's "a stale document reports itself as up to date" failure, and the
# most routine change this vocabulary will ever see (a new generator) is exactly what triggers it.
#
# **The shape of the guard, decided rather than defaulted.** Not a registry-of-registries (a ninth
# table that can itself drift) and not N^2 pairwise assertions (the relationship count grows with
# every new table, most of them restating the same fact twice). `_ARTIFACT_SLOTS_RAW` is the one
# *named* canonical source, and every other table is asserted against it rather than against each
# other: `REASONING_CONSUMERS` is already mechanically derived from it, and it is the map
# `_stale_since` actually reads -- it is the table whose omission is the dangerous one to begin
# with. Adding a ninth table costs one more relationship in `_artifact_vocabulary_mismatches`
# rather than a new pairwise matrix.


def _artifact_vocabulary_mismatches(*, slots_raw, generators, op_prompts, writers, generatable,
                                    artifact_filenames, artifact_files, artifact_labels) -> list[str]:
    """Every relationship the real tables must satisfy, checked through the same argument names
    whether the tables are the real module-level ones or a deliberately broken fixture copy -- the
    *same* function has to fail on the fixture below, or the passing test above is untested (the
    module's own "would this test still pass if the code did nothing" bar)."""
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
            "ARTIFACT_FILENAMES entry -- generate() would produce it with nowhere to save it")
    if not set(artifact_filenames) <= set(artifact_labels):
        problems.append(
            f"ARTIFACT_FILENAMES {sorted(set(artifact_filenames) - set(artifact_labels))} has no "
            "ARTIFACT_LABELS entry -- the Web would show the raw type string instead of a label")
    if not set(artifact_files) <= canonical:
        problems.append(
            f"ARTIFACT_FILES {sorted(set(artifact_files) - canonical)} not in _ARTIFACT_SLOTS_RAW")
    if not set(artifact_filenames) <= set(artifact_files):
        problems.append(
            f"ARTIFACT_FILENAMES {sorted(set(artifact_filenames) - set(artifact_files))} has no "
            "ARTIFACT_FILES entry -- this is the second dangerous one, found in review (#270): "
            "services/sessions.py's _resolve_stale iterates `for t in ARTIFACT_FILES` (key "
            "membership, not the value) to decide which already-saved artifacts an ordinary apply "
            "eagerly re-flags stale, so a type absent from ARTIFACT_FILES entirely is never "
            "auto-flagged by that path even though _stale_since (the save-time path, checked above "
            "via _ARTIFACT_SLOTS_RAW) still catches it correctly -- the two staleness paths read two "
            "different tables and previously only one of them was guarded")
    return problems


def test_the_real_artifact_registries_agree_on_their_key_sets():
    """#270. The must-not-fire half: on the tables actually shipped, every relationship holds.
    `test_a_type_missing_its__ARTIFACT_SLOTS_RAW_entry_is_caught` below is the must-fire control
    over the identical helper, so this passing is evidence about the tables and not about the
    check."""
    problems = _artifact_vocabulary_mismatches(
        slots_raw=_ARTIFACT_SLOTS_RAW, generators=_GENERATORS, op_prompts=_OP_PROMPTS,
        writers=_WRITERS, generatable=GENERATABLE, artifact_filenames=ARTIFACT_FILENAMES,
        artifact_files=ARTIFACT_FILES, artifact_labels=ARTIFACT_LABELS)
    joined = chr(10).join(problems)
    assert not problems, joined


def _real_tables() -> dict:
    return {
        "slots_raw": dict(_ARTIFACT_SLOTS_RAW), "generators": dict(_GENERATORS),
        "op_prompts": dict(_OP_PROMPTS), "writers": dict(_WRITERS),
        "generatable": tuple(GENERATABLE), "artifact_filenames": dict(ARTIFACT_FILENAMES),
        "artifact_files": dict(ARTIFACT_FILES), "artifact_labels": dict(ARTIFACT_LABELS),
    }


def _run_mismatches(tables: dict) -> list[str]:
    return _artifact_vocabulary_mismatches(
        slots_raw=tables["slots_raw"], generators=tables["generators"],
        op_prompts=tables["op_prompts"], writers=tables["writers"],
        generatable=tables["generatable"], artifact_filenames=tables["artifact_filenames"],
        artifact_files=tables["artifact_files"], artifact_labels=tables["artifact_labels"])


@pytest.mark.parametrize("table_name", ["generators", "writers", "artifact_filenames"])
def test_a_type_missing_its__ARTIFACT_SLOTS_RAW_entry_is_caught(table_name):
    """The positive control #270 asks for by name: a registry with a deliberately added type that
    has no _ARTIFACT_SLOTS_RAW entry must fail the same check that passes on the real tables above
    -- the exact drift the issue found (a type reaching ARTIFACT_FILENAMES/_GENERATORS/_WRITERS and
    not _ARTIFACT_SLOTS_RAW, silently never flagged stale). A guard that only ever passes on the
    current tables is untested."""
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


def test_a_type_missing_from_ARTIFACT_FILES_is_caught():
    """The second dangerous drift found in review (#270), one table over from the first: a type can
    have a real _ARTIFACT_SLOTS_RAW entry AND a real ARTIFACT_FILENAMES entry and still be silently
    never auto-flagged stale, because `services/sessions.py`'s `_resolve_stale` -- run on every
    apply, not only at save time -- iterates `for t in ARTIFACT_FILES`, a *third* table the original
    version of this guard never checked. Adding a type to both `_ARTIFACT_SLOTS_RAW` and
    `ARTIFACT_FILENAMES` (as a real generator addition would) while leaving `ARTIFACT_FILES` behind
    must be caught."""
    tables = _real_tables()
    tables["slots_raw"]["dummy"] = {"workflow"}
    tables["artifact_filenames"]["dummy"] = "dummy.md"
    # ARTIFACT_FILES deliberately NOT updated -- this is the omission itself.

    problems = _run_mismatches(tables)
    assert any("ARTIFACT_FILES" in p and "dummy" in p for p in problems), (
        f"a type in ARTIFACT_FILENAMES with no ARTIFACT_FILES entry must be caught: {problems}")


def test_ARTIFACT_FILES_and_ARTIFACT_FILENAMES_agree_wherever_both_name_a_file():
    """Two near-identical tables (#270's own open question). Not merged: when this was written
    ARTIFACT_FILES answered for `stories`/`estimate` with `None` ("the provider-path generator does
    not persist this itself"), where ARTIFACT_FILENAMES omitted `estimate` entirely and gave
    `stories` a real filename -- Claude Code could save one even though the provider path never did.
    A merge needs a three-state marker per type and would touch core/persistence.py,
    render/terminal.py and services/sessions.py, none of which that issue's own Scope section
    named. Pinned instead, per the acceptance criteria's own stated alternative: wherever both
    tables name a type, the filename must agree.
    `test_a_filename_disagreement_between_the_two_tables_is_caught` is the must-fire control.

    Since #519 (`decision: the-estimate-graduates`) both types name a file in both tables, so the
    two agree on every key and the `is not None` filter below excludes nothing. It stays because
    the value type still admits `None`, and comparing a `None` against a real string was never the
    disagreement this test exists to catch -- the first run of this test found that out the hard
    way, on `stories`, when the two tables were legitimately answering two different questions
    about the same type."""
    shared = {t for t in set(ARTIFACT_FILES) & set(ARTIFACT_FILENAMES) if ARTIFACT_FILES[t] is not None}
    assert shared, "the two tables share no comparable keys -- this test asserts nothing until they do"
    disagreements = {t: (ARTIFACT_FILES[t], ARTIFACT_FILENAMES[t]) for t in shared
                     if ARTIFACT_FILES[t] != ARTIFACT_FILENAMES[t]}
    assert not disagreements, disagreements


def test_a_filename_disagreement_between_the_two_tables_is_caught():
    """Must-fire control for the test above, over the same comparison (a `None` in ARTIFACT_FILES
    would be a legitimate different answer, not a disagreement -- see above)."""
    broken = dict(ARTIFACT_FILENAMES, brief="wrong.md")
    shared = {t for t in set(ARTIFACT_FILES) & set(broken) if ARTIFACT_FILES[t] is not None}
    disagreements = {t: (ARTIFACT_FILES[t], broken[t]) for t in shared if ARTIFACT_FILES[t] != broken[t]}
    assert disagreements == {"brief": (ARTIFACT_FILES["brief"], "wrong.md")}
