"""The perimeter router (#601): judge which installed perimeter a first discovery's shape belongs
to, before any model is reasoned, riding #593's own claim-judge-reclaim seam rather than a second
one beside it. `test_discovery_provider_seam.py`'s grounding-judgment section is this file's direct
sibling -- same shape of test, one question over.
"""
from __future__ import annotations

import json

import pytest
from _fakes import _ENGINE_REPLY, _JUDGMENT_REPLY, _ROUTING_REPLY, FakeClient, _run_app, out, slot

from requivo.cli import app
from requivo.core.contracts import ContextJudgment, PerimeterJudgment
from requivo.core.errors import AmbiguousPerimeterError
from requivo.core.perimeters import GO_TO_MARKET, SOFTWARE
from requivo.services.sessions import SessionService

# ── the contract's own validator ───────────────────────────────────────────────


def test_a_perimeter_judgment_whose_payload_contradicts_its_decision_is_refused():
    """The same discipline `ContextJudgment`'s validator holds, one question over: a decision and a
    payload that disagree is refused rather than reaching a caller. Guarded here rather than only
    pinned in `discovery.py`'s docstring, since the validator is the thing that must not regress."""
    PerimeterJudgment(decision="fits", reason="r", perimeter="software")               # control
    PerimeterJudgment(decision="ambiguous", reason="r", candidates=["software", "go-to-market"])
    PerimeterJudgment(decision="none", reason="r")

    with pytest.raises(Exception, match="names no perimeter"):
        PerimeterJudgment(decision="fits", reason="r")
    with pytest.raises(Exception, match="only 'fits' routes"):
        PerimeterJudgment(decision="none", reason="r", perimeter="software")
    with pytest.raises(Exception, match="fewer than two candidates"):
        PerimeterJudgment(decision="ambiguous", reason="r", candidates=["software"])
    with pytest.raises(Exception, match="only 'ambiguous' does"):
        PerimeterJudgment(decision="fits", reason="r", perimeter="software", candidates=["software"])


# ── the service, through a stub provider ───────────────────────────────────────


class _Router:
    """A `ReasoningProvider` that also answers routing questions (#601). Records what it was asked;
    its context grounding always answers `none` so these tests isolate routing from #593's own
    judgment -- the mirror of `test_discovery_provider_seam.py`'s `_Judge`, one question over."""

    name = "routing-stub"

    def __init__(self, judgment=None):
        self.judgment = judgment or PerimeterJudgment(decision="none", reason="an ordinary request")
        self.asked: list[list] = []

    def judge_perimeter(self, request, *, perimeters):
        self.asked.append(perimeters)
        return self.judgment

    def judge_context(self, request, *, cards):
        return ContextJudgment(decision="none", reason="ordinary software")

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
                perimeter=None):
        return out({"problem": slot(80, "explicit", "high")})

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        raise AssertionError("no generation in these tests")

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": self.name, "model_name": "stub-model", "prompt_version": "sha256:0"}


def _disco(provider):
    from requivo.services.discovery import DiscoveryService
    return DiscoveryService(provider)


def test_an_explicit_perimeter_is_not_second_guessed(workspace):
    """A `--perimeter` is a human decision. Paying to re-examine it would either agree at cost or
    disagree with nothing the service is allowed to do about it -- the identical rule
    `judge_grounding` already holds for `--context`."""
    router = _Router()
    routing = _disco(router).route_perimeter("a request", perimeter="software")

    assert router.asked == [], "the routing judgment was billed over a choice the user had already made"
    assert routing.judgment is None
    assert "--perimeter" in routing.why_not


def test_a_single_installed_perimeter_is_not_judged(workspace, monkeypatch):
    """One installed perimeter is not a routing decision -- the router's own mirror of
    `judge_grounding`'s "an install with no cards" guard."""
    from requivo.services import discovery as disco_mod

    monkeypatch.setattr(disco_mod, "known_perimeter_ids", lambda: (SOFTWARE,))
    router = _Router()
    routing = _disco(router).route_perimeter("a request", perimeter=None)

    assert router.asked == [], "a single-perimeter install was still billed for a routing judgment"
    assert routing.judgment is None and "only one perimeter" in routing.why_not


