"""The perimeter router (#601): judge which installed perimeter a first discovery's shape belongs
to, before any model is reasoned, riding #593's own claim-judge-reclaim seam rather than a second
one beside it. `test_discovery_provider_seam.py`'s grounding-judgment section is this file's direct
sibling -- same shape of test, one question over.
"""
from __future__ import annotations

import builtins
import json
import sys

import pytest
from _fakes import _ENGINE_REPLY, _JUDGMENT_REPLY, _ROUTING_REPLY, FakeClient, _run_app, out, slot

from requivo.cli import app
from requivo.core.contracts import ContextJudgment, PerimeterJudgment
from requivo.core.errors import AmbiguousPerimeterError, RevisionConflictError
from requivo.core.perimeters import GO_TO_MARKET, SOFTWARE
from requivo.services.sessions import SessionService

# ── the contract's own validator ───────────────────────────────────────────────


def test_a_perimeter_judgment_whose_payload_contradicts_its_decision_is_refused():
    """The same discipline `ContextJudgment`'s validator holds, one question over: a decision and a
    payload that disagree is refused rather than reaching a caller. Guarded here rather than only
    pinned in `discovery.py`'s docstring, since the validator is the thing that must not regress.

    The duplicate-candidate case is #601 P2 (Codex review): `len(candidates) < 2` passed a reply
    naming the *same* perimeter twice, so a non-interactive discovery deleted its claim and refused
    over an ambiguity nobody stated, and an interactive one offered the identical choice twice. The
    `none`-with-candidates case is that finding's own instruction to check a validator's neighbours
    -- same code path as `fits`-with-candidates, asserted in its own right so a future refactor
    cannot silently cover one arm and not the other."""
    PerimeterJudgment(decision="fits", reason="r", perimeter="software")               # control
    PerimeterJudgment(decision="ambiguous", reason="r", candidates=["software", "go-to-market"])
    PerimeterJudgment(decision="none", reason="r")

    with pytest.raises(Exception, match="names no perimeter"):
        PerimeterJudgment(decision="fits", reason="r")
    with pytest.raises(Exception, match="only 'fits' routes"):
        PerimeterJudgment(decision="none", reason="r", perimeter="software")
    with pytest.raises(Exception, match="fewer than two distinct candidates"):
        PerimeterJudgment(decision="ambiguous", reason="r", candidates=["software"])
    with pytest.raises(Exception, match="fewer than two distinct candidates"):
        PerimeterJudgment(decision="ambiguous", reason="r", candidates=["software", "software"])
    with pytest.raises(Exception, match="only 'ambiguous' does"):
        PerimeterJudgment(decision="fits", reason="r", perimeter="software", candidates=["software"])
    with pytest.raises(Exception, match="only 'ambiguous' does"):
        PerimeterJudgment(decision="none", reason="r", candidates=["software", "go-to-market"])


def test_a_judgment_naming_a_perimeter_the_install_does_not_have_is_refused(workspace):
    """The sibling of `test_a_judgment_naming_a_card_the_install_does_not_have_is_refused` (#593),
    one call over: an invented perimeter id is not inert -- it would reach `get_perimeter` as a
    claim and refuse the very discovery the judgment was supposed to route. Rides `_complete`'s
    retry loop as a `ValueError`, so the model is told what it got wrong rather than the run
    failing (#601)."""
    from requivo.core.errors import ProviderOutputError
    from requivo.core.perimeters import perimeter_summaries
    from requivo.providers.anthropic.generators import judge_perimeter

    invented = json.dumps({"decision": "fits", "reason": "r", "perimeter": "inventory-management"})
    client = FakeClient(invented, invented, invented)

    with pytest.raises(ProviderOutputError):
        judge_perimeter(client, "a request", perimeter_summaries())
    assert len(client.calls) == 3, "the correction did not ride the retry loop"

    # Must fire: the same shape naming a perimeter that *is* installed comes straight back.
    good = json.dumps({"decision": "fits", "reason": "r", "perimeter": "go-to-market"})
    judged = judge_perimeter(FakeClient(good), "a request", perimeter_summaries())
    assert judged.perimeter == "go-to-market"


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


