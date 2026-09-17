"""#255: the input-size cap lives in the service layer, not only in web config (invariant 14)."""

from __future__ import annotations

import pytest
from _fakes import _ENGINE_REPLY, FakeClient

from requivo.core.contracts import MAX_INPUT_CHARS
from requivo.core.errors import InputTooLargeError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(workspace):
    """conftest's `workspace` fixture, applied automatically to every test in this module."""


def test_an_oversized_request_is_refused_before_any_provider_call():
    """Invariant 3, first half: refuse, don't truncate (#286)."""
    fake = FakeClient(_ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    with pytest.raises(InputTooLargeError):
        disco.start("x" * (MAX_INPUT_CHARS + 1))
    assert fake.calls == []
    assert not SessionService().list_sessions()


def test_a_request_of_exactly_the_ceiling_still_reaches_the_provider():
    """Must-fire control: without it, a service that refused everything would also pass the test above,
    telling us nothing about where the ceiling actually sits."""
    fake = FakeClient(_ENGINE_REPLY)
    disco = DiscoveryService(client=fake)
    disco.start("x" * MAX_INPUT_CHARS)
    assert len(fake.calls) == 1
    assert len(SessionService().list_sessions()) == 1


def test_create_only_refuses_an_oversized_request_too():
    """The 'capture now, discover later' path persists the request directly and `run_discovery` later hands it
    to the provider with no size check of its own."""
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
    """The interactive loop's own un-persisted entry point."""
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
