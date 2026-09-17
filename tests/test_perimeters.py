"""The perimeter mechanism (#608): plural schemas, perimeter as session identity, the one place the
permissive-reader rule is inverted -- and the router (#601) that judges which perimeter a first discovery
belongs to, riding #593's claim-judge-reclaim seam."""
from __future__ import annotations

import builtins
import json
import sys

import pytest
from _fakes import _ENGINE_REPLY, _JUDGMENT_REPLY, _ROUTING_REPLY, FakeClient, StubProvider, full_model, run_cli

from conftest import FakeProvider
from requivo.cli import app
from requivo.core.contracts import ContextJudgment, EngineOutput, ModelProposal, PerimeterJudgment, schema_slot_ids
from requivo.core.dependencies import _ARTIFACT_SLOTS_RAW, ARTIFACT_FILENAMES, artifact_slots
from requivo.core.errors import (
    AmbiguousPerimeterError,
    ArtifactTypeNotOwnedError,
    ProviderOutputError,
    RevisionConflictError,
    SessionExistsError,
    UnknownPerimeterError,
    UnknownSlotError,
)
from requivo.core.integrity import inspect_session_dir
from requivo.core.perimeters import (
    DEFAULT_PERIMETER,
    GO_TO_MARKET,
    SOFTWARE,
    get_perimeter,
    known_perimeter_ids,
    perimeter_summaries,
    resolve_perimeter,
)
from requivo.deterministic.doctor import doctor_report
from requivo.providers.anthropic.generators import _GENERATORS, _OP_PROMPTS, judge_perimeter, prompt_version, run
from requivo.providers.anthropic.provider import AnthropicProvider
from requivo.providers.errors import EngineError
from requivo.services.discovery import _WRITERS, GENERATABLE, DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.viewmodels.labels import ARTIFACT_LABELS

pytestmark = pytest.mark.usefixtures("workspace")

_EXPLICIT = {"completeness": 90, "confidence": "explicit", "impact": "high", "value": "x", "evidence": "y"}


def _slots(perimeter: str = GO_TO_MARKET) -> dict:
    """A complete model under `perimeter`, all slots explicit and covered."""
    _, required = schema_slot_ids(perimeter)
    return {sid: dict(_EXPLICIT) for sid in required}


def _gtm_payload() -> dict:
    return {"model": _slots(), "questions": [], "summary": {"objective": "grow the funnel"}}


def _gtm_out() -> EngineOutput:
    return EngineOutput.model_validate(_gtm_payload(), context={"perimeter": GO_TO_MARKET})


class _PerimeterAware(StubProvider):
    """`analyze` answers in whichever perimeter's vocabulary it is asked for."""

    def analyze(self, request, *, perimeter=DEFAULT_PERIMETER, **kwargs):
        self.analyze_calls += 1
        return EngineOutput.model_validate(
            {"model": _slots(perimeter), "questions": [], "summary": {"objective": "grow"}},
            context={"perimeter": perimeter})


def _gtm_session():
    """A go-to-market session discovered through the stub, and its service."""
    svc = SessionService()
    disco = DiscoveryService(provider=_PerimeterAware(), sessions=svc)
    return svc, disco, disco.start("grow the funnel", finalize=False, perimeter=GO_TO_MARKET)


def _forge_perimeter(svc: SessionService, slug: str, perimeter):
    """Edit the persisted `perimeter` by hand; returns the session directory."""
    d = svc.repo.store().canonical_dir(slug)
    data = json.loads((d / "session.json").read_text(encoding="utf-8"))
    if perimeter is None:
        assert "perimeter" not in data or data["perimeter"] is None  # the shape this assumes
        data.pop("perimeter", None)
    else:
        data["perimeter"] = perimeter
    (d / "session.json").write_text(json.dumps(data), encoding="utf-8")
    return d


# ── the mechanism (#608) ───────────────────────────────────────────────────────


def test_two_perimeters_are_installed():
    """`known_perimeter_ids()` names both, each resolving to a real, readable schema; `doctor` reports them."""
    assert known_perimeter_ids() == (GO_TO_MARKET, SOFTWARE)
    for pid in known_perimeter_ids():
        p = get_perimeter(pid)
        assert p.schema_path.is_file() and p.elicitation_path.is_file() and p.engine_guidance_path.is_file()
    r = doctor_report()
    assert r["perimeters"]["ok"] is True and set(r["perimeters"]["installed"]) == {GO_TO_MARKET, SOFTWARE}


