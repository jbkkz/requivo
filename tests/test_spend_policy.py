"""#427: an injectable service-layer spend ceiling, checked before every provider call."""
from __future__ import annotations

import pytest
from _fakes import StubProvider, seeded

from requivo.core.contracts import PRD, Brief, PerimeterJudgment, Stories, Story
from requivo.core.errors import SpendCeilingReachedError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.usage import CallRecord, SpendPolicy, UsageLedger, record_call, track_usage

pytestmark = pytest.mark.usefixtures("workspace")

REQUEST = "a leave approval system"


class _Billing(StubProvider):
    """A `ReasoningProvider` that bills a fixed amount per call, routing included."""

    def __init__(self, cost_per_call: float = 0.05, rate=(2.0, 10.0)):
        super().__init__(artifacts={"brief": Brief(complexity="low", solution="S"),
                                    "stories": Stories(stories=[Story(id="s1", title="Story 1")]),
                                    "prd": PRD(title="T", problem="P")})
        self.billed = 0
        self._cost_per_call, self._rate = cost_per_call, rate

    def _bill(self) -> None:
        self.billed += 1
        record_call(CallRecord(model="stub-model", input_tokens=int(self._cost_per_call * 1_000_000 / self._rate[0]),
                               rate_per_mtok=self._rate, priced_as_of="2026-09-01"))

    def analyze(self, request, **kwargs):
        self._bill()
        return super().analyze(request, **kwargs)

    def judge_perimeter(self, request, *, perimeters):
        self._bill()
        return PerimeterJudgment(decision="none", reason="an ordinary request")

    def generate(self, artifact_type, model, **kwargs):
        self._bill()
        return super().generate(artifact_type, model, **kwargs)


def _ledger_at(ceiling: float) -> None:
    """One already-billed call on the active ledger whose estimated cost sits exactly at `ceiling`."""
    record_call(CallRecord(model="stub-model", input_tokens=int(ceiling * 1_000_000), rate_per_mtok=(1.0, 1.0), priced_as_of="2026-09-01"))


def _capped(provider, sessions=None, ceiling=0.10) -> DiscoveryService:
    return DiscoveryService(provider=provider, sessions=sessions or SessionService(), spend_policy=SpendPolicy(ceiling_usd=ceiling))


# ── SpendPolicy.check(), in isolation ──────────────────────────────────────────


def test_check_raises_at_or_above_the_ceiling():
    """A no-op with no ledger; the must-fire control: a cent above the spend, it does not raise."""
    SpendPolicy(ceiling_usd=0.0).check(None)
    ledger = UsageLedger()
    ledger.record(CallRecord(model="m", input_tokens=1_000_000, rate_per_mtok=(1.0, 1.0), priced_as_of="d"))
    with pytest.raises(SpendCeilingReachedError) as exc_info:
        SpendPolicy(ceiling_usd=1.0).check(ledger)
    assert exc_info.value.code == "spend_ceiling_reached"
    assert exc_info.value.details["reason"] == "ceiling_reached" and exc_info.value.details["spent_usd"] == 1.0
    SpendPolicy(ceiling_usd=1.01).check(ledger)


def test_check_refuses_an_unpriced_call_rather_than_treating_it_as_free():
    """Invariant 6, applied to money: a call with no rate on file cannot be compared against the ceiling."""
    ledger = UsageLedger()
    ledger.record(CallRecord(model="m", input_tokens=1_000_000))  # no rate_per_mtok
    with pytest.raises(SpendCeilingReachedError) as exc_info:
        SpendPolicy(ceiling_usd=1000.0).check(ledger)
    assert exc_info.value.details["reason"] == "unpriced_call" and exc_info.value.details["spent_usd"] is None


# ── every DiscoveryService chokepoint, driven from the outside ────────────────


def test_default_no_policy_is_byte_identical_to_before_this_existed():
    """Pinned by the issue's own acceptance criteria (#467)."""
    sessions = SessionService()
    provider = _Billing(cost_per_call=1_000_000.0)
    with track_usage():
        DiscoveryService(provider=provider, sessions=sessions).start(REQUEST, finalize=True)
    assert provider.billed == 2 and sessions.repo.read_meta(sessions.list_sessions()[0].slug).current_revision == 2


def test_a_ceiling_not_yet_reached_still_reaches_the_provider():
    """Must-fire control for every refusal test below; and no active ledger lets a policy through uncounted."""
    provider = _Billing(cost_per_call=0.01)
    with track_usage():
        _capped(provider).start(REQUEST)
    assert provider.billed == 1
    provider = _Billing()
    _capped(provider, ceiling=0.0).draft_turn(REQUEST)  # `current_ledger()` is None: nothing to check against
    assert provider.billed == 1


def test_start_refuses_its_second_call_once_the_first_alone_reaches_the_ceiling():
    """The check runs before EACH provider call inside one operation (#467)."""
    sessions = SessionService()
    provider = _Billing(cost_per_call=0.05)
    with track_usage(), pytest.raises(SpendCeilingReachedError):
        _capped(provider, sessions, ceiling=0.05).start(REQUEST, finalize=True)
    assert provider.billed == 1  # only analyze() ran; generate("brief") was refused
    assert sessions.repo.read_meta(sessions.list_sessions()[0].slug).current_revision == 1


# (act, needs a model) -- `start` claims its own revision-0 session before the check; `run_discovery` takes one.
_CHOKEPOINTS = {
    "start": (lambda d, slug: d.start(REQUEST), False),
    "draft_turn": (lambda d, slug: d.draft_turn(REQUEST), True),
    "route_perimeter": (lambda d, slug: d.route_perimeter(REQUEST, perimeter=None), True),  # #601 P2: had no check
    "run_discovery": (lambda d, slug: d.run_discovery(slug), False),
    "answer": (lambda d, slug: d.answer(slug, "more detail"), True),
    "reason": (lambda d, slug: d.reason(slug, "stories"), True),
    "generate brief": (lambda d, slug: d.generate(slug, "brief"), True),
    "generate prd": (lambda d, slug: d.generate(slug, "prd"), True),  # the ordinary writer branch, a distinct call site
}


@pytest.mark.parametrize("chokepoint", list(_CHOKEPOINTS))
def test_every_chokepoint_refuses_before_the_provider_call(chokepoint):
    act, with_model = _CHOKEPOINTS[chokepoint]
    sessions = SessionService()
    slug = seeded(sessions, REQUEST) if with_model else (sessions.create_session(REQUEST).slug if chokepoint == "run_discovery" else None)
    provider = _Billing()
    with track_usage():
        _ledger_at(0.10)
        with pytest.raises(SpendCeilingReachedError):
            act(_capped(provider, sessions), slug)
    assert provider.billed == 0
    slug = slug or sessions.list_sessions()[0].slug
    assert sessions.repo.read_meta(slug).current_revision == (1 if with_model else 0)
