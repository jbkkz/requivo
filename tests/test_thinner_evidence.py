"""A decision derived while its evidence was thinner than it is now (#493).

`propagate` answers *a slot changed, what rests on it?* What it could not represent is the case the
external validation on #489 hit: a slot moved *toward* being filled -- an empty `current_process`
became a measured one -- and the measurement undermined a decision recorded against the earlier,
thinner state of that same slot. Everyone is pleased the slot got filled, so nobody re-reads the
decision.

The cheap, mechanical version is what ships here, and its wording is deliberate: a decision is
*derived from thinner evidence than exists now, worth re-reading* -- never *contradicted*. Whether a
measurement disagrees with a decision is a judgment over both, which belongs to the assessment and
costs a provider call; this is a comparison of two confidence values and costs nothing.

Three layers, three tests each way:

* `core.dependencies.thinner_evidence` -- two models in, a report out, no IO. Both directions are
  asserted, plus the two ways it can *fail to look*, because a check that prints the same thing for
  *clean* and *could not look* is not finished (`CLAUDE.md`, "Build the third state").
* `SessionService.thinner_evidence` -- walks the frozen revisions to find where each decision was
  first recorded, which is the derivation revision. Content-derived ids (invariant 5) mean a
  reworded decision counts as newly derived at its rewording; that is the accepted limit and it is
  pinned here rather than left implicit.
* `web.viewmodels.status.evidence_view` -- translation only (`CLAUDE.md`, "Two vocabularies").
"""

from __future__ import annotations

import json

import pytest
from _fakes import _run_app, full_slots, slot

from requivo.core import persistence as store
from requivo.core.analysis import slot_label
from requivo.core.contracts import DesignDecision, EngineOutput
from requivo.core.dependencies import EvidenceReport, ImpactReport, propagate, thinner_evidence
from requivo.core.errors import ModelUnreadableError
from requivo.services.sessions import SessionService
from requivo.web.viewmodels.status import evidence_view


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


DECISION = "Cut the CI matrix to four legs"
# The schema's own label for `current_process`, read rather than restated so a relabel in
# `model_schema.json` does not turn this file red for a reason unrelated to its name.
CURRENT_PROCESS = slot_label("current_process")


def _model(*decisions, **slots) -> EngineOutput:
    return EngineOutput.model_validate({
        "model": full_slots(**slots), "questions": [],
        "summary": {"objective": "A faster pipeline"},
        "decisions": [d.model_dump() for d in decisions],
    })


def _decision(*derived_from):
    return DesignDecision(decision=DECISION, derived_from=list(derived_from))


# -- the pure comparison --------------------------------------------------------------------------


def test_a_decision_derived_from_an_empty_slot_that_is_now_explicit_is_flagged():
    then = _model(_decision("current_process"), current_process=slot(0, "empty", "high"))
    now = _model(_decision("current_process"), current_process=slot(90, "explicit", "high"))
    report = thinner_evidence(then, now)
    assert [f.decision for f in report.flagged] == [DECISION]
    assert report.flagged[0].thickened == [CURRENT_PROCESS]
    assert report.could_not_tell == [] and report.reviewed == 1


def test_a_decision_derived_from_an_inferred_slot_that_is_now_explicit_is_flagged():
    then = _model(_decision("current_process"), current_process=slot(40, "inferred", "high"))
    now = _model(_decision("current_process"), current_process=slot(90, "explicit", "high"))
    assert [f.decision for f in thinner_evidence(then, now).flagged] == [DECISION]


def test_a_decision_whose_slot_was_already_explicit_is_not_flagged():
    """The must-not-fire half. `reviewed == 1` is what separates *looked and found nothing* from
    *never looked*: a report that is empty because no decision was examined reads identically."""
    then = _model(_decision("current_process"), current_process=slot(70, "explicit", "high"))
    now = _model(_decision("current_process"), current_process=slot(95, "explicit", "high"))
    report = thinner_evidence(then, now)
    assert report.flagged == [] and report.could_not_tell == []
    assert report.reviewed == 1


def test_a_slot_the_decision_does_not_rest_on_never_flags_it():
    """`workflow` thickens, but the decision rests on `permissions` only -- the DAG edge is the
    whole rule, exactly as it is for `propagate`."""
    then = _model(_decision("permissions"), workflow=slot(0, "empty", "high"),
                  permissions=slot(60, "explicit", "high"))
    now = _model(_decision("permissions"), workflow=slot(90, "explicit", "high"),
                 permissions=slot(60, "explicit", "high"))
    report = thinner_evidence(then, now)
    assert report.flagged == [] and report.reviewed == 1