def test_a_go_to_market_discovery_completes_through_the_real_provider_completion_path():
    """[P1, review] `_require_complete_model` used to call `completeness_gap(out)` with no perimeter."""
    result = run(FakeClient(json.dumps(_gtm_payload())), [{"role": "user", "content": "grow the funnel"}], perimeter=GO_TO_MARKET)
    assert set(result.model) == schema_slot_ids(GO_TO_MARKET)[0]


def test_a_same_text_request_under_a_different_perimeter_does_not_reuse_the_session():
    """[P1, review] `_same_identity`/`_identity_hash` used to compare only the request and the card selection."""
    svc = SessionService()
    software_meta = svc.create_session("grow the funnel", perimeter=SOFTWARE)
    gtm_meta, created = svc.create_session_report("grow the funnel", perimeter=GO_TO_MARKET)
    assert created is True and gtm_meta.slug != software_meta.slug
    assert resolve_perimeter(gtm_meta.perimeter) == GO_TO_MARKET
    assert resolve_perimeter(svc.meta(software_meta.slug).perimeter) == SOFTWARE


def test_an_explicit_slug_reused_under_a_different_perimeter_is_refused_before_any_reasoning():
    """The `strict_slug=True` arm (the API's `POST /sessions`) refuses outright."""
    svc = SessionService()
    svc.create_session("grow the funnel", slug="gtm", perimeter=SOFTWARE)
    with pytest.raises(SessionExistsError):
        svc.create_session_report("grow the funnel", slug="gtm", perimeter=GO_TO_MARKET, strict_slug=True)


def test_a_go_to_market_model_validates_and_reaches_readiness_against_its_own_vocabulary():
    """#608 acceptance: a model built under go-to-market validates, and software-only ids do not leak in."""
    allowed, _ = schema_slot_ids(GO_TO_MARKET)
    assert set(_gtm_out().model) == allowed and "business_rules" not in allowed


def _software_with_capacity():
    return {"model": {**_slots(SOFTWARE), "capacity": {"completeness": 10, "confidence": "empty", "impact": "low"}},
            "questions": [], "summary": {"objective": "x"}}


@pytest.mark.parametrize("payload,context,match", [
    ({"model": {**_slots(), "business_rules": {"completeness": 10, "confidence": "empty", "impact": "low"}},
      "questions": [], "summary": {"objective": "x"}}, {"perimeter": GO_TO_MARKET}, "business_rules"),
    (_software_with_capacity(), None, "capacity"),
    ({"model": _slots(), "questions": [{"q": "?", "slot": "business_rules", "why": "?"}],
      "summary": {"objective": "x"}}, {"perimeter": GO_TO_MARKET}, "business_rules"),
    ({"model": _slots(), "questions": [], "summary": {"objective": "x"},
      "decisions": [{"decision": "Use channel X", "derived_from": ["business_rules"]}]}, {"perimeter": GO_TO_MARKET}, "business_rules"),
], ids=["software slot in a gtm model", "gtm slot in a software model", "question target", "dag edge"])
def test_a_slot_id_from_the_other_perimeter_is_refused_wherever_it_appears(payload, context, match):
    """#608: the model, each `Question.slot` and every DAG edge are checked against one vocabulary."""
    with pytest.raises(Exception, match=match):
        ModelProposal.model_validate(payload, context=context)


def test_impact_is_scoped_to_the_sessions_own_perimeter():
    """`requivo impact` (`SessionService.impact`) reasons over the session's own vocabulary, artifact included (#609)."""
    svc, _, slug = _gtm_session()
    report = svc.impact(slug, ["capacity"])
    assert report.changed == ["Capacity"] and "gtm_plan" in report.artifacts
    with pytest.raises(UnknownSlotError):
        svc.impact(slug, ["business_rules"])


def test_perimeter_is_frozen_at_creation_and_visible_in_status_and_session_show():
    """#608 acceptance: recorded, frozen, and visible through `status --json` and `session show --json`."""
    svc = SessionService()
    meta = svc.create_session("grow the funnel", slug="gtm", perimeter=GO_TO_MARKET)
    assert meta.perimeter == GO_TO_MARKET and svc.snapshot("gtm").perimeter == GO_TO_MARKET
    assert json.loads(meta.model_dump_json())["perimeter"] == GO_TO_MARKET


