"""#427: an injectable service-layer spend ceiling, checked before every provider call."""
from __future__ import annotations

import pytest
from _fakes import StubProvider, out, slot

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


def _seeded(sessions: SessionService) -> str:
    """A session already carrying a model (revision 1)."""
    meta = sessions.create_session(REQUEST)
    sessions.update_model(meta.slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json())
    return meta.slug


def _ledger_at_or_above(ceiling: float) -> None:
    """One already-billed call on the active ledger whose estimated cost sits exactly at `ceiling`."""
    record_call(CallRecord(model="stub-model", input_tokens=int(ceiling * 1_000_000),
                           rate_per_mtok=(1.0, 1.0), priced_as_of="2026-09-01"))


def _one_dollar_ledger() -> UsageLedger:
    ledger = UsageLedger()
    ledger.record(CallRecord(model="m", input_tokens=1_000_000, rate_per_mtok=(1.0, 1.0), priced_as_of="d"))
    return ledger


# ── SpendPolicy.check(), in isolation ──────────────────────────────────────────


def test_check_is_a_noop_with_no_active_ledger():
    SpendPolicy(ceiling_usd=0.0).check(None)  # must not raise


def test_check_raises_at_or_above_the_ceiling():
    """And the must-fire control: a cent above the spend, it does not raise."""
    with pytest.raises(SpendCeilingReachedError) as exc_info:
        SpendPolicy(ceiling_usd=1.0).check(_one_dollar_ledger())
    assert exc_info.value.code == "spend_ceiling_reached"
    assert exc_info.value.details["reason"] == "ceiling_reached" and exc_info.value.details["spent_usd"] == 1.0
    SpendPolicy(ceiling_usd=1.01).check(_one_dollar_ledger())


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
    assert provider.billed == 2
    assert sessions.repo.read_meta(sessions.list_sessions()[0].slug).current_revision == 2


def test_a_ceiling_not_yet_reached_still_reaches_the_provider():
    """Must-fire control for every refusal test below."""
    provider = _Billing(cost_per_call=0.01)
    with track_usage():
        DiscoveryService(provider=provider, sessions=SessionService(), spend_policy=SpendPolicy(ceiling_usd=0.10)).start(REQUEST)
    assert provider.billed == 1


def test_start_refuses_before_its_first_call_once_the_ceiling_is_already_reached():
    sessions = SessionService()
    provider = _Billing()
    with track_usage():
        _ledger_at_or_above(0.10)
        with pytest.raises(SpendCeilingReachedError):
            DiscoveryService(provider=provider, sessions=sessions, spend_policy=SpendPolicy(ceiling_usd=0.10)).start(REQUEST)
    assert provider.billed == 0
    # `claim_session()` (revision 0, no model) runs before the spend check.
    assert sessions.repo.read_meta(sessions.list_sessions()[0].slug).current_revision == 0


def test_start_refuses_its_second_call_once_the_first_alone_reaches_the_ceiling():
    """The check runs before EACH provider call inside one operation (#467)."""
    sessions = SessionService()
    provider = _Billing(cost_per_call=0.05)
    with track_usage(), pytest.raises(SpendCeilingReachedError):
        DiscoveryService(provider=provider, sessions=sessions, spend_policy=SpendPolicy(ceiling_usd=0.05)).start(REQUEST, finalize=True)
    assert provider.billed == 1  # only analyze() ran; generate("brief") was refused
    assert sessions.repo.read_meta(sessions.list_sessions()[0].slug).current_revision == 1


_CHOKEPOINTS = {
    "draft_turn": lambda d, slug: d.draft_turn(REQUEST),
    "route_perimeter": lambda d, slug: d.route_perimeter(REQUEST, perimeter=None),  # #601 P2: had no check
    "run_discovery": lambda d, slug: d.run_discovery(slug),
    "answer": lambda d, slug: d.answer(slug, "more detail"),
    "reason": lambda d, slug: d.reason(slug, "stories"),
    "generate brief": lambda d, slug: d.generate(slug, "brief"),
    "generate prd": lambda d, slug: d.generate(slug, "prd"),  # the ordinary writer branch, a distinct call site
}


@pytest.mark.parametrize("chokepoint", list(_CHOKEPOINTS))
def test_every_chokepoint_refuses_before_the_provider_call(chokepoint):
    sessions = SessionService()
    fresh = chokepoint == "run_discovery"
    slug = sessions.create_session(REQUEST).slug if fresh else _seeded(sessions)
    provider = _Billing()
    disco = DiscoveryService(provider=provider, sessions=sessions, spend_policy=SpendPolicy(ceiling_usd=0.10))
    with track_usage():
        _ledger_at_or_above(0.10)
        with pytest.raises(SpendCeilingReachedError):
            _CHOKEPOINTS[chokepoint](disco, slug)
    assert provider.billed == 0
    assert sessions.repo.read_meta(slug).current_revision == (0 if fresh else 1)


def test_no_active_ledger_lets_a_policy_through_uncounted():
    """`current_ledger()` returning `None` means no `track_usage()` scope is open: nothing to check against."""
    provider = _Billing()
    DiscoveryService(provider=provider, spend_policy=SpendPolicy(ceiling_usd=0.0)).draft_turn(REQUEST)
    assert provider.billed == 1