def test_an_ambiguous_verdicts_reason_is_neutralized_in_the_message_not_in_details(workspace):
    """#601 P2 (Codex review): `reason` is LLM-authored prose over an untrusted request, and the
    exception's message reaches the terminal verbatim -- no renderer sits between this raise and
    `sys.stderr` (`app()` writes `str(error)` directly). A forged reason carrying a control sequence
    must not reach the screen unescaped, the same rule invariant 14 holds for a stored card name
    forging a line of `doctor`. `details['reason']` keeps the raw text, for a `--json` consumer that
    renders it through its own escaping."""
    forged = "ordinary\x1b[2Jrequest"
    router = _Router(PerimeterJudgment(
        decision="ambiguous", reason=forged, candidates=[SOFTWARE, GO_TO_MARKET]))

    with pytest.raises(AmbiguousPerimeterError) as exc_info:
        _disco(router).claim_and_ground("an ambiguous request", cards=None, slug=None)

    assert "\x1b" not in str(exc_info.value), "a raw control character reached the human-readable message"
    assert exc_info.value.details["reason"] == forged, "the raw reason must survive in details"


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


def test_reclaiming_onto_a_pre_existing_session_never_authorises_deleting_it(workspace):
    """#601 P1 (Codex review): `_reclaim_under` used to return `True` unconditionally once its own
    delete succeeded, even when the recreate that followed landed *idempotently* on a session that
    already existed under the routed perimeter -- so a perimeter reclaim's false "I created this"
    fed straight into the grounding step's own precondition check, authorising it to delete and
    recreate a session neither call actually made, silently narrowing its cards. The victim here is
    created before `claim_and_ground` is even called."""
    from requivo.core.contracts import ContextJudgment

    svc = SessionService()
    victim = svc.create_session("a request", perimeter=GO_TO_MARKET)

    class _RoutesAndGrounds(_Router):
        def judge_context(self, request, *, cards):
            return ContextJudgment(decision="installed", reason="finance",
                                   cards=["financial-reporting"])

    router = _RoutesAndGrounds(PerimeterJudgment(decision="fits", reason="x", perimeter=GO_TO_MARKET))
    meta, _grounding, cards, _routing = _disco(router).claim_and_ground(
        "a request", cards=None, slug=None)

    assert meta.slug == victim.slug, "landed on a different session than the pre-existing one"
    assert svc.exists(victim.slug), "the pre-existing session was deleted"
    assert svc.meta(victim.slug).context_cards is None, (
        "cards were narrowed onto a session this call did not create")
    assert cards is None, "the caller was told cards narrowed when nothing was authorised to narrow"


def test_an_idempotent_perimeter_reclaim_onto_the_correct_identity_is_announced_as_landed(
        workspace):
    """The routing branch's own mirror of the test below (#601 P2, round three, Codex review): a
    perimeter reclaim that lands idempotently on a go-to-market session must report *that*
    identity -- not fall back to announcing the software claim retained, which is what reading
    `created` (False on an idempotent match) as `landed` used to do. Getting this wrong here is
    worse than the grounding-side mirror: `claim_perimeter` feeds straight into the `perimeter=`
    this call's own discovery turn reasons under, so a wrong answer here means analysing under one
    perimeter's schema against a session recorded under another.

    The victim must appear *during* the routing call, not before it: round four's own fix
    (`find_existing_session`, invariant 13) now resolves any pre-existing match before routing is
    even asked, so a victim created up front is caught there instead of exercising this reclaim at
    all -- correctly, and the still-reachable shape of this race is a concurrent writer landing the
    identity while the routing judgment is in flight."""

    class _RoutesWithARace(_Router):
        def judge_perimeter(self, request, *, perimeters):
            # A concurrent writer claims the go-to-market identity between this call's own
            # find_existing_session check (which found nothing) and its reclaim.
            SessionService().create_session(request, perimeter=GO_TO_MARKET)
            return super().judge_perimeter(request, perimeters=perimeters)

    router = _RoutesWithARace(
        PerimeterJudgment(decision="fits", reason="a launch plan", perimeter=GO_TO_MARKET))
    meta, _grounding, _cards, routing = _disco(router).claim_and_ground(
        "a request", cards=None, slug=None)

    assert meta.perimeter == GO_TO_MARKET, "an idempotent landing on the correct perimeter was not announced"
    assert routing.judgment is not None and routing.judgment.decision.value == "fits", (
        "a route that DID land was reported as though it had not")


