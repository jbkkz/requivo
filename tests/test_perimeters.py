"""The perimeter mechanism (#608): plural schemas, perimeter as session identity, and the one
place the permissive-reader rule is deliberately inverted.

Split out on its own rather than folded into `test_dependencies.py`/`test_sessions_service.py`:
this is a cross-cutting mechanism (contracts, persistence, services, doctor, integrity all move
together), and a reader asking "how does #608 work" should find one file, not eight diffs.
"""
from __future__ import annotations

import json

import pytest

from requivo.core.contracts import EngineOutput, ModelProposal, schema_slot_ids
from requivo.core.dependencies import _ARTIFACT_SLOTS_RAW, ARTIFACT_FILENAMES, artifact_slots
from requivo.core.errors import SessionExistsError, UnknownPerimeterError, UnknownSlotError
from requivo.core.integrity import inspect_session_dir
from requivo.core.perimeters import (
    DEFAULT_PERIMETER,
    GO_TO_MARKET,
    SOFTWARE,
    get_perimeter,
    known_perimeter_ids,
    resolve_perimeter,
)
from requivo.deterministic.doctor import doctor_report
from requivo.providers.anthropic.generators import _GENERATORS, _OP_PROMPTS
from requivo.services.discovery import _WRITERS, GENERATABLE, DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.viewmodels.labels import ARTIFACT_LABELS


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


def _go_to_market_slots() -> dict:
    """A complete go-to-market model, all slots explicit and covered -- the model a real discovery
    reply would carry, built directly from that perimeter's own schema rather than the software
    one every other fixture in this suite assumes."""
    allowed, required = schema_slot_ids(GO_TO_MARKET)
    return {sid: {"completeness": 90, "confidence": "explicit", "impact": "high",
                  "value": "x", "evidence": "y"} for sid in required}


def _go_to_market_out() -> EngineOutput:
    return EngineOutput.model_validate(
        {"model": _go_to_market_slots(), "questions": [], "summary": {"objective": "grow the funnel"}},
        context={"perimeter": GO_TO_MARKET},
    )


def test_a_go_to_market_discovery_completes_through_the_real_provider_completion_path():
    """[P1, review] `_require_complete_model` used to call `completeness_gap(out)` with no
    perimeter, so a complete go-to-market reply was rejected for missing `business_rules` and every
    other software-only required slot -- and adding those to satisfy it then failed the perimeter
    vocabulary check instead, burning every retry. The stub-provider evidence this issue originally
    shipped with never exercised `_complete()`'s `validate` hook at all; this goes through the real
    completion path (`FakeClient` -> `run()` -> `_complete()`) instead of a stub's own `analyze()`."""
    from _fakes import FakeClient

    from requivo.providers.anthropic.generators import run

    reply = json.dumps({"model": _go_to_market_slots(), "questions": [],
                        "summary": {"objective": "grow the funnel"}})
    fake = FakeClient(reply)
    result = run(fake, [{"role": "user", "content": "grow the funnel"}], perimeter=GO_TO_MARKET)
    assert set(result.model) == schema_slot_ids(GO_TO_MARKET)[0]


def test_a_same_text_request_under_a_different_perimeter_does_not_reuse_the_session():
    """[P1, review] `_same_identity`/`_identity_hash` used to compare only the request and the card
    selection, so a revision-zero `software` session and a `go-to-market` request with identical
    text collided on the same slug -- `_same_identity` said "yes, reuse", `DiscoveryService.start`
    then paid for the analysis under the requested perimeter, and `update_model` rejected the result
    against the existing session's vocabulary *after* the call. `create_session_report` runs before
    any provider call in every discovery entry point (invariant 13), so fixing the identity
    comparison itself is what keeps the refusal-or-reuse decision ahead of the paid call -- there is
    no separate ordering to get right here, only a correct comparison."""
    svc = SessionService()
    software_meta = svc.create_session("grow the funnel", perimeter=SOFTWARE)

    gtm_meta, created = svc.create_session_report("grow the funnel", perimeter=GO_TO_MARKET)

    assert created is True
    assert gtm_meta.slug != software_meta.slug
    assert resolve_perimeter(gtm_meta.perimeter) == GO_TO_MARKET
    assert resolve_perimeter(svc.meta(software_meta.slug).perimeter) == SOFTWARE