def test_an_unknown_perimeter_is_refused_by_name_by_the_loader():
    """#608's deliberate inversion of invariant 8."""
    svc = SessionService()
    svc.create_session("a request", slug="unk")
    _forge_perimeter(svc, "unk", "space-exploration")
    with pytest.raises(UnknownPerimeterError, match="space-exploration"):
        svc.meta("unk")


def test_an_unknown_perimeter_is_refused_by_name_by_session_verify_and_doctor():
    """The same session through `inspect_session_dir`, which `session verify` and `doctor` both call."""
    svc = SessionService()
    svc.create_session("a request", slug="unk2")
    d = _forge_perimeter(svc, "unk2", "space-exploration")
    findings = inspect_session_dir(d, expected_slug="unk2")
    assert "unknown_perimeter" in {f.code for f in findings}
    assert all(f.severity == "problem" for f in findings if f.code == "unknown_perimeter")


def test_a_pre_perimeter_session_still_opens_as_software():
    """The forward sibling `test_a_session_written_by_an_older_requivo_still_loads` asks for (#608)."""
    svc = SessionService()
    svc.create_session("an old-shaped session", slug="old")
    _forge_perimeter(svc, "old", None)
    meta = svc.meta("old")
    assert meta.perimeter is None
    assert resolve_perimeter(meta.perimeter) == SOFTWARE == DEFAULT_PERIMETER
    assert svc.snapshot("old").perimeter == SOFTWARE


def test_go_to_market_ships_exactly_its_one_artifact():
    """#607's cost rule: each new perimeter ships with exactly one artifact, registered by #609."""
    assert get_perimeter(GO_TO_MARKET).artifact_types == frozenset({"gtm_plan"})
    slots = artifact_slots(GO_TO_MARKET)
    allowed, _ = schema_slot_ids(GO_TO_MARKET)
    assert set(slots) == {"gtm_plan"} and slots["gtm_plan"] == set(allowed)
    assert "business_rules" not in slots["gtm_plan"]


def test_the_real_artifact_registries_agree_on_their_key_sets_per_perimeter():
    """`test_the_real_artifact_registries_agree_on_their_key_sets`, filtered to each perimeter's `artifact_types`."""
    from test_dependencies import _artifact_vocabulary_mismatches

    for pid in known_perimeter_ids():
        owned = get_perimeter(pid).artifact_types
        problems = _artifact_vocabulary_mismatches(
            slots_raw={k: v for k, v in _ARTIFACT_SLOTS_RAW.items() if k in owned},
            generators={k: v for k, v in _GENERATORS.items() if k in owned},
            op_prompts={k: v for k, v in _OP_PROMPTS.items() if k in owned or k == "analyze"},
            writers={k: v for k, v in _WRITERS.items() if k in owned},
            generatable=tuple(t for t in GENERATABLE if t in owned),
            artifact_filenames={k: v for k, v in ARTIFACT_FILENAMES.items() if k in owned},
            artifact_labels={k: v for k, v in ARTIFACT_LABELS.items() if k in owned})
        assert not problems, f"perimeter {pid!r}: {problems}"


def test_generate_refuses_an_artifact_type_the_sessions_perimeter_does_not_own():
    """A go-to-market session cannot be handed to a software-only generator (#609)."""
    _, disco, slug = _gtm_session()
    with pytest.raises(ArtifactTypeNotOwnedError, match="go-to-market") as exc_info:
        disco.generate(slug, "prd")
    assert exc_info.value.code == "artifact_type_not_owned"
    assert exc_info.value.details == {"artifact_type": "prd", "perimeter": GO_TO_MARKET, "owned": ["gtm_plan"]}