def test_an_idempotent_reclaim_onto_the_correct_identity_reports_that_identity_not_the_old_one(
        workspace):
    """#601 P2, round three (Codex review): a reclaim that lands *idempotently* on a session that
    already has the narrowed identity is still a landed reclaim -- `created=False` there means "this
    call did not make it", never "the identity did not move". The old code read `created` for both
    questions and reported the pre-narrowing cards (`None`) for a session whose own `session.json`
    already said `financial-reporting`, so `start()` went on to reason over every card against a
    session that claimed to be narrowed."""
    from requivo.core.contracts import ContextJudgment

    svc = SessionService()
    # A session already sitting under the exact identity grounding is about to narrow onto -- the
    # reachable shape is a prior call's own abandoned narrowing attempt, same family as the P2 test
    # above, one call further down the chain.
    already_narrowed = svc.create_session(
        "a request", context_cards=["financial-reporting"], perimeter=SOFTWARE)

    class _Grounds(_Router):
        def judge_context(self, request, *, cards):
            return ContextJudgment(decision="installed", reason="finance",
                                   cards=["financial-reporting"])

    meta, _grounding, cards, _routing = _disco(_Grounds()).claim_and_ground(
        "a request", cards=None, slug=None)

    assert meta.slug == already_narrowed.slug
    assert cards == ["financial-reporting"], (
        "an idempotent landing on the correct identity was reported as though nothing narrowed")
    assert meta.context_cards == ["financial-reporting"]


def test_a_route_that_cannot_land_is_not_announced_as_though_it_did(workspace):
    """#601 P2 (Codex review): when a reclaim's own delete is refused (the fourth precondition gone
    by the time the router replies), the session stays exactly where it was claimed, and the
    returned `Routing` must say so rather than keep reporting a route that never took effect --
    reasoning under one perimeter while the screen names another is the exact failure this router
    exists to remove.

    Round four's own fix (`find_existing_session`, invariant 13) means a *leftover* claim from an
    earlier interrupted run is now resolved before routing is ever asked (a cheaper, earlier fix for
    the identical case this test used to construct), so the shape that still reaches this branch is
    a concurrent writer discovering *this call's own* freshly-claimed placeholder while the routing
    judgment is in flight -- the same race pinned for the grounding side, one call earlier, by
    `test_a_session_that_moved_off_revision_zero_during_the_judgment_is_left_alone`."""
    from conftest import full_model as _full_model

    class _RoutesMidJudgment(_Router):
        def judge_perimeter(self, request, *, perimeters):
            slug = SessionService().list_sessions()[0].slug
            SessionService().update_model(slug, _full_model())
            return super().judge_perimeter(request, perimeters=perimeters)

    router = _RoutesMidJudgment(
        PerimeterJudgment(decision="fits", reason="a launch plan", perimeter=GO_TO_MARKET))
    meta, _grounding, _cards, routing = _disco(router).claim_and_ground(
        "a request", cards=None, slug=None)

    assert meta.perimeter == SOFTWARE, "reasoning proceeded under a perimeter the screen does not name"
    assert SessionService().meta(meta.slug).current_revision == 1, "the concurrent write was lost"
    assert routing.judgment is None, "a route that did not land was still reported as having fit"
    assert "could not" in routing.why_not


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