def test_a_slot_that_thinned_or_stayed_thin_is_not_a_flag():
    """Only the *thickened* direction is the finding. explicit -> inferred is a slot that moved,
    which `propagate`/`diff_models` already report; inferred -> inferred is nothing."""
    thinned = thinner_evidence(
        _model(_decision("workflow"), workflow=slot(70, "explicit", "high")),
        _model(_decision("workflow"), workflow=slot(40, "inferred", "high")))
    unchanged = thinner_evidence(
        _model(_decision("workflow"), workflow=slot(40, "inferred", "high")),
        _model(_decision("workflow"), workflow=slot(40, "inferred", "high")))
    assert thinned.flagged == [] and unchanged.flagged == []
    assert thinned.reviewed == unchanged.reviewed == 1


def test_a_decision_with_no_derived_from_is_could_not_tell_not_clean():
    """A decision recording no slots it rests on cannot be reviewed. That is the third state, not a
    clean bill: `reviewed` counts it and `could_not_tell` names it."""
    then = _model(_decision(), current_process=slot(0, "empty", "high"))
    now = _model(_decision(), current_process=slot(90, "explicit", "high"))
    report = thinner_evidence(then, now)
    assert report.flagged == []
    assert [u.decision for u in report.could_not_tell] == [DECISION]
    assert "no slots" in report.could_not_tell[0].reason
    assert report.reviewed == 1


def test_a_decision_absent_from_the_earlier_model_is_could_not_tell():
    then = _model(current_process=slot(0, "empty", "high"))
    now = _model(_decision("current_process"), current_process=slot(90, "explicit", "high"))
    report = thinner_evidence(then, now)
    assert report.flagged == []
    assert [u.decision for u in report.could_not_tell] == [DECISION]


def test_a_slot_missing_from_the_earlier_model_is_could_not_tell():
    """Permissive read (invariant 8): a frozen model from a Requivo whose schema lacked the slot is
    not a flag and not a crash -- the decision is named as unreviewable, with the slot it needed."""
    then = _model(_decision("current_process"), current_process=slot(0, "empty", "high"))
    then_dict = then.model_dump()
    del then_dict["model"]["current_process"]
    then = EngineOutput.model_validate(then_dict)
    now = _model(_decision("current_process"), current_process=slot(90, "explicit", "high"))
    report = thinner_evidence(then, now)
    assert report.flagged == []
    assert [u.decision for u in report.could_not_tell] == [DECISION]
    assert CURRENT_PROCESS in report.could_not_tell[0].reason


def test_the_report_dict_carries_all_three_states_and_the_impact_report_carries_it():
    then = _model(_decision("current_process"), _decision_named("Keep the matrix", "permissions"),
                  current_process=slot(0, "empty", "high"))
    now = _model(_decision("current_process"), _decision_named("Keep the matrix", "permissions"),
                 current_process=slot(90, "explicit", "high"))
    d = thinner_evidence(then, now).to_dict()
    assert set(d) == {"reviewed", "flagged", "could_not_tell"}
    assert d["reviewed"] == 2 and len(d["flagged"]) == 1 and d["could_not_tell"] == []
    assert set(d["flagged"][0]) == {"decision", "id", "thickened", "derived_at"}
    # The pure comparison knows no revision numbers; `derived_at` is the service's to fill.
    assert d["flagged"][0]["derived_at"] is None
    # `propagate` alone has no revision history to review against: `evidence` is None -- *not
    # reviewed* -- and the wire shape says so rather than pretending an empty review happened.
    assert propagate(now, ["current_process"]).to_dict()["evidence"] is None
    rep = ImpactReport(changed=[], evidence=EvidenceReport())
    assert rep.to_dict()["evidence"] == {"reviewed": 0, "flagged": [], "could_not_tell": []}


def _decision_named(text, *derived_from):
    return DesignDecision(decision=text, derived_from=list(derived_from))


# -- the service: which revision a decision was derived at ----------------------------------------


def _walk(svc, slug, *models):
    """Apply `models` in order as successive revisions."""
    for m in models:
        svc.update_model(slug, m.model_dump())


