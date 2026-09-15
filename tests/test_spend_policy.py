"""#427: an injectable service-layer spend ceiling, before automation can drive paid calls.

Filed from the 2026-09 readiness audit's security pass: `requivo.usage` recorded spend and never
gated it, and the only ceilings anywhere were the per-call input/output size bounds. `DiscoveryService`
now consults an optional, injected `SpendPolicy` immediately before every provider call -- the same
chokepoint `_usage_since` already brackets (`decision: the-http-api-facade`).

Driven directly against `DiscoveryService` with a stub `ReasoningProvider` -- no CLI, no web, no real
network -- the same shape `test_revision_usage_provenance.py` and `test_paid_call_safety.py`
use. `_CountingProvider` records a caller-chosen, fixed-cost `CallRecord` per call so the ceiling
arithmetic can be pinned exactly, and counts its own invocations so a refusal that happened *before*
the call reads as `calls == 0`, not merely as a raised exception.

Every provider call site inside `DiscoveryService` is exercised once: `start` (both its calls --
analyze, then generate("brief") when finalizing), `draft_turn`, `run_discovery`, `answer`,
`reason`/`reason_from`, and `generate` (both its branches -- the brief branch and the ordinary
writer branch).
"""

from __future__ import annotations

import pytest
from _fakes import out, slot

from requivo.core.contracts import PRD, Brief, Stories, Story
from requivo.core.errors import SpendCeilingReachedError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.usage import CallRecord, SpendPolicy, UsageLedger, record_call, track_usage


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))


class _CountingProvider:
    """A stub `ReasoningProvider` that bills a fixed, caller-chosen cost per call into whatever
    ledger is active -- the same recording shape a real provider's `_complete()` uses -- and counts
    its own invocations, so a refusal that happened *before* the call reads as `calls == 0`."""

    name = "stub"

    def __init__(self, cost_per_call: float = 0.05, rate=(2.0, 10.0)):
        self.calls = 0
        self._cost_per_call = cost_per_call
        self._rate = rate

    def _bill(self) -> None:
        self.calls += 1
        input_tokens = int(self._cost_per_call * 1_000_000 / self._rate[0])
        record_call(CallRecord(model="stub-model", input_tokens=input_tokens,
                               rate_per_mtok=self._rate, priced_as_of="2026-09-01"))

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
                perimeter=None):
        self._bill()
        return out({"problem": slot(80, "explicit", "high")})

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self._bill()
        if artifact_type == "brief":
            return Brief(complexity="low", solution="S")
        if artifact_type == "stories":
            return Stories(stories=[Story(id="s1", title="Story 1")])
        if artifact_type == "prd":
            return PRD(title="T", problem="P")
        raise NotImplementedError(artifact_type)  # pragma: no cover - not exercised here

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}


def _seeded_session(sessions: SessionService) -> str:
    """A session already carrying a model (revision 1), for the refinement/generation chokepoints
    that refuse a session with none."""
    meta = sessions.create_session("a leave approval system")
    sessions.update_model(meta.slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json())
    return meta.slug


def _ledger_at_or_above(ceiling: float) -> None:
    """Pre-seed the active `track_usage()` ledger with one already-billed call whose estimated cost
    sits exactly at `ceiling` -- standing in for "N calls already happened this operation", so the
    N+1th call is the one under test."""
    record_call(CallRecord(model="stub-model", input_tokens=int(ceiling * 1_000_000),
                           rate_per_mtok=(1.0, 1.0), priced_as_of="2026-09-01"))


# ── SpendPolicy.check(), in isolation ──────────────────────────────────────────

def test_check_is_a_noop_with_no_active_ledger():
    SpendPolicy(ceiling_usd=0.0).check(None)  # must not raise