def test_an_explicit_slug_reused_under_a_different_perimeter_is_refused_before_any_reasoning():
    """The `strict_slug=True` arm (the API's `POST /sessions`) refuses outright, before a
    `DiscoveryService` caller could ever reach a provider call with it: an explicit slug already
    claimed by a different perimeter is exactly the "different identity" `strict_slug` exists to
    refuse rather than silently suffix."""
    svc = SessionService()
    svc.create_session("grow the funnel", slug="gtm", perimeter=SOFTWARE)

    with pytest.raises(SessionExistsError):
        svc.create_session_report("grow the funnel", slug="gtm", perimeter=GO_TO_MARKET,
                                  strict_slug=True)


def test_two_perimeters_are_installed():
    """#608. `known_perimeter_ids()` names both, and each resolves to a real, readable schema."""
    assert known_perimeter_ids() == (GO_TO_MARKET, SOFTWARE)
    for pid in known_perimeter_ids():
        p = get_perimeter(pid)
        assert p.schema_path.is_file()
        assert p.elicitation_path.is_file()
        assert p.engine_guidance_path.is_file()


def test_a_go_to_market_model_validates_and_reaches_readiness_against_its_own_vocabulary():
    """#608 acceptance: a model built under go-to-market validates, and its readiness reasons over
    its own twelve slots, not software's fifteen."""
    out = _go_to_market_out()
    allowed, _ = schema_slot_ids(GO_TO_MARKET)
    assert set(out.model) == allowed
    assert "business_rules" not in allowed  # software-only, must not leak in


def test_a_software_slot_id_is_refused_in_a_go_to_market_model():
    """#608: a slot id valid in one perimeter and not the other is refused in the model itself.
    Must-fire proof: reverting `_context_perimeter`/`_validate_slot_vocabulary` to ignore `info`
    (i.e. always validating against software) makes this pass wrongly -- ran by hand, confirmed red
    is the failure mode this guards."""
    model = _go_to_market_slots()
    model["business_rules"] = {"completeness": 10, "confidence": "empty", "impact": "low"}
    with pytest.raises(Exception, match="business_rules"):
        EngineOutput.model_validate(
            {"model": model, "questions": [], "summary": {"objective": "x"}},
            context={"perimeter": GO_TO_MARKET},
        )


def test_a_go_to_market_slot_id_is_refused_in_a_software_model():
    """The symmetric direction: a go-to-market-only slot id is unknown under the software default."""
    allowed, required = schema_slot_ids(SOFTWARE)
    model = {sid: {"completeness": 90, "confidence": "explicit", "impact": "low", "value": "x"}
             for sid in required}
    model["capacity"] = {"completeness": 10, "confidence": "empty", "impact": "low"}
    with pytest.raises(Exception, match="capacity"):
        EngineOutput.model_validate({"model": model, "questions": [], "summary": {"objective": "x"}})


def test_a_question_targeting_the_other_perimeters_slot_is_refused():
    """The DAG-edge rule extends to `Question.slot` -- a software-only slot named by a question in a
    go-to-market reply is refused the same way an unknown slot in the model itself is."""
    model = _go_to_market_slots()
    with pytest.raises(Exception, match="business_rules"):
        ModelProposal.model_validate(
            {"model": model,
             "questions": [{"q": "?", "slot": "business_rules", "why": "?"}],
             "summary": {"objective": "x"}},
            context={"perimeter": GO_TO_MARKET},
        )