def test_the_go_to_market_artifact_generates_saves_and_goes_stale_end_to_end():
    """#609 acceptance, through the real completion path (`FakeClient` -> `AnthropicProvider` -> `_complete()`)."""
    svc = SessionService()
    meta = svc.create_session("grow the funnel", slug="gtm-e2e", perimeter=GO_TO_MARKET)
    svc.update_model(meta.slug, _gtm_out().model_dump_json(), expected_revision=0)
    reply = json.dumps({
        "plan": ["Ship a weekly outbound sequence to the existing waitlist."],
        "exclusions": [{"option": "Paid search", "reason": "No budget for it this quarter.", "rests_on": ["budget", "capacity"]}],
        "thresholds": [{"condition": "CAC exceeds the stated ceiling", "measure": "CAC", "action": "stop the paid channel",
                        "rests_on": ["unit_economics"]}],
        "envelope": [{"kind": "Capacity", "value": "4h/week", "origin": "slot", "source_slot": "capacity"}]})
    disco = DiscoveryService(provider=AnthropicProvider(client=FakeClient(reply)), sessions=svc)

    result = disco.generate(meta.slug, "gtm_plan")
    assert result.status.filename == "go-to-market-plan.md" and result.status.stale is False
    applied = svc.load_model(meta.slug)
    assert [e.option for e in applied.exclusions] == ["Paid search"]
    assert [t.condition for t in applied.thresholds] == ["CAC exceeds the stated ceiling"]
    rec = svc.meta(meta.slug).revisions[-1]
    assert rec.prompt_version == prompt_version("gtm_plan", perimeter=GO_TO_MARKET)
    assert rec.prompt_version != prompt_version("gtm_plan")  # software default -- must differ

    # The full staleness path: change the slot the exclusion rests on.
    changed = applied.model["capacity"].model_copy(update={"value": "only 2h/week now"})
    updated = applied.model_copy(update={"model": {**applied.model, "capacity": changed}})
    svc.update_model(meta.slug, updated.model_dump_json(), expected_revision=svc.meta(meta.slug).current_revision)
    assert svc.meta(meta.slug).artifact_status["gtm_plan"].stale is True


# ── the router's contract (#601) ───────────────────────────────────────────────


@pytest.mark.parametrize("kwargs,match", [
    (dict(decision="fits", perimeter="software"), None),
    (dict(decision="ambiguous", candidates=["software", "go-to-market"]), None),
    (dict(decision="none"), None),
    (dict(decision="fits"), "names no perimeter"),
    (dict(decision="none", perimeter="software"), "only 'fits' routes"),
    (dict(decision="ambiguous", candidates=["software"]), "fewer than two distinct candidates"),
    (dict(decision="ambiguous", candidates=["software", "software"]), "fewer than two distinct candidates"),
    (dict(decision="fits", perimeter="software", candidates=["software"]), "only 'ambiguous' does"),
    (dict(decision="none", candidates=["software", "go-to-market"]), "only 'ambiguous' does"),
])
def test_a_perimeter_judgment_whose_payload_contradicts_its_decision_is_refused(kwargs, match):
    """The same discipline `ContextJudgment`'s validator holds, one question over (#601)."""
    if match is None:
        PerimeterJudgment(reason="r", **kwargs)
    else:
        with pytest.raises(Exception, match=match):
            PerimeterJudgment(reason="r", **kwargs)


def test_a_judgment_naming_a_perimeter_the_install_does_not_have_is_refused():
    """The sibling of `test_a_judgment_naming_a_card_the_install_does_not_have_is_refused` (#593)."""
    invented = json.dumps({"decision": "fits", "reason": "r", "perimeter": "inventory-management"})
    client = FakeClient(invented, invented, invented)
    with pytest.raises(ProviderOutputError):
        judge_perimeter(client, "a request", perimeter_summaries())
    assert len(client.calls) == 3, "the correction did not ride the retry loop"
    good = json.dumps({"decision": "fits", "reason": "r", "perimeter": "go-to-market"})
    assert judge_perimeter(FakeClient(good), "a request", perimeter_summaries()).perimeter == "go-to-market"


# ── the router through the service (#601) ─────────────────────────────────────


class _Router(StubProvider):
    """A `ReasoningProvider` that also answers routing and grounding questions."""

    name = "routing-stub"

    def __init__(self, judgment=None):
        super().__init__()
        self.judgment = judgment or PerimeterJudgment(decision="none", reason="an ordinary request")
        self.asked: list[list] = []

    def judge_perimeter(self, request, *, perimeters):
        self.asked.append(perimeters)
        return self.judgment

    def judge_context(self, request, *, cards):
        return ContextJudgment(decision="none", reason="ordinary software")


_FITS_GTM = PerimeterJudgment(decision="fits", reason="a launch plan", perimeter=GO_TO_MARKET)
_NARROWS = ContextJudgment(decision="installed", reason="finance", cards=["financial-reporting"])


def _claim(router, request="a request", **kwargs):
    return DiscoveryService(router).claim_and_ground(request, cards=None, slug=None, **kwargs)


