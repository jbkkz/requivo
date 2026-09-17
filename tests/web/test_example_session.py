"""#226 — keyless activation on the product surface, and the click that seeds the decision brief too (#429)."""

from __future__ import annotations

import pytest

from requivo.core.persistence import _held_locks, canonical_dir, list_session_slugs
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService
from requivo.web.example import EXAMPLE_SLUG, example_brief, example_proposal, example_request, seed_example
from requivo.web.viewmodels.labels import EXAMPLE_BADGE
from requivo.web.viewmodels.sessions import session_list
from tests.web.conftest import seed_session


@pytest.fixture(autouse=True)
def no_provider_may_be_reached(monkeypatch):
    """Must fire. `DiscoveryService._need_provider` is the single door onto every paid call."""
    def _boom(self):
        raise AssertionError("the example path reached the provider — it must be entirely offline")
    monkeypatch.setattr("requivo.services.discovery.DiscoveryService._need_provider", _boom)


def _seed(client):
    """Click the affordance. Returns the slug the redirect landed on."""
    r = client.post("/sessions/example", follow_redirects=False)
    assert r.status_code == 303, r.text[:400]
    location = r.headers["location"]
    assert location.startswith("/sessions/"), location
    return location.rsplit("/", 1)[-1]


def _ordinary(slug="a-real-request"):
    """A session the reader made themselves — the control every labelling assertion needs."""
    return seed_session(slug, "A leave approval request", objective="Leave approval")


# ── the affordance is on the page a keyless visitor lands on ──────────────────


