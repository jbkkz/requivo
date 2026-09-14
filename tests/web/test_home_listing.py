"""The home page's "Recent" list: what order it is in, and what vocabulary it states times (#237).

Its own file rather than a section of `test_web_routing.py`, on the same argument #142 made about the
original split: this is one subject with a must-fire control of its own, and among fifteen tests about
routes an ordering assertion that stops being collected looks exactly like one that passes.

Two claims, and they fail in opposite directions:

* **order** — the heading says *Recent*, so the row touched five minutes ago has to lead. It came off
  `SessionService.list_entries()`, which sorts by slug and is right to: `requivo session list` is a
  public surface whose order other callers read. Ordering for a screen is presentation, so it belongs
  to the view model, and the CLI's order is asserted here as unchanged rather than assumed.
* **vocabulary** — an ISO-8601 instant is machine vocabulary on the one screen whose whole doctrine is
  translating machine vocabulary away. The exact value is kept in a `title`, because hiding is
  presentational: nothing is dropped.

Offline, isolated workspace per test; the fixtures live in `tests/web/conftest.py`.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from requivo.core.persistence import canonical_dir
from requivo.services.sessions import SessionService
from requivo.web.viewmodels.labels import human_time
from requivo.web.viewmodels.sessions import session_list
from tests.web.conftest import full_model

# An ISO-8601 instant as the store writes it. What must not survive into the rendered list.
_MACHINE_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

_LIST_OPENS = "<ul class="


def _seed(slug: str, *, updated_at: str | None = None, analysed: bool = True) -> str:
    """A session, offline, with its `updated_at` set to a chosen instant.

    Rewriting `session.json` rather than mocking a clock: the ordering is a property of what is on
    disk, and a patched `_now` would let this agree with a view model that reads something else.
    """
    svc = SessionService()
    svc.create_session(f"A request about {slug}", slug=slug)
    if analysed:
        svc.update_model(slug, json.dumps({
            "model": full_model(), "questions": [],
            "summary": {"objective": f"Objective for {slug}"}}))
    if updated_at is not None:
        p = canonical_dir(slug) / "session.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["updated_at"] = updated_at
        p.write_text(json.dumps(data), encoding="utf-8")
    return slug


def _break_model(slug: str) -> str:
    """An unreadable `model.json`, so the row degrades and states no timestamp (invariant 15).

    A real on-disk state rather than a monkeypatched raise, for the reason `test_degraded_listing`
    gives at length: what is being asserted is which layer the failure comes out of, and a patched
    method would let this agree with an implementation that guards the wrong one.
    """
    (canonical_dir(slug) / "model.json").write_text("not a model at all", encoding="utf-8")
    return slug


def _visible_text(html: str) -> str:
    """What a reader sees: markup stripped, so an ISO value parked in a `title` is not mistaken for
    one printed on the page. Crude on purpose — it only has to tell an attribute from a text node."""
    return re.sub(r"<[^>]*>", " ", html)


# ── order ─────────────────────────────────────────────────────────────────────

def test_the_recent_list_leads_with_the_session_that_moved_last():
    """"Recent" has to mean recent (#237). The slugs are chosen so alphabetical order is the
    exact reverse of recency: sorted by slug the abandoned experiment leads and the recently
    touched session sits last -- the opposite of what a returning reader should meet. Asserted as
    the whole order, not just the first row, since moving one row to the top would satisfy a
    weaker check."""
    _seed("aaa-oldest", updated_at="2026-01-01T00:00:00Z")
    _seed("mmm-newest", updated_at="2026-08-25T12:36:48Z")
    _seed("zzz-middle", updated_at="2026-05-05T05:05:05Z")

    rows = session_list(SessionService())

    assert [r["slug"] for r in rows] == ["mmm-newest", "zzz-middle", "aaa-oldest"]


def test_a_row_nobody_could_read_sorts_last_rather_than_first():
    """The third state does not get to lead the page. An unreadable row states no timestamp at
    all -- `updated_at` is empty, deliberately, since we did not read a time and must not invent
    one (invariant 15). An empty string is also the smallest string, so a naive ascending sort
    would put every broken session at the top of the one screen a reader resumes from; it goes
    last instead, and keeps its badge.
    """
    _seed("healthy-newer", updated_at="2026-08-25T12:36:48Z")
    _seed("healthy-older", updated_at="2026-01-01T00:00:00Z")
    _break_model(_seed("cannot-read", updated_at="2026-12-31T23:59:59Z"))

    rows = session_list(SessionService())

    assert [r["slug"] for r in rows] == ["healthy-newer", "healthy-older", "cannot-read"]
    # Must fire: the healthy rows are the control, and the broken one really is broken — a fixture
    # that quietly stopped breaking anything would pass the order assertion for free.
    assert rows[-1]["state"] == "unreadable"
    assert rows[-1]["updated_at"] == ""
    assert [r["state"] for r in rows[:2]] == ["ready", "ready"]


def test_two_sessions_touched_at_the_same_instant_stay_in_a_stable_order():
    """Second-precision timestamps collide, and a listing that reshuffles between reloads is its own
    small betrayal of trust. Equal instants fall back to slug order."""
    same = "2026-08-25T12:36:48Z"
    _seed("bbb", updated_at=same)
    _seed("aaa", updated_at=same)
    _seed("ccc", updated_at=same)

    assert [r["slug"] for r in session_list(SessionService())] == ["aaa", "bbb", "ccc"]


def test_the_cli_listing_order_is_not_what_changed():
    """`requivo session list` is a public surface and its slug order stays (#237, out of scope).

    Sorting for a screen is presentation and belongs to the view model; changing the *service* would
    reorder a CLI output other callers read. This is the assertion that says which of the two moved.
    """
    _seed("zzz-newest", updated_at="2026-08-25T12:36:48Z")
    _seed("aaa-oldest", updated_at="2026-01-01T00:00:00Z")

    entries = SessionService().list_entries()

    assert [e.slug for e in entries] == ["aaa-oldest", "zzz-newest"]


# ── vocabulary ────────────────────────────────────────────────────────────────

def test_the_home_list_states_no_machine_timestamp(client):
    """The rendered list carries no ISO-8601 instant, and the exact value is still one hover away.

    Both halves matter. Formatting that dropped the precise value would be hiding rather than
    translating, which the Two vocabularies rule forbids: the underlying values stay available, only
    the caption changes.
    """
    _seed("leave-approval", updated_at="2026-08-25T12:36:48Z")

    body = client.get("/").text
    listing = body[body.index(_LIST_OPENS):]

    assert not _MACHINE_TIMESTAMP.search(_visible_text(listing)), (
        "a raw ISO-8601 instant reached the reader on the one screen whose doctrine is translating "
        "machine vocabulary away")
    assert "2026-08-25T12:36:48Z" in listing, (
        "the exact instant has to survive in a title attribute — translating is not discarding")


def test_a_recent_session_reads_as_recent_rather_than_as_a_date(client):
    """Must fire for the filter itself. "no ISO in the listing" is also true of a page that prints no
    time at all, so something readable has to be asserted present."""
    minutes_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=7)
    _seed("just-now", updated_at=minutes_ago.replace(microsecond=0).isoformat().replace("+00:00", "Z"))

    assert "7 minutes ago" in client.get("/").text


# ── the filter's own three states ─────────────────────────────────────────────

def test_human_time_translates_a_stamp_it_can_read():
    now = "2026-08-25T12:00:00Z"
    assert human_time("2026-08-25T11:59:30Z", now=now) == "just now"
    assert human_time("2026-08-25T11:59:00Z", now=now) == "1 minute ago"
    assert human_time("2026-08-25T11:00:00Z", now=now) == "1 hour ago"
    assert human_time("2026-08-24T12:00:00Z", now=now) == "yesterday"
    assert human_time("2026-08-22T12:00:00Z", now=now) == "3 days ago"
    # Past the point where "N days ago" stops helping, the date itself is the useful answer.
    assert human_time("2026-01-02T09:30:00Z", now=now) == "2 Jan 2026"


def test_human_time_says_nothing_when_there_is_nothing_to_say():
    """An unreadable row carries an empty string on purpose. The filter must not turn that absence
    into a plausible time — the quiet-wrong-answer form of the very bug invariant 15 exists for."""
    assert human_time("") == ""
    assert human_time(None) == ""


def test_human_time_hands_back_a_stamp_it_could_not_read_rather_than_hiding_it():
    """The third state, and the one a formatter usually gets wrong in both directions at once.
    A hand-edited timestamp, or one written by a newer Requivo in a format this build cannot
    parse, is a fact the row does have -- inventing a time would lie, swallowing it would delete
    the only evidence something is odd. So it passes through unchanged, visibly not a date."""
    assert human_time("not-a-timestamp") == "not-a-timestamp"
    assert human_time("2026-13-45T99:99:99Z") == "2026-13-45T99:99:99Z"