def test_the_issues_own_shape_fires_on_impact():
    """The session #493 describes: a decision derived while `current_process` was empty, then the
    slot measured. `requivo impact <slug> current_process` names the decision as worth re-reading,
    with the revision it was derived at."""
    svc = SessionService()
    svc.create_session("Speed up CI.", slug="ci")
    _walk(svc, "ci",
          _model(_decision("current_process"), current_process=slot(0, "empty", "high")),
          _model(_decision("current_process"), current_process=slot(90, "explicit", "high")))

    report = svc.thinner_evidence("ci")
    assert [(f.decision, f.derived_at, f.thickened) for f in report.flagged] == [
        (DECISION, 1, [CURRENT_PROCESS])]
    assert report.reviewed == 1 and report.could_not_tell == []

    text = _run_app(["impact", "ci", "current_process"])
    assert "THINNER EVIDENCE" in text and DECISION in text
    assert "revision 1" in text and CURRENT_PROCESS in text
    assert "contradict" not in text.lower()


def test_a_decision_derived_after_the_slot_was_measured_is_not_flagged_on_impact():
    """The must-not-fire control for the test above, through the same verb: the slot thickened at
    revision 2 and the decision was first recorded at revision 3, so it rests on the measured
    value and there is nothing to re-read. `reviewed == 1` keeps this from passing on a review that
    never ran."""
    svc = SessionService()
    svc.create_session("Speed up CI.", slug="ci")
    _walk(svc, "ci",
          _model(current_process=slot(0, "empty", "high")),
          _model(current_process=slot(90, "explicit", "high")),
          _model(_decision("current_process"), current_process=slot(90, "explicit", "high")))
    report = svc.thinner_evidence("ci")
    assert report.flagged == [] and report.could_not_tell == [] and report.reviewed == 1

    text = _run_app(["impact", "ci", "current_process"])
    assert "THINNER EVIDENCE" not in text
    assert "1 of 1 decision(s) checked, none rests on thinner evidence" in text


def test_the_derivation_revision_is_the_earliest_that_carries_the_decision():
    """Three revisions: derived at 1 (empty), still present at 2 (inferred), explicit at 3. The
    comparison runs against revision 1, not 2 -- a later revision that also carries the decision is
    not where it was derived."""
    svc = SessionService()
    svc.create_session("Speed up CI.", slug="ci")
    _walk(svc, "ci",
          _model(_decision("current_process"), current_process=slot(0, "empty", "high")),
          _model(_decision("current_process"), current_process=slot(40, "inferred", "high")),
          _model(_decision("current_process"), current_process=slot(90, "explicit", "high")))
    report = svc.thinner_evidence("ci")
    assert [f.derived_at for f in report.flagged] == [1]


def test_a_reworded_decision_counts_as_newly_derived_at_its_rewording():
    """The accepted limit (invariant 5: ids are content-derived). Reworded at revision 2 after the
    slot was measured, the decision is a *new* id first recorded against explicit evidence, so it is
    not flagged even though a reader would call it the same decision. Pinned so the limit is a
    sentence somebody can read rather than a surprise."""
    svc = SessionService()
    svc.create_session("Speed up CI.", slug="ci")
    _walk(svc, "ci",
          _model(_decision("current_process"), current_process=slot(0, "empty", "high")),
          _model(_decision_named("Cut the CI matrix down to four legs", "current_process"),
                 current_process=slot(90, "explicit", "high")))
    report = svc.thinner_evidence("ci")
    assert report.flagged == [] and report.reviewed == 1


def test_a_revision_from_an_older_requivo_without_confidence_data_is_could_not_tell():
    """Permissive read, service side (invariant 8). A frozen revision this Requivo cannot read --
    here, one whose slot carries no `confidence` at all -- makes every decision not yet located a
    *could not tell*, naming the revision. Never a flag, never a crash; `requivo impact` still
    answers."""
    svc = SessionService()
    svc.create_session("Speed up CI.", slug="ci")
    _walk(svc, "ci",
          _model(_decision("current_process"), current_process=slot(0, "empty", "high")),
          _model(_decision("current_process"), current_process=slot(90, "explicit", "high")))
    frozen = store.canonical_dir("ci") / "revisions" / "0001-model.json"
    payload = json.loads(frozen.read_text(encoding="utf-8"))
    del payload["model"]["current_process"]["confidence"]
    frozen.write_text(json.dumps(payload), encoding="utf-8")
    # Positive control: the store really does refuse that file rather than reading it leniently.
    with pytest.raises(ModelUnreadableError):
        svc.load_revision("ci", 1)

    report = svc.thinner_evidence("ci")
    assert report.flagged == []
    assert [u.decision for u in report.could_not_tell] == [DECISION]
    assert "revision 1" in report.could_not_tell[0].reason
    assert report.reviewed == 1
    text = _run_app(["impact", "ci", "current_process"])
    assert "Could not check" in text and "revision 1 could not be read" in text
    assert "THINNER EVIDENCE" not in text