def test_a_provider_that_cannot_route_reports_not_asked(workspace):
    """`PerimeterJudge` is a protocol a provider may simply not implement. *Nobody looked* and *no
    routing needed* must never be the same answer -- #492's rule, ridden one layer up."""
    from conftest import FakeProvider as _FakeProvider

    routing = _disco(_FakeProvider()).route_perimeter("a request", perimeter=None)

    assert routing.judgment is None, "a provider that cannot route produced a verdict anyway"
    assert routing.why_not, "not asked, and it did not say why"


def test_the_routing_judgment_reaches_the_provider_with_one_line_per_installed_perimeter(workspace):
    """The summaries are read in `core` and passed down, so the provider cannot answer about a
    different install than the one this build actually has."""
    from requivo.core.perimeters import known_perimeter_ids

    router = _Router()
    _disco(router).route_perimeter("a request", perimeter=None)

    assert len(router.asked) == 1
    assert sorted(p.id for p in router.asked[0]) == sorted(known_perimeter_ids())


def test_a_fitting_perimeter_verdict_reroutes_and_reclaims(workspace):
    """The mirror of #593's own `test_a_narrowing_verdict_reclaims_under_the_narrowed_identity`:
    perimeter is half of identity too (invariant 11), so acting on `fits` cannot be an edit -- the
    empty session claimed under the default is deleted and re-claimed under the routed perimeter."""
    router = _Router(PerimeterJudgment(decision="fits", reason="a launch plan", perimeter=GO_TO_MARKET))
    meta, _grounding, _cards, routing = _disco(router).claim_and_ground(
        "help us launch this", cards=None, slug=None)

    assert meta.perimeter == GO_TO_MARKET
    assert routing.judgment.decision.value == "fits"
    assert len(SessionService().list_sessions()) == 1, "the default-perimeter claim was left behind"


def test_an_ambiguous_verdict_refuses_before_any_model_is_reasoned(workspace):
    """The decline state that is a real answer, not a fallback (#601's own framing): more than one
    perimeter plausibly fits, so the service says so and asks rather than guessing -- and it refuses
    before `analyze()` is ever reached, leaving no session behind for the caller to clean up."""
    router = _Router(PerimeterJudgment(
        decision="ambiguous", reason="could be either", candidates=[SOFTWARE, GO_TO_MARKET]))

    with pytest.raises(AmbiguousPerimeterError, match="could be either"):
        _disco(router).claim_and_ground("an ambiguous request", cards=None, slug=None)

    assert SessionService().list_sessions() == [], "an ambiguous verdict left a session behind"


def test_an_explicit_perimeter_is_never_overridden_by_the_router(workspace):
    """A caller that names `--perimeter` explicitly is never re-routed, even when the router (were
    it asked) would have picked something else -- it is never asked at all."""
    router = _Router(PerimeterJudgment(decision="fits", reason="x", perimeter=GO_TO_MARKET))
    meta, _grounding, _cards, routing = _disco(router).claim_and_ground(
        "a request", cards=None, slug=None, perimeter=SOFTWARE)

    assert meta.perimeter == SOFTWARE, "an explicit --perimeter was overridden by the router"
    assert router.asked == [], "the router was consulted despite an explicit --perimeter"
    assert routing.judgment is None


def test_a_none_verdict_continues_under_the_default_perimeter_named(workspace):
    """"None fits" is a real answer, not a failure: the session continues under the default
    perimeter, and that name is what a reader finds recorded on it -- never silently assumed."""
    router = _Router(PerimeterJudgment(decision="none", reason="neither shape"))
    meta, _grounding, _cards, routing = _disco(router).claim_and_ground(
        "a request", cards=None, slug=None)

    assert meta.perimeter == SOFTWARE
    assert routing.judgment.decision.value == "none"