def test_check_raises_at_or_above_the_ceiling():
    ledger = UsageLedger()
    ledger.record(CallRecord(model="m", input_tokens=1_000_000,
                             rate_per_mtok=(1.0, 1.0), priced_as_of="d"))  # cost is exactly $1.00
    with pytest.raises(SpendCeilingReachedError) as exc_info:
        SpendPolicy(ceiling_usd=1.0).check(ledger)
    assert exc_info.value.code == "spend_ceiling_reached"
    assert exc_info.value.details["reason"] == "ceiling_reached"
    assert exc_info.value.details["spent_usd"] == 1.0


def test_check_below_the_ceiling_does_not_raise():
    """Must-fire control for the test above: without it, a `check` that always raised would also
    pass it, telling us nothing about where the ceiling actually sits."""
    ledger = UsageLedger()
    ledger.record(CallRecord(model="m", input_tokens=1_000_000,
                             rate_per_mtok=(1.0, 1.0), priced_as_of="d"))  # cost is exactly $1.00
    SpendPolicy(ceiling_usd=1.01).check(ledger)  # must not raise


def test_check_refuses_an_unpriced_call_rather_than_treating_it_as_free():
    """invariant 6, applied to money: a call with no rate on file cannot be compared against the
    ceiling honestly. Guessing it costs $0 would let exactly the call this ceiling exists to catch
    spend past it unseen."""
    ledger = UsageLedger()
    ledger.record(CallRecord(model="m", input_tokens=1_000_000))  # no rate_per_mtok
    with pytest.raises(SpendCeilingReachedError) as exc_info:
        SpendPolicy(ceiling_usd=1000.0).check(ledger)  # ceiling is nowhere close, and still refuses
    assert exc_info.value.details["reason"] == "unpriced_call"
    assert exc_info.value.details["spent_usd"] is None


# ── every DiscoveryService chokepoint, driven from the outside ────────────────

def test_default_no_policy_is_byte_identical_to_before_this_existed():
    """Pinned by the issue's own acceptance criteria: no policy injected, no behaviour change, even
    a ludicrously expensive call must go through uncontested. Revision 2, not 1: since #467,
    `start(finalize=True)` lands `analyze()` as revision 1 before attempting the brief, then folds
    the brief in through the ordinary `generate(slug, "brief")` path, so a full success now produces
    two revisions, not one."""
    sessions = SessionService()
    provider = _CountingProvider(cost_per_call=1_000_000.0)
    disco = DiscoveryService(provider=provider, sessions=sessions)  # no spend_policy
    with track_usage():
        disco.start("a leave approval system", finalize=True)
    assert provider.calls == 2
    assert sessions.repo.read_meta(sessions.list_sessions()[0].slug).current_revision == 2


def test_a_ceiling_not_yet_reached_still_reaches_the_provider():
    """Must-fire control for every refusal test below: without it, a policy that refused
    everything unconditionally would also pass all of them."""
    sessions = SessionService()
    provider = _CountingProvider(cost_per_call=0.01)
    disco = DiscoveryService(provider=provider, sessions=sessions,
                             spend_policy=SpendPolicy(ceiling_usd=0.10))
    with track_usage():
        disco.start("a leave approval system")
    assert provider.calls == 1


def test_start_refuses_before_its_first_call_once_the_ceiling_is_already_reached():
    sessions = SessionService()
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions,
                             spend_policy=SpendPolicy(ceiling_usd=0.10))
    with track_usage():
        _ledger_at_or_above(0.10)
        with pytest.raises(SpendCeilingReachedError):
            disco.start("a leave approval system")
    assert provider.calls == 0
    # `claim_session()` (revision 0, no model) runs before the spend check -- same as every other
    # first-discovery entry point (invariant 13) -- so the session exists with nothing paid for
    # or applied to it, not "no session at all".
    slug = sessions.list_sessions()[0].slug
    assert sessions.repo.read_meta(slug).current_revision == 0