def test_a_dag_edge_targeting_the_other_perimeters_slot_is_refused():
    """`derived_from`/`contests`/`rests_on` are checked against the same vocabulary as the model and
    the questions -- a decision resting on a software-only slot is refused under go-to-market."""
    model = _go_to_market_slots()
    with pytest.raises(Exception, match="business_rules"):
        ModelProposal.model_validate(
            {"model": model, "questions": [], "summary": {"objective": "x"},
             "decisions": [{"decision": "Use channel X", "derived_from": ["business_rules"]}]},
            context={"perimeter": GO_TO_MARKET},
        )


def test_impact_is_scoped_to_the_sessions_own_perimeter():
    """`requivo impact` (`SessionService.impact`) reasons over the session's own vocabulary only: a
    go-to-market session accepts `capacity` and refuses `business_rules` as unknown."""
    svc = SessionService()
    disco = DiscoveryService(provider=_StubProvider(), sessions=svc)
    slug = disco.start("grow the funnel", finalize=False, perimeter=GO_TO_MARKET)
    report = svc.impact(slug, ["capacity"])
    assert report.changed == ["Capacity"]
    with pytest.raises(UnknownSlotError):
        svc.impact(slug, ["business_rules"])


def test_perimeter_is_frozen_at_creation_and_visible_in_status_and_session_show():
    """#608 acceptance: recorded, frozen, and visible through both `status --json` (service) and
    `session show --json` (SessionMeta itself, via extra="allow" round-trip)."""
    svc = SessionService()
    meta = svc.create_session("grow the funnel", slug="gtm", perimeter=GO_TO_MARKET)
    assert meta.perimeter == GO_TO_MARKET
    assert svc.snapshot("gtm").perimeter == GO_TO_MARKET
    # No model yet at revision 0 -- status() still names the perimeter without a model to project.
    payload = json.loads(meta.model_dump_json())
    assert payload["perimeter"] == GO_TO_MARKET


def test_an_unknown_perimeter_is_refused_by_name_by_the_loader():
    """#608's deliberate inversion of invariant 8: the loader (`SessionService.meta` /
    `migrate_session`) refuses a session naming a perimeter this install does not have, by name,
    rather than tolerating or defaulting it."""
    svc = SessionService()
    svc.create_session("a request", slug="unk")
    p = svc.repo.store().canonical_dir("unk") / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["perimeter"] = "space-exploration"
    p.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(UnknownPerimeterError, match="space-exploration"):
        svc.meta("unk")


def test_an_unknown_perimeter_is_refused_by_name_by_session_verify_and_doctor():
    """The same session, read through `inspect_session_dir` (what `session verify` and `doctor`'s
    per-session health scan both call) -- reported as a named `unknown_perimeter` problem rather
    than raised, since this module reports for a caller that decides, and rather than silently
    passing as a session `read_meta` would open fine (invariant 8's ordinary tolerance)."""
    svc = SessionService()
    svc.create_session("a request", slug="unk2")
    d = svc.repo.store().canonical_dir("unk2")
    data = json.loads((d / "session.json").read_text(encoding="utf-8"))
    data["perimeter"] = "space-exploration"
    (d / "session.json").write_text(json.dumps(data), encoding="utf-8")

    findings = inspect_session_dir(d, expected_slug="unk2")
    codes = {f.code for f in findings}
    assert "unknown_perimeter" in codes
    assert all(f.severity == "problem" for f in findings if f.code == "unknown_perimeter")


def test_a_pre_perimeter_session_still_opens_as_software():
    """The forward sibling `test_a_session_written_by_an_older_requivo_still_loads` asks for
    (#608): a session with no `perimeter` key at all -- the shape every session had before this
    issue -- opens without a migration step, reading as the software perimeter."""
    svc = SessionService()
    svc.create_session("an old-shaped session", slug="old")
    p = svc.repo.store().canonical_dir("old") / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "perimeter" not in data or data["perimeter"] is None  # the shape this test assumes
    data.pop("perimeter", None)
    p.write_text(json.dumps(data), encoding="utf-8")

    meta = svc.meta("old")
    assert meta.perimeter is None
    assert resolve_perimeter(meta.perimeter) == SOFTWARE == DEFAULT_PERIMETER
    assert svc.snapshot("old").perimeter == SOFTWARE


