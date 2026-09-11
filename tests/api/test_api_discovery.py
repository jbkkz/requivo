"""Discovery write routes (#425, slice 2): the first turn and the answers-as-turns fold-in.

Every reasoning path here runs offline against `with_provider`'s `FakeClient` -- no network, no real
provider. The refused-before-payment tests are the load-bearing half (invariant 13's own lesson):
they assert the provider's *call count*, not only the response status, because a test that only
checks the status would still pass if the guard fired one line too late, after the call was already
made."""

from __future__ import annotations

import json

from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.services.sessions import SessionService
from tests.api.conftest import seed_session


def _full_model(**overrides) -> dict:
    _, required = schema_slot_ids()
    model = {sid: {"completeness": 0, "confidence": "empty", "impact": "low"}
             for sid in _schema_order() if sid in required}
    model.update(overrides)
    return model


def _engine_reply(**overrides) -> str:
    return json.dumps({"model": _full_model(**overrides), "questions": [],
                       "summary": {"objective": "A leave approval system"}})


def test_discover_runs_the_first_turn(client, with_provider):
    with_provider(_engine_reply(business_rules={"completeness": 80, "confidence": "explicit",
                                                "impact": "high"}))
    create = client.post("/api/v1/sessions", json={"request": "A leave approval system."})
    slug = create.json()["slug"]

    resp = client.post(f"/api/v1/sessions/{slug}/discover")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied"
    assert body["revision"] == 1
    assert SessionService().meta(slug).current_revision == 1


def test_discover_above_revision_zero_is_refused_before_any_provider_call(client, with_provider):
    """Invariant 13: a session that has already been discovered may not be discovered again --
    the gate fires before the (here, empty) provider queue is ever touched."""
    slug = seed_session("leave-approval")  # already at revision 1
    fake = with_provider()  # no replies queued -- a call here would raise IndexError, not merely cost money

    resp = client.post(f"/api/v1/sessions/{slug}/discover")
    assert resp.status_code == 409
    assert resp.json()["code"] == "revision_conflict"
    assert fake.calls == []


def test_answer_folds_into_the_model_as_a_new_revision(client, with_provider):
    slug = seed_session("leave-approval")
    with_provider(_engine_reply(business_rules={"completeness": 90, "confidence": "explicit",
                                                "impact": "high"}))

    resp = client.post(f"/api/v1/sessions/{slug}/answers",
                       json={"answers": "Exceptions go to Finance.", "expected_revision": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied"
    assert body["revision"] == 2


def test_answer_requires_expected_revision_in_the_body(client, with_provider):
    slug = seed_session("leave-approval")
    fake = with_provider()  # omitted body field must never reach the provider either

    resp = client.post(f"/api/v1/sessions/{slug}/answers", json={"answers": "Exceptions go to Finance."})
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"
    assert fake.calls == []


def test_answer_with_a_stale_expected_revision_is_refused_before_any_provider_call(client, with_provider):
    """#205: a precondition already known to be stale is refused before payment, not after."""
    slug = seed_session("leave-approval")  # current revision is 1
    fake = with_provider()  # empty queue -- a reached call would raise, not merely waste a token

    resp = client.post(f"/api/v1/sessions/{slug}/answers",
                       json={"answers": "Exceptions go to Finance.", "expected_revision": 0})
    assert resp.status_code == 409
    assert resp.json()["code"] == "revision_conflict"
    assert fake.calls == []


def test_answer_with_the_current_revision_still_succeeds(client, with_provider):
    """The must-fire control for the two refusal tests above: a *correct* `expected_revision` is not
    itself refused -- the guard only catches a stale one."""
    slug = seed_session("leave-approval")
    fake = with_provider(_engine_reply())

    resp = client.post(f"/api/v1/sessions/{slug}/answers",
                       json={"answers": "Exceptions go to Finance.", "expected_revision": 1})
    assert resp.status_code == 200
    assert len(fake.calls) == 1


def test_discover_reports_session_locked_with_a_retry_after_header(client, app):
    """503 `session_locked`, `Retry-After` set -- the write never started, so resubmitting the
    identical request shortly is the documented recovery (§2 of the decision record)."""
    from requivo.api.dependencies import get_discovery
    from requivo.core.errors import SessionLockedError

    slug = "locked-session"  # never actually created -- the fake below never touches the store

    class _LockedDiscovery:
        def run_discovery(self, slug, *, surface):
            raise SessionLockedError(
                f"session '{slug}' is locked by another process; retry in a moment",
                details={"slug": slug})

    app.dependency_overrides[get_discovery] = lambda: _LockedDiscovery()
    try:
        resp = client.post(f"/api/v1/sessions/{slug}/discover")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503
    assert resp.json()["code"] == "session_locked"
    assert resp.headers["retry-after"] == "1"