def test_a_session_this_call_did_not_create_is_never_deleted_by_a_routing_verdict(workspace):
    """The mirror of #593's own `test_a_session_this_call_did_not_create_is_never_deleted_by_a_verdict`:
    an idempotent re-entry onto an existing claim must never be the thing a routing verdict deletes."""
    svc = SessionService()
    first = svc.create_session("a request", perimeter=SOFTWARE)

    router = _Router(PerimeterJudgment(decision="fits", reason="x", perimeter=GO_TO_MARKET))
    meta, _grounding, _cards, _routing = _disco(router).claim_and_ground(
        "a request", cards=None, slug=None)

    assert meta.slug == first.slug, "an idempotent re-entry landed somewhere else"
    assert meta.perimeter == SOFTWARE, "a session this call did not create was rerouted anyway"
    assert svc.exists(first.slug), "a session this call did not create was deleted"


# ── the CLI, end to end, with a fake client ────────────────────────────────────

_GTM_FITS_REPLY = json.dumps(
    {"decision": "fits", "reason": "this reads as a launch plan, not a system to build",
     "perimeter": GO_TO_MARKET})
_AMBIGUOUS_REPLY = json.dumps(
    {"decision": "ambiguous", "reason": "this could be either a system or a launch plan",
     "candidates": [SOFTWARE, GO_TO_MARKET]})


def _gtm_reply() -> str:
    from requivo.core.contracts import schema_slot_ids
    _, required = schema_slot_ids(GO_TO_MARKET)
    return json.dumps({
        "model": {sid: {"completeness": 90, "confidence": "explicit", "impact": "high",
                        "value": "x", "evidence": "y"} for sid in required},
        "questions": [], "summary": {"objective": "grow the funnel"},
    })


def test_a_go_to_market_shaped_request_routes_there_on_a_first_discovery(workspace):
    """Acceptance criterion: a request of an installed perimeter's shape routes to it on a first
    discovery, before any model is reasoned -- and the verdict is printed before the turn's own
    output, verbatim (#593's rule, ridden for the router)."""
    fake = FakeClient(_GTM_FITS_REPLY, _JUDGMENT_REPLY, _gtm_reply())
    printed = _run_app(["discover", "help us launch this new product", "--once"], client=fake)

    assert len(fake.calls) == 3, "the route, the grounding judgment, and the turn"
    meta = SessionService().list_sessions()[0]
    assert meta.perimeter == GO_TO_MARKET
    route_at = printed.index("this reads as a launch plan")
    turn_at = printed.index("Ready?")
    assert route_at < turn_at, "the verdict must be shown before the turn it grounds"


def test_an_explicit_perimeter_flag_costs_no_routing_call(workspace):
    """The router is for when the user did not say (acceptance criterion): with `--perimeter`
    named, the router is never consulted, so the call count is two, not three."""
    fake = FakeClient(_JUDGMENT_REPLY, _ENGINE_REPLY)
    _run_app(["discover", "a request", "--perimeter", SOFTWARE, "--once"], client=fake)

    assert len(fake.calls) == 2, "the grounding judgment and the turn -- no routing call"
    assert SessionService().list_sessions()[0].perimeter == SOFTWARE


def test_an_ambiguous_router_verdict_refuses_before_any_model_is_reasoned_cli(workspace, capsys):
    """The CLI walk of the same refusal: one call only, no session left behind, and the candidates
    named on stderr rather than a traceback."""
    fake = FakeClient(_AMBIGUOUS_REPLY)

    with pytest.raises(SystemExit) as exit_:
        app(["discover", "an ambiguous request", "--once"], client=fake)

    assert exit_.value.code == 1
    assert len(fake.calls) == 1, "only the routing call was paid for before the refusal"
    assert SessionService().list_sessions() == []
    err = capsys.readouterr().err
    assert "could be either a system or a launch plan" in err
    assert "Traceback" not in err


def test_a_none_routing_verdict_is_shown_and_the_session_continues_under_software(workspace):
    """"None fits" is a real answer that lets the session proceed (acceptance criterion), under a
    perimeter that is *named*, not merely assumed -- and the router still cost exactly one call."""
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
    printed = _run_app(["discover", "a request", "--once"], client=fake)

    assert len(fake.calls) == 3
    assert "Perimeter" in printed
    assert SessionService().list_sessions()[0].perimeter == SOFTWARE