def _at_a_terminal(monkeypatch) -> None:
    """Mirrors `test_cli_interactive.py`'s own helper, duplicated rather than imported -- a test
    reaching into a sibling module for a helper breaks when that module reorganises, the rule
    `test_cli_flag_names.py` states from the other side."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


def test_an_ambiguous_verdict_asks_interactively_and_continues_under_the_chosen_answer(
        workspace, monkeypatch):
    """JB's call over the issue's own wording (#601): interactive discovery asks, one question, not
    a guess, using the identical `input()` pattern `_prompt_answers` has since #592 -- and the
    re-run with an explicit perimeter costs no second routing call, so the total stays three: the
    same count a first-try `fits` verdict would have cost."""
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "2")   # 1=software, 2=go-to-market
    fake = FakeClient(_AMBIGUOUS_REPLY, _JUDGMENT_REPLY, _gtm_reply())

    printed = _run_app(["discover", "help with our internal tool rollout"], client=fake)

    assert len(fake.calls) == 3, "asking interactively must not cost a second routing call"
    assert "could be either a system or a launch plan" in printed
    assert "Continuing under go-to-market" in printed
    assert SessionService().list_sessions()[0].perimeter == GO_TO_MARKET


def test_an_ambiguous_verdict_with_no_tty_refuses_without_reading_stdin(workspace, capsys):
    """The other half of the same acceptance criterion: closed stdin (no `_at_a_terminal` patch, and
    no `--once`) must take the same non-interactive refusal `--once` does, never block on a read
    that will never return. If this test hangs, that is the failure."""
    fake = FakeClient(_AMBIGUOUS_REPLY)

    with pytest.raises(SystemExit) as exit_:
        app(["discover", "an ambiguous request"], client=fake)

    assert exit_.value.code == 1
    assert len(fake.calls) == 1
    assert SessionService().list_sessions() == []
    assert "could be either" in capsys.readouterr().err


def test_claim_and_ground_resolves_an_existing_non_default_perimeter_session_before_routing(
        workspace):
    """The mechanism behind the CLI-level repro below, isolated (#601, Codex review round four):
    invariant 13 promises a repeat discovery is refused before any paid call -- and a repeat of a
    request already routed to go-to-market has an identity the *default*-perimeter claim never
    matches, so the free revision-zero gate used to slip past on the wrong identity and the routing
    judgment got billed before the real session was ever found. `find_existing_session` must be
    consulted, for every installed perimeter, before either the claim or the router."""
    first = _disco(_Router(PerimeterJudgment(decision="fits", reason="a launch plan",
                                             perimeter=GO_TO_MARKET)))
    meta, _grounding, _cards, _routing = first.claim_and_ground("a request", cards=None, slug=None)
    assert meta.perimeter == GO_TO_MARKET

    from requivo.core.contracts import schema_slot_ids
    _, required = schema_slot_ids(GO_TO_MARKET)
    reply = {"model": {sid: {"completeness": 90, "confidence": "explicit", "impact": "high",
                             "value": "x", "evidence": "y"} for sid in required},
            "questions": [], "summary": {"objective": "grow the funnel"}}
    SessionService().update_model(meta.slug, json.dumps(reply))   # a completed discovery: revision 1

    second_router = _Router()   # a fresh stub -- its judge_perimeter must never be reached
    with pytest.raises(RevisionConflictError):
        _disco(second_router).claim_and_ground("a request", cards=None, slug=None)
    assert second_router.asked == [], (
        "the routing judgment was billed on a repeat of an already-discovered session")


@pytest.mark.parametrize("argv_tail", [["--once"], []], ids=["once", "interactive"])
def test_a_repeat_of_a_routed_sessions_discovery_costs_no_provider_call(workspace, monkeypatch, capsys, argv_tail):
    """The CLI walk of the test above, both entry points (#601, Codex review round four).
    `test_both_discover_entry_points_refuse_a_refined_session_before_paying` (#133, #593) pins this
    identical rule for a session under the *default* perimeter; this is the shape that actually
    slipped, because #601's router claims under a perimeter no earlier call named. `FakeClient()`
    with no replies at all -- any provider call attempted here fails loudly, not quietly."""
    _at_a_terminal(monkeypatch)
    _run_app(["discover", "help us launch this new product", "--once"],
             client=FakeClient(_GTM_FITS_REPLY, _JUDGMENT_REPLY, _gtm_reply()))  # -> go-to-market, revision 1

    fake = FakeClient()
    with pytest.raises(SystemExit) as exit_:
        app(["discover", "help us launch this new product", *argv_tail], client=fake)

    assert exit_.value.code == 1
    assert "already carries a model" in capsys.readouterr().err
    assert fake.calls == [], (
        f"{len(fake.calls)} provider call(s) were billed before the refusal on a session already "
        f"routed to a non-default perimeter")


def test_an_interrupt_at_the_perimeter_prompt_exits_130_not_1(workspace, monkeypatch):
    """#601 P2 (Codex review, round four): an operator cancelling at the perimeter prompt is not a
    failed judgment. `_prompt_perimeter_choice` used to catch `KeyboardInterrupt` and return `None`,
    which fed the caller's `raise` -- re-raising `AmbiguousPerimeterError`, a `RequivoError`, so
    `app()` exited 1 where every other interrupt in this file exits 130
    (`docs/compatibility.md`'s own published contract)."""
    _at_a_terminal(monkeypatch)

    def _interrupt(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", _interrupt)
    fake = FakeClient(_AMBIGUOUS_REPLY)

    with pytest.raises(SystemExit) as exit_:
        app(["discover", "an ambiguous request"], client=fake)

    assert exit_.value.code == 130
    assert len(fake.calls) == 1


def test_eof_at_the_perimeter_prompt_still_refuses_cleanly(workspace, monkeypatch, capsys):
    """The must-fire control for the arm that does stay caught: EOF is the non-interactive-style
    refusal even at an interactive prompt -- `_prompt_perimeter_choice` returns `None`, the caller
    re-raises the original `AmbiguousPerimeterError`, and `app()` exits 1 with the candidates named,
    same as `--once`."""
    _at_a_terminal(monkeypatch)

    def _eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", _eof)
    fake = FakeClient(_AMBIGUOUS_REPLY)

    with pytest.raises(SystemExit) as exit_:
        app(["discover", "an ambiguous request"], client=fake)

    assert exit_.value.code == 1
    assert len(fake.calls) == 1
    assert "could be either" in capsys.readouterr().err


def test_a_failed_routing_call_does_not_lock_the_retry_into_the_default_perimeter(workspace):
    """#601 P2 (Codex review, round five): round four's own fix traded "pays before refusing" for
    "never routes again", which is worse. A routing call that fails or is interrupted left its
    revision-zero software placeholder behind, and the retry's own `find_existing_session` (round
    four's own check) found it and read the accidental default as a *decision* -- so the router was
    never consulted again, and the session silently stayed under software while its own `Routing`
    reported that `--perimeter` had been supplied, which nobody did. The two states genuinely
    differ (a session whose routing completed vs. a placeholder a routing call never finished) and
    must not be confused: the placeholder is cleaned up on any failed or interrupted routing call,
    the same "disposable until routing lands" rule the ambiguous-verdict branch already lives by --
    never a persisted "routing completed" field, which would touch the public session format for
    every session, including the ones that never route at all."""
    from requivo.providers.errors import EngineError

    class _FailsOnce(_Router):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.attempts = 0

        def judge_perimeter(self, request, *, perimeters):
            self.attempts += 1
            if self.attempts == 1:
                raise EngineError("transport failure")
            return super().judge_perimeter(request, perimeters=perimeters)

    router = _FailsOnce(
        PerimeterJudgment(decision="fits", reason="a launch plan", perimeter=GO_TO_MARKET))
    disco = _disco(router)

    with pytest.raises(EngineError):
        disco.claim_and_ground("a request", cards=None, slug=None)
    assert SessionService().list_sessions() == [], (
        "the failed routing call's own placeholder survived")

    meta, _grounding, _cards, routing = disco.claim_and_ground("a request", cards=None, slug=None)

    assert router.attempts == 2, "the retry never asked the router again"
    assert meta.perimeter == GO_TO_MARKET
    assert routing.judgment is not None and routing.judgment.decision.value == "fits", (
        "the retry silently reasoned under the leftover default instead of asking the router")