def test_an_explicit_perimeter_is_not_second_guessed():
    """A `--perimeter` is a human decision; the router is never consulted and never overrides it."""
    router = _Router(_FITS_GTM)
    routing = DiscoveryService(router).route_perimeter("a request", perimeter="software")
    assert router.asked == [] and routing.judgment is None and "--perimeter" in routing.why_not
    meta, _grounding, _cards, routing = _claim(router, perimeter=SOFTWARE)
    assert meta.perimeter == SOFTWARE and router.asked == [] and routing.judgment is None


def test_a_single_installed_perimeter_is_not_judged(monkeypatch):
    from requivo.services import discovery as disco_mod

    monkeypatch.setattr(disco_mod, "known_perimeter_ids", lambda: (SOFTWARE,))
    router = _Router()
    routing = DiscoveryService(router).route_perimeter("a request", perimeter=None)
    assert router.asked == [], "a single-perimeter install was still billed for a routing judgment"
    assert routing.judgment is None and "only one perimeter" in routing.why_not


def test_a_provider_that_cannot_route_reports_not_asked():
    """`PerimeterJudge` is a protocol a provider may simply not implement (#492)."""
    routing = DiscoveryService(FakeProvider()).route_perimeter("a request", perimeter=None)
    assert routing.judgment is None and routing.why_not


def test_the_routing_judgment_reaches_the_provider_with_one_line_per_installed_perimeter():
    router = _Router()
    DiscoveryService(router).route_perimeter("a request", perimeter=None)
    assert len(router.asked) == 1
    assert sorted(p.id for p in router.asked[0]) == sorted(known_perimeter_ids())


def test_a_fitting_perimeter_verdict_reroutes_and_reclaims():
    """The mirror of #593's own `test_a_narrowing_verdict_reclaims_under_the_narrowed_identity`."""
    meta, _grounding, _cards, routing = _claim(_Router(_FITS_GTM), "help us launch this")
    assert meta.perimeter == GO_TO_MARKET and routing.judgment.decision.value == "fits"
    assert len(SessionService().list_sessions()) == 1, "the default-perimeter claim was left behind"


def test_an_ambiguous_verdict_refuses_before_any_model_is_reasoned():
    """The decline state is a real answer; its LLM-authored reason is neutralised in the message, raw in details (#601 P2)."""
    forged = "could be either\x1b[2Jrequest"
    router = _Router(PerimeterJudgment(decision="ambiguous", reason=forged, candidates=[SOFTWARE, GO_TO_MARKET]))
    with pytest.raises(AmbiguousPerimeterError, match="could be either") as exc_info:
        _claim(router, "an ambiguous request")
    assert SessionService().list_sessions() == [], "an ambiguous verdict left a session behind"
    assert "\x1b" not in str(exc_info.value) and exc_info.value.details["reason"] == forged


def test_a_none_verdict_continues_under_the_default_perimeter_named():
    """"None fits" is a real answer: the session continues under the default perimeter, recorded by name."""
    meta, _grounding, _cards, routing = _claim(_Router(PerimeterJudgment(decision="none", reason="neither shape")))
    assert meta.perimeter == SOFTWARE and routing.judgment.decision.value == "none"


def test_a_session_this_call_did_not_create_is_never_deleted_by_a_routing_verdict():
    """The mirror of #593's own `test_a_session_this_call_did_not_create_is_never_deleted_by_a_verdict`."""
    svc = SessionService()
    first = svc.create_session("a request", perimeter=SOFTWARE)
    meta, _grounding, _cards, _routing = _claim(_Router(_FITS_GTM))
    assert meta.slug == first.slug and meta.perimeter == SOFTWARE and svc.exists(first.slug)


def test_reclaiming_onto_a_pre_existing_session_never_authorises_deleting_it():
    """#601 P1 (Codex review)."""
    svc = SessionService()
    victim = svc.create_session("a request", perimeter=GO_TO_MARKET)

    class _RoutesAndGrounds(_Router):
        def judge_context(self, request, *, cards):
            return _NARROWS

    meta, _grounding, cards, _routing = _claim(_RoutesAndGrounds(_FITS_GTM))
    assert meta.slug == victim.slug and svc.exists(victim.slug)
    assert svc.meta(victim.slug).context_cards is None, "cards were narrowed onto a session this call did not create"
    assert cards is None, "the caller was told cards narrowed when nothing was authorised to narrow"


