"""#226 — keyless activation on the product surface.

Web is the declared product experience, and a keyless first run showed an empty page with a
provider notice: nothing to read, nothing to click, and no way to feel what the engine does. The
CLI got `requivo demo` for exactly that reason; this is the same activation, on the surface a
first-time visitor actually lands on.

Two things make this file more than a smoke test:

* **No provider may be reached.** The whole premise is that this path costs nothing and needs no
  key, so the provider seam is booby-trapped rather than merely absent — a seeding path that
  quietly reasoned a turn would otherwise pass every other assertion here.
* **The example must be reachable *and* recognisable.** A sample session indistinguishable from
  the reader's own work is worse than none: it is a session they did not create, in a list they
  own, claiming to be theirs. So every labelling assertion is paired with a must-fire control on
  an ordinary session.
"""

from __future__ import annotations

import json

import pytest

from requivo.core.persistence import canonical_dir, list_session_slugs
from requivo.services.sessions import SessionService
from requivo.web.example import EXAMPLE_SLUG, example_proposal, example_request, seed_example
from requivo.web.viewmodels.labels import EXAMPLE_BADGE
from tests.web.conftest import full_model


@pytest.fixture(autouse=True)
def no_provider_may_be_reached(monkeypatch):
    """Must fire. `DiscoveryService._need_provider` is the single door onto every paid call, so a
    seeding path that reasons anything at all dies here rather than passing quietly.

    Autouse, and not scoped to one test: the point is that *nothing* in this module reaches a
    provider, including the page renders after the seed.
    """
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
    svc = SessionService()
    svc.create_session("A leave approval request", slug=slug)
    svc.update_model(slug, json.dumps({
        "model": full_model(), "questions": [], "summary": {"objective": "Leave approval"}}))
    return slug


# ── the affordance is on the page a keyless visitor lands on ──────────────────