def test_a_bare_model_file_is_not_reviewed_and_says_so(tmp_path):
    """`impact` also accepts a model.json path, which has no revision history to walk. That is
    *not reviewed*, rendered as such -- distinct from a session where every decision checked out."""
    p = tmp_path / "loose" / "model.json"
    p.parent.mkdir()
    p.write_text(_model(_decision("current_process"),
                        current_process=slot(90, "explicit", "high")).model_dump_json(),
                 encoding="utf-8")
    text = _run_app(["impact", str(p), "current_process"])
    assert "not reviewed" in text
    assert "none rests on thinner evidence" not in text


def test_the_full_map_form_reviews_the_evidence_too():
    svc = SessionService()
    svc.create_session("Speed up CI.", slug="ci")
    _walk(svc, "ci",
          _model(_decision("current_process"), current_process=slot(0, "empty", "high")),
          _model(_decision("current_process"), current_process=slot(90, "explicit", "high")))
    text = _run_app(["impact", "ci"])
    assert "DEPENDENCY MAP" in text and "THINNER EVIDENCE" in text and DECISION in text


def test_the_service_impact_report_carries_the_review():
    """`SessionService.impact` is what the HTTP API's `/impact` route returns; it carries the
    review under `evidence` rather than leaving the API to compose a second call."""
    svc = SessionService()
    svc.create_session("Speed up CI.", slug="ci")
    _walk(svc, "ci",
          _model(_decision("current_process"), current_process=slot(0, "empty", "high")),
          _model(_decision("current_process"), current_process=slot(90, "explicit", "high")))
    body = svc.impact("ci", ["workflow"]).to_dict()
    assert body["evidence"]["reviewed"] == 1
    assert [f["derived_at"] for f in body["evidence"]["flagged"]] == [1]


# -- the web view model: translation only -------------------------------------------------------


def test_evidence_view_relabels_the_core_report_without_recomputing_it():
    then = _model(_decision("current_process"), _decision_named("Keep the matrix"),
                  current_process=slot(0, "empty", "high"))
    now = _model(_decision("current_process"), _decision_named("Keep the matrix"),
                 current_process=slot(90, "explicit", "high"))
    report = thinner_evidence(then, now)
    for f in report.flagged:
        f.derived_at = 1
    view = evidence_view(report)
    assert view["reviewed"] is True
    flagged_id = report.flagged[0].id
    assert "worth re-reading" in view["reread"][flagged_id].lower()
    assert CURRENT_PROCESS in view["reread"][flagged_id]
    assert "revision 1" in view["reread"][flagged_id]
    assert "contradict" not in view["reread"][flagged_id].lower()
    unknown_id = report.could_not_tell[0].id
    assert unknown_id in view["unchecked"] and unknown_id not in view["reread"]


def test_evidence_view_of_no_review_is_not_reviewed_not_clean():
    view = evidence_view(None)
    assert view == {"reviewed": False, "reread": {}, "unchecked": {}}


def test_the_traceability_panel_marks_the_decision_worth_re_reading(client):
    svc = SessionService()
    svc.create_session("Speed up CI.", slug="ci")
    _walk(svc, "ci",
          _model(_decision("current_process"), _decision_named("Keep the matrix", "permissions"),
                 current_process=slot(0, "empty", "high"), permissions=slot(70, "explicit", "high")),
          _model(_decision("current_process"), _decision_named("Keep the matrix", "permissions"),
                 current_process=slot(90, "explicit", "high"), permissions=slot(70, "explicit", "high")))
    html = client.get("/sessions/ci").text
    assert DECISION in html and "Keep the matrix" in html
    assert html.count("Worth re-reading") == 1
    assert CURRENT_PROCESS in html


@pytest.fixture
def client():
    """The Web's own everyday client, built the way `tests/web/conftest.py` builds it (a cross-site
    token on every request); imported here rather than reached into, because that conftest is scoped
    to its directory."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from requivo.web.app import create_app
    from requivo.web.security import CSRF_HEADER, csrf_token

    c = TestClient(create_app(), base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    c.headers[CSRF_HEADER] = csrf_token()
    del fastapi
    return c