def test_doctor_reports_installed_perimeters():
    """#608 acceptance: `requivo doctor` reports the installed perimeters the way it already
    reports the context cards."""
    r = doctor_report()
    assert r["perimeters"]["ok"] is True
    assert set(r["perimeters"]["installed"]) == {GO_TO_MARKET, SOFTWARE}


def test_go_to_market_has_no_registered_artifacts_yet():
    """#607's cost rule ("each new perimeter ships with exactly one artifact... a second is added
    when a user asks") applied to a *first* artifact: go-to-market ships none in #608, so its
    artifact-type set is empty until #609 registers its one artifact."""
    assert get_perimeter(GO_TO_MARKET).artifact_types == frozenset()
    assert artifact_slots(GO_TO_MARKET) == {}


def test_the_real_artifact_registries_agree_on_their_key_sets_per_perimeter():
    """#608 acceptance: the same relationships `test_the_real_artifact_registries_agree_on_their_key_sets`
    pins for the real tables (test_dependencies.py), filtered to each perimeter's own `artifact_types`
    -- trivially true for go-to-market today (it owns no type yet), but this is the guard that would
    catch a future artifact registered under the wrong perimeter."""
    from tests.test_dependencies import _artifact_vocabulary_mismatches  # noqa: PLC0415

    for pid in known_perimeter_ids():
        owned = get_perimeter(pid).artifact_types
        problems = _artifact_vocabulary_mismatches(
            slots_raw={k: v for k, v in _ARTIFACT_SLOTS_RAW.items() if k in owned},
            generators={k: v for k, v in _GENERATORS.items() if k in owned},
            op_prompts={k: v for k, v in _OP_PROMPTS.items() if k in owned or k == "analyze"},
            writers={k: v for k, v in _WRITERS.items() if k in owned},
            generatable=tuple(t for t in GENERATABLE if t in owned),
            artifact_filenames={k: v for k, v in ARTIFACT_FILENAMES.items() if k in owned},
            artifact_labels={k: v for k, v in ARTIFACT_LABELS.items() if k in owned},
        )
        assert not problems, f"perimeter {pid!r}: {problems}"


def test_generate_refuses_an_artifact_type_the_sessions_perimeter_does_not_own():
    """A go-to-market session cannot be handed to a software-only generator (`prd`, `brief`, ...) --
    refused before any provider call, naming the perimeter, rather than validating the reply
    against the wrong schema two layers down."""
    svc = SessionService()
    disco = DiscoveryService(provider=_StubProvider(), sessions=svc)
    slug = disco.start("grow the funnel", finalize=False, perimeter=GO_TO_MARKET)
    with pytest.raises(ValueError, match="go-to-market"):
        disco.generate(slug, "prd")


class _StubProvider:
    """A minimal `ReasoningProvider` for the discovery-service tests above: one `analyze()` that
    returns a complete model under whatever perimeter it is asked for."""

    name = "stub"

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
               perimeter=DEFAULT_PERIMETER):
        allowed, required = schema_slot_ids(perimeter)
        model = {sid: {"completeness": 90, "confidence": "explicit", "impact": "high",
                       "value": "x", "evidence": "y"} for sid in required}
        return EngineOutput.model_validate(
            {"model": model, "questions": [], "summary": {"objective": "grow"}},
            context={"perimeter": perimeter})

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=DEFAULT_PERIMETER):
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": "sha256:x"}

    def generate(self, artifact_type, model, *, only=None, **kwargs):  # pragma: no cover - unreached
        raise AssertionError("no generator should be reached for an unowned artifact type")