def test_an_idempotent_perimeter_reclaim_onto_the_correct_identity_is_announced_as_landed():
    """#601 P2, round three (Codex review): a route that DID land is reported as such."""

    class _RoutesWithARace(_Router):
        def judge_perimeter(self, request, *, perimeters):
            SessionService().create_session(request, perimeter=GO_TO_MARKET)  # a concurrent claimant
            return super().judge_perimeter(request, perimeters=perimeters)

    meta, _grounding, _cards, routing = _claim(_RoutesWithARace(_FITS_GTM))
    assert meta.perimeter == GO_TO_MARKET
    assert routing.judgment is not None and routing.judgment.decision.value == "fits"


def test_an_idempotent_reclaim_onto_the_correct_identity_reports_that_identity_not_the_old_one():
    """#601 P2, round three: a reclaim that lands idempotently on the narrowed identity is still a landed reclaim."""
    svc = SessionService()
    already_narrowed = svc.create_session("a request", context_cards=["financial-reporting"], perimeter=SOFTWARE)

    class _Grounds(_Router):
        def judge_context(self, request, *, cards):
            return _NARROWS

    meta, _grounding, cards, _routing = _claim(_Grounds())
    assert meta.slug == already_narrowed.slug
    assert cards == ["financial-reporting"] and meta.context_cards == ["financial-reporting"]


def test_a_route_that_cannot_land_is_not_announced_as_though_it_did():
    """#601 P2 (Codex review)."""

    class _RoutesMidJudgment(_Router):
        def judge_perimeter(self, request, *, perimeters):
            SessionService().update_model(SessionService().list_sessions()[0].slug, full_model())
            return super().judge_perimeter(request, perimeters=perimeters)

    meta, _grounding, _cards, routing = _claim(_RoutesMidJudgment(_FITS_GTM))
    assert meta.perimeter == SOFTWARE, "reasoning proceeded under a perimeter the screen does not name"
    assert SessionService().meta(meta.slug).current_revision == 1, "the concurrent write was lost"
    assert routing.judgment is None and "could not" in routing.why_not


def test_claim_and_ground_resolves_an_existing_non_default_perimeter_session_before_routing():
    """The mechanism behind the CLI-level repro below, isolated (#601, Codex review round four)."""
    meta, _grounding, _cards, _routing = _claim(_Router(_FITS_GTM))
    assert meta.perimeter == GO_TO_MARKET
    SessionService().update_model(meta.slug, json.dumps(_gtm_payload()))   # a completed discovery: revision 1
    second_router = _Router()   # its judge_perimeter must never be reached
    with pytest.raises(RevisionConflictError):
        _claim(second_router)
    assert second_router.asked == [], "the routing judgment was billed on a repeat of an already-discovered session"


def test_a_failed_routing_call_does_not_lock_the_retry_into_the_default_perimeter():
    """#601 P2, round five: a failed routing call leaves no placeholder, and the retry asks the router again."""

    class _FailsOnce(_Router):
        attempts = 0

        def judge_perimeter(self, request, *, perimeters):
            self.attempts += 1
            if self.attempts == 1:
                raise EngineError("transport failure")
            return super().judge_perimeter(request, perimeters=perimeters)

    router = _FailsOnce(_FITS_GTM)
    disco = DiscoveryService(router)
    with pytest.raises(EngineError):
        disco.claim_and_ground("a request", cards=None, slug=None)
    assert SessionService().list_sessions() == [], "the failed routing call's own placeholder survived"
    meta, _grounding, _cards, routing = disco.claim_and_ground("a request", cards=None, slug=None)
    assert router.attempts == 2 and meta.perimeter == GO_TO_MARKET
    assert routing.judgment is not None and routing.judgment.decision.value == "fits"


# ── the CLI, end to end, with a fake client (#601) ─────────────────────────────

_GTM_FITS_REPLY = json.dumps({"decision": "fits", "reason": "this reads as a launch plan, not a system to build",
                              "perimeter": GO_TO_MARKET})
_AMBIGUOUS_REPLY = json.dumps({"decision": "ambiguous", "reason": "this could be either a system or a launch plan",
                               "candidates": [SOFTWARE, GO_TO_MARKET]})
_GTM_TURN = json.dumps(_gtm_payload())


def _at_a_terminal(monkeypatch, answer=None) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    if answer is not None:
        monkeypatch.setattr(builtins, "input", answer)