def test_a_keyless_empty_workspace_offers_a_route_into_the_example(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Set ANTHROPIC_API_KEY" in r.text          # must fire: this really is the keyless state
    assert 'action="/sessions/example"' in r.text and "Nothing here yet" in r.text


def test_the_example_stays_reachable_once_a_real_session_exists(client):
    """The issue proposed showing this only on an empty workspace."""
    _ordinary()
    r = client.get("/")
    assert r.status_code == 200 and "a-real-request" in r.text   # must fire: the real session is listed
    assert 'action="/sessions/example"' in r.text


def test_seeding_is_refused_without_the_cross_site_token(raw_client):
    """Same guard as every other POST in this app."""
    r = raw_client.post("/sessions/example", follow_redirects=False)
    assert r.status_code == 403, r.text[:200]
    assert not list_session_slugs()


# ── what one click produces ───────────────────────────────────────────────────


def test_one_click_yields_a_browsable_session_through_the_validated_path(client):
    """A real session, not a hand-written directory (the issue's own constraint), with no key and no call."""
    slug = _seed(client)
    r = client.get(f"/sessions/{slug}")
    assert r.status_code == 200, r.text[:400]
    assert "door staff" in r.text and "What could change the solution" in r.text and "Are we ready?" in r.text
    svc = SessionService()
    assert svc.meta(slug).current_revision == 1
    assert (canonical_dir(slug) / "revisions" / "0001-model.json").exists()
    status = svc.status(slug)
    assert status["questions"] and status["readiness"]["ready"] in (True, False)


def test_the_revision_claims_no_provider_it_did_not_use(client):
    """Invariant 6 — provenance is real or absent."""
    record = SessionService().meta(_seed(client)).revisions[-1]
    assert record.surface == "web-example" and record.provider is None and record.model_name is None


def test_a_second_click_returns_to_the_same_session_rather_than_making_another(client):
    """`create_session` is an atomic claim on a slug (invariant 11) and idempotent on identity."""
    first = _seed(client)
    revision = SessionService().meta(first).current_revision
    assert _seed(client) == first and list_session_slugs() == [first]
    assert SessionService().meta(first).current_revision == revision


# ── it says what it is, wherever it appears ───────────────────────────────────


def test_the_example_names_itself_in_the_listing_and_on_its_own_page_beside_a_real_session(client):
    """The issue's own acceptance criterion, and the control is the point."""
    ordinary = _ordinary()
    slug = _seed(client)
    rows = {r["slug"]: r for r in session_list(SessionService())}
    assert rows[slug]["is_example"] is True and rows[ordinary]["is_example"] is False   # must fire
    assert EXAMPLE_BADGE in client.get("/").text
    own = client.get(f"/sessions/{slug}").text
    assert EXAMPLE_BADGE in own and "bundled" in own.lower()
    assert "no API key" in own                        # what a keyless reader can and cannot do
    assert EXAMPLE_BADGE not in client.get(f"/sessions/{ordinary}").text   # must fire


def test_the_example_is_recognised_by_what_it_asks_not_by_the_name_it_landed_under(client):
    """`is_example` compares the request text against the bundled payload rather than testing the slug."""
    SessionService().create_session("Something else entirely", slug=EXAMPLE_SLUG)
    slug = _seed(client)
    assert slug != EXAMPLE_SLUG
    rows = {r["slug"]: r for r in session_list(SessionService())}
    assert rows[slug]["is_example"] is True and rows[EXAMPLE_SLUG]["is_example"] is False


def test_the_bundled_brief_is_read_rather_than_restated():
    """The request the session captures is the client email itself, and the brief is the bundled file (#429)."""
    request = example_request()
    assert request.startswith("Look, the whole event thing is chaos")
    assert "# Request" not in request and ">" not in request
    assert set(example_proposal()) >= {"model", "questions", "summary"}
    brief = example_brief()
    assert brief.startswith("# Decision Brief") and "One word, two different problems" in brief


def test_seeding_without_a_running_server_needs_only_the_service(client):
    """`seed_example` is the whole operation; the route is a redirect around it."""
    slug = seed_example(SessionService())
    assert SessionService().meta(slug).current_revision == 1


# ── #429: the click delivers the decision brief too, not just the understanding ──


def test_one_click_also_seeds_the_decision_brief_no_key_needed(client):
    """README.md's own promise; "Nothing generated yet" still shows for the *other* documents."""
    slug = _seed(client)
    r = client.get(f"/sessions/{slug}/artifacts/brief")
    assert r.status_code == 200, r.text[:400]
    assert "Decision Brief" in r.text and "One word, two different problems" in r.text   # a real challenge
    page = client.get(f"/sessions/{slug}").text
    assert "Decision brief" in page and "Up to date" in page
    assert "The decision brief is what you take into a scope review" not in page


def test_a_second_click_does_not_reseed_or_duplicate_the_brief(client, monkeypatch):
    """Idempotent on identity, not a fresh write every time (#428); a reader's own brief is never overwritten."""
    calls = []
    real_save = ArtifactService.save

    def _counting_save(self, *args, **kwargs):
        calls.append((args, kwargs))
        return real_save(self, *args, **kwargs)
    monkeypatch.setattr(ArtifactService, "save", _counting_save)
    first = _seed(client)
    assert _seed(client) == first
    brief_calls = [c for c in calls if c[0][:2] == (first, "brief")]
    assert len(brief_calls) == 1, f"expected exactly one ArtifactService.save('brief', ...) across two clicks, got {len(brief_calls)}"
    written = "# Decision Brief\n\nSomething the reader actually generated.\n"
    ArtifactService().save(first, "brief", written, source_revision=1)
    assert client.post("/sessions/example", follow_redirects=False).status_code == 303
    assert ArtifactService().show(first, "brief") == written


def test_seeding_the_brief_holds_the_lock_across_the_check_and_the_save(monkeypatch):
    """#428 review finding: the check-then-act on the saved brief runs under the session lock."""
    sessions = SessionService()
    seen = {}
    real_list = ArtifactService.list

    def _spying_list(self, slug):
        key = sessions.repo._resolve_store()._lock_key(slug)
        seen["depth_during_list"] = getattr(_held_locks, "depths", {}).get(key, 0)
        return real_list(self, slug)
    monkeypatch.setattr(ArtifactService, "list", _spying_list)
    seed_example(sessions)   # the first click -- the only one that reaches this gate
    assert "depth_during_list" in seen, "ArtifactService.list was never called -- gate not reached"
    assert seen["depth_during_list"] > 0, "artifacts.list() ran with the lock NOT held -- the check-then-act race is back"