def test_a_keyless_empty_workspace_offers_a_route_into_the_example(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Set ANTHROPIC_API_KEY" in r.text          # must fire: this really is the keyless state
    assert 'action="/sessions/example"' in r.text
    assert "Nothing here yet" in r.text               # …and the empty list still says so


def test_the_example_stays_reachable_once_a_real_session_exists(client):
    """The issue proposed showing this only on an empty workspace. That is one real session away
    from an activation path nobody can reach again — including the reader who wants to compare
    their own half-finished session against a worked one."""
    _ordinary()
    r = client.get("/")
    assert r.status_code == 200
    assert "a-real-request" in r.text                 # must fire: the real session is listed
    assert 'action="/sessions/example"' in r.text


def test_seeding_is_refused_without_the_cross_site_token(raw_client):
    """Same guard as every other POST in this app. Seeding writes to the reader's workspace, so it
    is not exempt for being free."""
    r = raw_client.post("/sessions/example", follow_redirects=False)
    assert r.status_code == 403, r.text[:200]
    assert not list_session_slugs()


# ── what one click produces ───────────────────────────────────────────────────

def test_one_click_yields_a_browsable_session_with_no_key_and_no_call(client):
    slug = _seed(client)
    r = client.get(f"/sessions/{slug}")
    assert r.status_code == 200, r.text[:400]
    # the understanding, the questions and the readiness verdict — all rendered from the bundled
    # model, none of them reasoned in this process
    assert "door staff" in r.text
    assert "What could change the solution" in r.text
    assert "Are we ready?" in r.text


def test_the_example_is_created_through_the_validated_path(client):
    """Not a hand-written directory (the issue's own constraint): `create_session` +
    `update_model`, so the session carries a revision, a frozen copy of the model it applied, and
    a readiness verdict computed the same way every other session's is."""
    slug = _seed(client)
    svc = SessionService()
    meta = svc.meta(slug)
    assert meta.current_revision == 1
    assert (canonical_dir(slug) / "revisions" / "0001-model.json").exists()
    status = svc.status(slug)
    assert status["questions"]                       # the bundled questions survived the apply
    assert status["readiness"]["ready"] in (True, False)


def test_the_revision_claims_no_provider_it_did_not_use(client):
    """Invariant 6 — provenance is real or absent. Nothing reasoned this revision, so it names the
    surface that applied it and leaves `provider`/`model_name` empty rather than inheriting the
    provider that produced the payload months ago."""
    slug = _seed(client)
    record = SessionService().meta(slug).revisions[-1]
    assert record.surface == "web-example"
    assert record.provider is None
    assert record.model_name is None


# ── the second click ──────────────────────────────────────────────────────────

def test_a_second_click_returns_to_the_same_session_rather_than_making_another(client):
    """`create_session` is an atomic claim on a slug (invariant 11) and idempotent on identity, so
    navigating (not refusing) is the honest shape -- the reader clicked a button labelled as an
    example, and being told off for clicking it twice teaches nothing. The revision must not move
    either: re-applying an identical model would mint a new one with no reason, provenance
    describing an event that did not happen."""
    first = _seed(client)
    revision = SessionService().meta(first).current_revision
    second = _seed(client)
    assert second == first
    assert list_session_slugs() == [first]
    assert SessionService().meta(first).current_revision == revision


# ── it says what it is, wherever it appears ───────────────────────────────────

def test_the_example_names_itself_in_the_listing_beside_a_real_session(client):
    """The issue's own acceptance criterion, and the control is the point: a badge every row
    carries names nothing."""
    ordinary = _ordinary()
    slug = _seed(client)

    from requivo.web.viewmodels.sessions import session_list
    rows = {r["slug"]: r for r in session_list(SessionService())}
    assert rows[slug]["is_example"] is True
    assert rows[ordinary]["is_example"] is False     # must fire

    r = client.get("/")
    assert EXAMPLE_BADGE in r.text


def test_the_example_says_it_is_one_on_its_own_page(client):
    slug = _seed(client)
    r = client.get(f"/sessions/{slug}")
    assert EXAMPLE_BADGE in r.text
    assert "bundled" in r.text.lower()

    ordinary = _ordinary()
    r = client.get(f"/sessions/{ordinary}")
    assert EXAMPLE_BADGE not in r.text               # must fire


def test_the_example_page_says_what_a_keyless_reader_can_and_cannot_do(client):
    """The seeded session is a real, writable session in the reader's own workspace, so the
    refinement box and the generate buttons offer themselves exactly as they do anywhere else —
    and without a key the paid half of that cannot run. The page says so rather than letting the
    reader find out by pressing something."""
    slug = _seed(client)
    r = client.get(f"/sessions/{slug}")
    assert "no API key" in r.text


# ── the recognition rule itself ───────────────────────────────────────────────

def test_the_example_is_recognised_by_what_it_asks_not_by_the_name_it_landed_under(client):
    """`is_example` compares the request text against the bundled payload rather than testing the
    slug, so a workspace that already holds a session called `example-event-check-in` cannot make
    the badge lie in either direction — the sample lands under a derived name and is still
    labelled, and the squatter is not."""
    svc = SessionService()
    svc.create_session("Something else entirely", slug=EXAMPLE_SLUG)

    slug = _seed(client)
    assert slug != EXAMPLE_SLUG

    from requivo.web.viewmodels.sessions import session_list
    rows = {r["slug"]: r for r in session_list(SessionService())}
    assert rows[slug]["is_example"] is True
    assert rows[EXAMPLE_SLUG]["is_example"] is False


def test_the_bundled_payload_is_read_rather_than_restated():
    """The request the session captures is the client email itself, not the markdown wrapper the
    CLI demo narrates around it — and the model is the packaged one, not a copy kept here."""
    request = example_request()
    assert request.startswith("Look, the whole event thing is chaos")
    assert "# Request" not in request
    assert ">" not in request
    proposal = example_proposal()
    assert set(proposal) >= {"model", "questions", "summary"}


def test_seeding_without_a_running_server_needs_only_the_service(client):
    """`seed_example` is the whole operation; the route is a redirect around it. Called directly it
    must work on a bare `SessionService`, which is what makes it testable at all and what would
    let a second surface reuse it."""
    slug = seed_example(SessionService())
    assert SessionService().meta(slug).current_revision == 1


# ── #429 -- the click delivers the decision brief too, not just the understanding ──────

def test_one_click_also_seeds_the_decision_brief_no_key_needed(client):
    """README.md's own promise: 'the understanding, the open questions, the readiness verdict and
    the decision brief, all read from the payload bundled with the install.' Reaching the brief
    route must not need a key or a call -- the autouse fixture above already refuses one."""
    slug = _seed(client)
    r = client.get(f"/sessions/{slug}/artifacts/brief")
    assert r.status_code == 200, r.text[:400]
    assert "Decision Brief" in r.text
    assert "One word, two different problems" in r.text  # a real challenge, not a placeholder


def test_the_seeded_brief_is_listed_as_up_to_date_on_the_session_page(client):
    """"Nothing generated yet" still shows for the *other* documents (PRD, criteria, ...) -- only
    the brief is seeded, matching the audit's own scope. The primary slot itself must show a real
    artifact row rather than the "nothing to review yet" placeholder -- checked without assuming a
    key is configured, since this whole path is meant to work without one."""
    slug = _seed(client)
    r = client.get(f"/sessions/{slug}")
    assert "Decision brief" in r.text
    assert "Up to date" in r.text
    assert "The decision brief is what you take into a scope review" not in r.text


def test_a_second_click_does_not_reseed_or_duplicate_the_brief(client, monkeypatch):
    """Mirrors `test_a_second_click_returns_to_the_same_session_rather_than_making_another` for
    the model: idempotent on identity, not a fresh write every time. Asserted by counting
    `ArtifactService.save` calls rather than comparing `.list()` before/after (#428):
    `updated_at` truncates to whole seconds, so two saves inside one wall-clock second are
    indistinguishable through `.list()` even with the gate deleted -- not a test of the gate."""
    from requivo.services.artifacts import ArtifactService

    calls = []
    real_save = ArtifactService.save

    def _counting_save(self, *args, **kwargs):
        calls.append((args, kwargs))
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(ArtifactService, "save", _counting_save)

    first = _seed(client)
    second = _seed(client)
    assert second == first
    brief_calls = [c for c in calls if c[0][:2] == (first, "brief")]
    assert len(brief_calls) == 1, (
        f"expected exactly one ArtifactService.save('brief', ...) across two clicks, got "
        f"{len(brief_calls)}")


def test_a_readers_own_saved_brief_is_never_overwritten_by_a_later_click(client):
    """A brief already recorded against this session -- generated for real, with the reader's own
    key, however it got there -- must not be silently replaced by the bundled one on a later click
    of the same button. Saved directly through the service rather than a real generation, so this
    stays inside the module's no-provider guard above."""
    from requivo.services.artifacts import ArtifactService

    slug = _seed(client)
    written = "# Decision Brief\n\nSomething the reader actually generated.\n"
    ArtifactService().save(slug, "brief", written, source_revision=1)

    r = client.post("/sessions/example", follow_redirects=False)
    assert r.status_code == 303
    assert ArtifactService().show(slug, "brief") == written


def test_the_bundled_brief_is_read_rather_than_restated():
    """Sibling of `test_the_bundled_payload_is_read_rather_than_restated`: the brief content ships
    in the wheel, it is not assembled at seed time."""
    from requivo.web.example import example_brief

    brief = example_brief()
    assert brief.startswith("# Decision Brief")
    assert "One word, two different problems" in brief


def test_seeding_the_brief_holds_the_lock_across_the_check_and_the_save():
    """#428 review finding: a plain check-then-act (`list()` then `save()`, no lock spanning
    both) leaves a window where a reader's real generation could be silently overwritten by this
    call's unconditional save -- contradicting `seed_example`'s own docstring. Proven against
    `Store.session_lock`'s re-entrancy bookkeeping (`_held_locks`) rather than racing real threads,
    since the store's lock is a real OS-level file lock: checks the mechanism directly."""
    from requivo.core.persistence import _held_locks
    from requivo.services.artifacts import ArtifactService
    from requivo.services.sessions import SessionService

    sessions = SessionService()
    seen = {}
    real_list = ArtifactService.list

    def _spying_list(self, slug):
        store = sessions.repo._resolve_store()
        key = store._lock_key(slug)
        seen["depth_during_list"] = getattr(_held_locks, "depths", {}).get(key, 0)
        return real_list(self, slug)

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(ArtifactService, "list", _spying_list)
        seed_example(sessions)   # the first click -- the only one that reaches this gate

    assert "depth_during_list" in seen, "ArtifactService.list was never called -- gate not reached"
    assert seen["depth_during_list"] > 0, (
        "artifacts.list() ran with the lock NOT held -- the check-then-act race is back")