def test_a_go_to_market_shaped_request_routes_there_on_a_first_discovery():
    """Acceptance: a request of an installed perimeter's shape routes to it before any model is reasoned."""
    fake = FakeClient(_GTM_FITS_REPLY, _JUDGMENT_REPLY, _GTM_TURN)
    printed = run_cli(["discover", "help us launch this new product", "--once"], client=fake)
    assert len(fake.calls) == 3, "the route, the grounding judgment, and the turn"
    assert SessionService().list_sessions()[0].perimeter == GO_TO_MARKET
    assert printed.index("this reads as a launch plan") < printed.index("Ready?"), "the verdict is shown before the turn"


def test_an_explicit_perimeter_flag_costs_no_routing_call():
    fake = FakeClient(_JUDGMENT_REPLY, _ENGINE_REPLY)
    run_cli(["discover", "a request", "--perimeter", SOFTWARE, "--once"], client=fake)
    assert len(fake.calls) == 2, "the grounding judgment and the turn -- no routing call"
    assert SessionService().list_sessions()[0].perimeter == SOFTWARE


def test_a_none_routing_verdict_is_shown_and_the_session_continues_under_software():
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
    printed = run_cli(["discover", "a request", "--once"], client=fake)
    assert len(fake.calls) == 3 and "Perimeter" in printed
    assert SessionService().list_sessions()[0].perimeter == SOFTWARE


@pytest.mark.parametrize("argv_tail", [["--once"], []], ids=["once", "no tty"])
def test_an_ambiguous_router_verdict_refuses_before_any_model_is_reasoned_cli(capsys, argv_tail):
    """One call only, no session left behind, the candidates on stderr rather than a traceback; no stdin read."""
    fake = FakeClient(_AMBIGUOUS_REPLY)
    with pytest.raises(SystemExit) as exit_:
        app(["discover", "an ambiguous request", *argv_tail], client=fake)
    assert exit_.value.code == 1 and len(fake.calls) == 1
    assert SessionService().list_sessions() == []
    err = capsys.readouterr().err
    assert "could be either a system or a launch plan" in err and "Traceback" not in err


def test_an_ambiguous_verdict_asks_interactively_and_continues_under_the_chosen_answer(monkeypatch):
    """JB's call over the issue's own wording (#601)."""
    _at_a_terminal(monkeypatch, lambda _prompt="": "2")   # 1=software, 2=go-to-market
    fake = FakeClient(_AMBIGUOUS_REPLY, _JUDGMENT_REPLY, _GTM_TURN)
    printed = run_cli(["discover", "help with our internal tool rollout"], client=fake)
    assert len(fake.calls) == 3, "asking interactively must not cost a second routing call"
    assert "could be either a system or a launch plan" in printed and "Continuing under go-to-market" in printed
    assert SessionService().list_sessions()[0].perimeter == GO_TO_MARKET


@pytest.mark.parametrize("argv_tail", [["--once"], []], ids=["once", "interactive"])
def test_a_repeat_of_a_routed_sessions_discovery_costs_no_provider_call(monkeypatch, capsys, argv_tail):
    """The CLI walk of the claim-and-ground test above, both entry points (#601, round four)."""
    _at_a_terminal(monkeypatch)
    run_cli(["discover", "help us launch this new product", "--once"], client=FakeClient(_GTM_FITS_REPLY, _JUDGMENT_REPLY, _GTM_TURN))
    fake = FakeClient()
    with pytest.raises(SystemExit) as exit_:
        app(["discover", "help us launch this new product", *argv_tail], client=fake)
    assert exit_.value.code == 1 and "already carries a model" in capsys.readouterr().err
    assert fake.calls == [], "provider calls were billed before the refusal on a session already routed"


@pytest.mark.parametrize("interruption,code", [(KeyboardInterrupt, 130), (EOFError, 1)], ids=["interrupt", "eof"])
def test_an_interrupt_at_the_perimeter_prompt_exits_130_not_1(monkeypatch, capsys, interruption, code):
    """#601 P2, round four: cancelling at the prompt is not a failed judgment; EOF is the caught arm's control."""

    def _raise(_prompt=""):
        raise interruption

    _at_a_terminal(monkeypatch, _raise)
    fake = FakeClient(_AMBIGUOUS_REPLY)
    with pytest.raises(SystemExit) as exit_:
        app(["discover", "an ambiguous request"], client=fake)
    assert exit_.value.code == code and len(fake.calls) == 1
    if code == 1:
        assert "could be either" in capsys.readouterr().err