def test_start_refuses_its_second_call_once_the_first_alone_reaches_the_ceiling():
    """The check runs before EACH provider call inside one operation: `start(finalize=True)` makes
    two calls (analyze, then generate("brief")), and a ceiling the first alone reaches must stop the
    second. Since #467, `finalize_discovery` writes `analyze()`'s result as revision 1 before the
    brief is attempted, so that write stands -- only the (also refused, so free) second call is
    stopped. See `test_finalize_discovery_keeps_a_paid_analyze_call.py` for the direct guard."""
    sessions = SessionService()
    provider = _CountingProvider(cost_per_call=0.05)
    disco = DiscoveryService(provider=provider, sessions=sessions,
                             spend_policy=SpendPolicy(ceiling_usd=0.05))
    with track_usage():
        with pytest.raises(SpendCeilingReachedError):
            disco.start("a leave approval system", finalize=True)
    assert provider.calls == 1  # only analyze() ran; generate("brief") was refused
    # claim_session() creates the session before any provider call, and finalize_discovery()'s own
    # update_model() ran right after analyze() succeeded -- before the refused second call was even
    # attempted -- so the session sits at revision 1 with the discovered model applied.
    slug = sessions.list_sessions()[0].slug
    assert sessions.repo.read_meta(slug).current_revision == 1


def test_draft_turn_refuses_before_reasoning_once_the_ceiling_is_already_reached():
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, spend_policy=SpendPolicy(ceiling_usd=0.10))
    with track_usage():
        _ledger_at_or_above(0.10)
        with pytest.raises(SpendCeilingReachedError):
            disco.draft_turn("a leave approval system")
    assert provider.calls == 0


def test_run_discovery_refuses_before_the_provider_call():
    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug  # revision 0, no model
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions,
                             spend_policy=SpendPolicy(ceiling_usd=0.10))
    with track_usage():
        _ledger_at_or_above(0.10)
        with pytest.raises(SpendCeilingReachedError):
            disco.run_discovery(slug)
    assert provider.calls == 0
    assert sessions.repo.read_meta(slug).current_revision == 0


def test_answer_refuses_before_the_provider_call():
    sessions = SessionService()
    slug = _seeded_session(sessions)
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions,
                             spend_policy=SpendPolicy(ceiling_usd=0.10))
    with track_usage():
        _ledger_at_or_above(0.10)
        with pytest.raises(SpendCeilingReachedError):
            disco.answer(slug, "more detail")
    assert provider.calls == 0


def test_reason_refuses_before_the_provider_call():
    sessions = SessionService()
    slug = _seeded_session(sessions)
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions,
                             spend_policy=SpendPolicy(ceiling_usd=0.10))
    with track_usage():
        _ledger_at_or_above(0.10)
        with pytest.raises(SpendCeilingReachedError):
            disco.reason(slug, "stories")
    assert provider.calls == 0


def test_generate_brief_refuses_before_the_provider_call():
    sessions = SessionService()
    slug = _seeded_session(sessions)
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions,
                             spend_policy=SpendPolicy(ceiling_usd=0.10))
    with track_usage():
        _ledger_at_or_above(0.10)
        with pytest.raises(SpendCeilingReachedError):
            disco.generate(slug, "brief")
    assert provider.calls == 0


def test_generate_prd_refuses_before_the_provider_call():
    """The ordinary writer branch of `generate()` (not "brief"), a second and distinct call site."""
    sessions = SessionService()
    slug = _seeded_session(sessions)
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions,
                             spend_policy=SpendPolicy(ceiling_usd=0.10))
    with track_usage():
        _ledger_at_or_above(0.10)
        with pytest.raises(SpendCeilingReachedError):
            disco.generate(slug, "prd")
    assert provider.calls == 0


def test_no_active_ledger_lets_a_policy_through_uncounted():
    """Design decision (see the PR body): `current_ledger()` returning `None` means no
    `track_usage()` scope is open at all -- there is no accounting to check the ceiling against, so
    the call proceeds. This mirrors `usage.py`'s own rule for that state everywhere else: absent
    reads as "nothing to report", never as "spent nothing" and never as "spent everything"."""
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, spend_policy=SpendPolicy(ceiling_usd=0.0))
    disco.draft_turn("a leave approval system")  # no track_usage() scope open anywhere
    assert provider.calls == 1
