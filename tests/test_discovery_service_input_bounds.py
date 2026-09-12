"""#255: the input-size cap lives in the service layer, not only in web config (invariant 14).

Driven directly against `DiscoveryService`/`SessionService` -- no CLI, no web -- because the whole
point of the issue is that a caller reaching past both surfaces still gets the refusal. Before this
fix `read_user_text`/`web/config.py` were the only places a size was ever checked, so any of these
would have sailed straight through to the (billed) provider call. The cost was not a failure: with
a 1M-token context window a multi-megabyte paste does not even fail at the API, it just bills, and
the interactive loop resends the request on every turn, multiplying that by however many turns it
takes to converge. `MAX_INPUT_CHARS` (`core/contracts.py`) is the one ceiling, and `web/config.py`
re-exports it under its own names so the routes keep their friendly re-render on top.
"""

from __future__ import annotations

import pytest
from _fakes import _ENGINE_REPLY, FakeClient

from requivo.core.contracts import MAX_INPUT_CHARS
from requivo.core.errors import InputTooLargeError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))


def test_an_oversized_request_is_refused_before_any_provider_call():
    fake = FakeClient(_ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    with pytest.raises(InputTooLargeError):
        disco.start("x" * (MAX_INPUT_CHARS + 1))
    assert fake.calls == []
    assert not SessionService().list_sessions()


def test_a_request_of_exactly_the_ceiling_still_reaches_the_provider():
    """Must-fire control: without it, a service that refused everything would also pass the test
    above, telling us nothing about where the ceiling actually sits."""
    fake = FakeClient(_ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    disco.start("x" * MAX_INPUT_CHARS)
    assert len(fake.calls) == 1
    assert len(SessionService().list_sessions()) == 1


def test_create_only_refuses_an_oversized_request_too():
    """The 'capture now, discover later' path persists the request directly and `run_discovery`
    later hands it to the provider with no size check of its own -- so the cap has to hold on this
    path too, not only on immediate discovery (#255's audit measured the billing risk here)."""
    disco = DiscoveryService(client=FakeClient())
    with pytest.raises(InputTooLargeError):
        disco.create_only("x" * (MAX_INPUT_CHARS + 1))
    assert not SessionService().list_sessions()


def test_oversized_answers_are_refused_before_the_paid_turn():
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    slug = disco.start("a leave approval system")
    assert len(fake.calls) == 1

    with pytest.raises(InputTooLargeError):
        disco.answer(slug, "y" * (MAX_INPUT_CHARS + 1))
    assert len(fake.calls) == 1                       # refused before the second call was billed

    # must-fire control, same fixture
    disco.answer(slug, "y" * MAX_INPUT_CHARS)
    assert len(fake.calls) == 2


def test_draft_turn_refuses_an_oversized_request_before_reasoning():
    """The interactive loop's own un-persisted entry point -- the path the audit measured the real
    cost on: up to eight turns of a re-sent oversized request, all billed, before the loop ever
    reaches `finalize_discovery` and the session-creation cap above."""
    fake = FakeClient(_ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    with pytest.raises(InputTooLargeError):
        disco.draft_turn("x" * (MAX_INPUT_CHARS + 1))
    assert fake.calls == []


def test_draft_turn_refuses_oversized_answers_before_reasoning():
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    model = disco.draft_turn("a leave approval system")
    assert len(fake.calls) == 1

    with pytest.raises(InputTooLargeError):
        disco.draft_turn("a leave approval system", current_model=model,
                         answers="y" * (MAX_INPUT_CHARS + 1))
    assert len(fake.calls) == 1

    # must-fire control, same fixture
    disco.draft_turn("a leave approval system", current_model=model, answers="y" * MAX_INPUT_CHARS)
    assert len(fake.calls) == 2
