"""Invariant 15 — a listing survives its own members (#7), speaks the product's language (#240), and the home
page's "Recent" list is in the order and the vocabulary a reader expects (#237)."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re

import pytest

from requivo.core.errors import InvalidSessionError
from requivo.core.persistence import SESSION_FORMAT_VERSION, canonical_dir, session_root
from requivo.services.sessions import SessionService
from requivo.web.viewmodels.labels import UNREADABLE_HINT, human_time
from requivo.web.viewmodels.sessions import session_list
from tests.web.conftest import seed_row

HEALTHY_ANALYSED = "healthy-analysed"
HEALTHY_AWAITING = "healthy-awaiting"
BROKEN_META = "broken-meta"
BROKEN_REQUEST = "broken-request"
BROKEN_MODEL = "broken-model"


# ── the three break modes: each a real on-disk state a user can reach, not a monkeypatched raise ──


def break_meta(slug: str) -> None:
    """A session written by a newer Requivo."""
    p = canonical_dir(slug) / "session.json"
    p.write_text(p.read_text(encoding="utf-8").replace(
        f'"format_version": {SESSION_FORMAT_VERSION}', f'"format_version": {SESSION_FORMAT_VERSION + 1}'),
        encoding="utf-8")


def break_request(slug: str) -> None:
    """`request.md` replaced by a directory, so `request_text` cannot read it."""
    p = canonical_dir(slug) / "request.md"
    p.unlink()
    p.mkdir()


def break_model(slug: str) -> None:
    """A crash mid-write leaves `model.json` truncated -- the sharpest of the three."""
    (canonical_dir(slug) / "model.json").write_text('{"summary": {"objec', encoding="utf-8")


BREAKERS = {BROKEN_META: break_meta, BROKEN_REQUEST: break_request, BROKEN_MODEL: break_model}


def _broken(slug: str) -> str:
    seed_row(slug)
    BREAKERS[slug](slug)
    return slug


def _row(slug: str) -> dict:
    """One home-page row, as the view model builds it."""
    return next(r for r in session_list(SessionService()) if r["slug"] == slug)


@pytest.fixture
def mixed_workspace():
    """Two healthy sessions and three broken ones, each broken a different way."""
    seed_row(HEALTHY_ANALYSED)
    seed_row(HEALTHY_AWAITING, analysed=False)
    for slug in BREAKERS:
        _broken(slug)
    return SessionService()


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_each_breaker_defeats_the_strict_read(slug):
    """Must fire. `list_sessions()` is the strict read and is *supposed* to raise here."""
    _broken(slug)
    svc = SessionService()
    with pytest.raises(Exception) as ei:      # noqa: PT011 - the type is the platform's, not ours
        svc.list_sessions()
        svc.request_text(slug)
        svc.status(slug)
    assert ei.value is not None


# ── the service: the source of the rows degrades per member ───────────────────


def test_list_entries_degrades_only_the_unreadable_member(mixed_workspace):
    """Every slug on disk gets an entry: a listing that *drops* the broken member is the same absence, quieter."""
    entries = {e.slug: e for e in mixed_workspace.list_entries()}
    assert set(entries) == set(BREAKERS) | {HEALTHY_ANALYSED, HEALTHY_AWAITING}
    for slug in (HEALTHY_ANALYSED, HEALTHY_AWAITING):
        assert entries[slug].readable and entries[slug].meta is not None      # must fire
    assert not entries[BROKEN_META].readable and entries[BROKEN_META].meta is None
    assert entries[BROKEN_META].error and entries[BROKEN_META].slug == BROKEN_META
    assert "format" in entries[BROKEN_META].error.lower()   # the reason is kept: *newer format*
    with pytest.raises(InvalidSessionError):
        SessionService().meta(BROKEN_META)                  # must fire: the strict read still refuses
    # A member broken *below* the metadata is still readable at this layer.
    assert entries[BROKEN_REQUEST].readable and entries[BROKEN_MODEL].readable


def test_a_healthy_workspace_has_no_degraded_entries():
    """The clean-path control at the service layer."""
    seed_row(HEALTHY_ANALYSED)
    seed_row(HEALTHY_AWAITING, analysed=False)
    entries = SessionService().list_entries()
    assert len(entries) == 2 and all(e.readable and e.error is None for e in entries)


# ── the home page: one bad session cannot take the list down ──────────────────


def test_the_home_page_renders_every_row_when_three_are_broken(client, mixed_workspace):
    r = client.get("/")
    assert r.status_code == 200, r.text[:400]
    # must fire — the healthy rows are *fully* rendered, not degraded alongside the broken ones
    assert f"/sessions/{HEALTHY_ANALYSED}" in r.text and f"A request about {HEALTHY_ANALYSED}" in r.text
    assert "Awaiting analysis" in r.text                  # the revision-0 row kept its own state
    for slug in BREAKERS:
        assert slug in r.text, f"{slug} vanished from the listing"


def test_a_degraded_row_is_not_dressed_as_an_ordinary_state_and_states_no_facts_it_could_not_read(client):
    """`unreadable` is not `awaiting`; a row nobody could read claims no timestamp, count or freshness verdict."""
    seed_row(HEALTHY_AWAITING, analysed=False)
    _broken(BROKEN_MODEL)
    _broken(BROKEN_META)
    rows = {r["slug"]: r for r in session_list(SessionService())}
    assert rows[HEALTHY_AWAITING]["state"] == "awaiting"          # must fire
    assert rows[BROKEN_MODEL]["state"] == "unreadable" and rows[BROKEN_MODEL]["error"]
    assert rows[BROKEN_META]["updated_at"] == ""            # not a fabricated timestamp
    assert rows[BROKEN_META]["open_questions"] is None      # not 0 — we did not count, we could not
    assert rows[BROKEN_META]["needs_update"] is False       # no artifact claim either way


def test_an_entry_that_could_not_be_examined_is_a_row_and_not_a_broken_page(client, request):
    """The fourth break mode, one layer below the other three: an entry whose examination raises (#80)."""
    seed_row(HEALTHY_ANALYSED)
    d = session_root() / "blocked-entry"
    d.mkdir()
    request.addfinalizer(lambda: d.chmod(0o755))
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: that the home page "
                    "survives an entry whose examination raises; tests/test_persistence_scan.py skips the same.")
    d.chmod(0o000)
    try:
        (d / "session.json").stat()
    except PermissionError:
        pass
    else:
        pytest.skip("chmod 000 did not deny the probe on this run (running as root?). UNTESTED HERE: "
                    "the home page's fourth break mode.")
    r = client.get("/")
    assert r.status_code == 200
    assert f"A request about {HEALTHY_ANALYSED}" in r.text   # must fire: the healthy row is intact
    assert "blocked-entry" in r.text and "could not be read" in r.text.lower()


# ── #240: the third state speaks the product's language, and loses nothing ────

# Every token here occurs in `str(exc)` for at least one break mode; the control below keeps that honest.
_ENGINE_INTERNALS = ("Errno", "ValidationError", "pydantic")


def _leaks(slug: str) -> bool:
    raw = _row(slug)["error"]
    return any(t in raw for t in _ENGINE_INTERNALS) or str(canonical_dir(slug)) in raw


def test_the_leak_this_section_checks_for_is_reachable_at_all():
    """Must fire: the control the first draft of this section lacked."""
    for slug in sorted(BREAKERS):
        _broken(slug)
    assert [slug for slug in sorted(BREAKERS) if _leaks(slug)], (
        "no break mode produces text carrying an engine token or an absolute path, so nothing in "
        "this section can fail: " + repr({s: _row(s)["error"] for s in sorted(BREAKERS)}))


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_a_degraded_row_shows_one_human_line_and_no_engine_internals(client, slug):
    """One break mode at a time: the healthy row is intact, the broken one names itself, in one human line (#7, #240)."""
    seed_row(HEALTHY_ANALYSED)
    _broken(slug)
    r = client.get("/")
    assert r.status_code == 200, f"{slug} took the whole page down: {r.text[:400]}"
    assert f"A request about {HEALTHY_ANALYSED}" in r.text   # must fire: the healthy row is intact
    assert slug in r.text and "could not be read" in r.text.lower()
    for token in _ENGINE_INTERNALS:
        assert token not in r.text, f"{slug}: the home page is still printing {token}"
    assert str(canonical_dir(slug)) not in r.text            # the other half of the same leak
    hint = _row(slug)["hint"]
    assert "\n" not in hint and len(hint) <= 200, f"{slug}: the row hint is not one line — {hint!r}"
    assert hint in r.text


def test_a_failure_already_written_for_a_reader_survives_to_the_row(client):
    """The over-correction to avoid: `read_meta`'s *newer format* sentence is already for a reader (#240)."""
    _broken(BROKEN_META)
    hint = _row(BROKEN_META)["hint"]
    assert "upgrade requivo" in hint.lower() and "format" in hint.lower(), hint
    assert hint != UNREADABLE_HINT                       # must fire: not the generic sentence
    assert "upgrade requivo" in client.get("/").text.lower()
    _broken(BROKEN_MODEL)                                # …and a machine-shaped failure is still replaced
    assert _row(BROKEN_MODEL)["hint"] == UNREADABLE_HINT


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_humanising_the_row_did_not_flatten_the_third_state(client, slug):
    """Must fire, and the reason this is a separate test: the full text is still carried, only not printed."""
    seed_row(HEALTHY_ANALYSED)
    seed_row(HEALTHY_AWAITING, analysed=False)
    _broken(slug)
    rows = {r["slug"]: r for r in session_list(SessionService())}
    assert rows[slug]["state"] == "unreadable"
    assert rows[slug]["status_label"] != rows[HEALTHY_AWAITING]["status_label"]
    assert rows[slug]["status_label"] != rows[HEALTHY_ANALYSED]["status_label"]
    assert rows[slug]["error"] and rows[slug]["error"] != rows[slug]["hint"]
    assert "Could not be read" in client.get("/").text


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_opening_an_unreadable_session_answers_with_the_status_it_always_did(client, caplog, slug):
    """409 for a newer format, 500 for a store failure; the page names the session and the remedy, the log the cause."""
    _broken(slug)
    with caplog.at_level(logging.ERROR, logger="requivo.web"):
        r = client.get(f"/sessions/{slug}")
    assert r.status_code == (409 if slug == BROKEN_META else 500)
    assert slug in r.text and "could not be read" in r.text.lower()
    assert "session verify" in r.text                      # the remedy the CLI already names
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(slug in m and "could not be read" in m for m in messages), messages
    assert any(_row(slug)["error"] in m for m in messages), messages


def test_a_healthy_session_page_is_untouched(client):
    """The must-fire control for the whole section."""
    seed_row(HEALTHY_ANALYSED)
    r = client.get(f"/sessions/{HEALTHY_ANALYSED}")
    assert r.status_code == 200 and "could not be read" not in r.text.lower()
    assert "What Requivo understood" in r.text


# ── the "Recent" list: order (#237) ───────────────────────────────────────────


def test_the_recent_list_leads_with_the_session_that_moved_last():
    """"Recent" has to mean recent (#237); alphabetical order is chosen as the exact reverse of recency."""
    seed_row("aaa-oldest", updated_at="2026-01-01T00:00:00Z")
    seed_row("mmm-newest", updated_at="2026-08-25T12:36:48Z")
    seed_row("zzz-middle", updated_at="2026-05-05T05:05:05Z")
    assert [r["slug"] for r in session_list(SessionService())] == ["mmm-newest", "zzz-middle", "aaa-oldest"]


def test_a_row_nobody_could_read_sorts_last_rather_than_first():
    """The third state does not get to lead the page."""
    seed_row("healthy-newer", updated_at="2026-08-25T12:36:48Z")
    seed_row("healthy-older", updated_at="2026-01-01T00:00:00Z")
    (canonical_dir(seed_row("cannot-read", updated_at="2026-12-31T23:59:59Z")) / "model.json").write_text(
        "not a model at all", encoding="utf-8")
    rows = session_list(SessionService())
    assert [r["slug"] for r in rows] == ["healthy-newer", "healthy-older", "cannot-read"]
    assert rows[-1]["state"] == "unreadable" and rows[-1]["updated_at"] == ""   # must fire: really broken
    assert [r["state"] for r in rows[:2]] == ["ready", "ready"]


def test_two_sessions_touched_at_the_same_instant_stay_in_a_stable_order():
    """Second-precision timestamps collide; a listing that reshuffles between reloads betrays trust."""
    for slug in ("bbb", "aaa", "ccc"):
        seed_row(slug, updated_at="2026-08-25T12:36:48Z")
    assert [r["slug"] for r in session_list(SessionService())] == ["aaa", "bbb", "ccc"]


def test_the_cli_listing_order_is_not_what_changed():
    """`requivo session list` is a public surface and its slug order stays (#237, out of scope)."""
    seed_row("zzz-newest", updated_at="2026-08-25T12:36:48Z")
    seed_row("aaa-oldest", updated_at="2026-01-01T00:00:00Z")
    assert [e.slug for e in SessionService().list_entries()] == ["aaa-oldest", "zzz-newest"]


# ── the "Recent" list: vocabulary (#237) ──────────────────────────────────────


def test_the_home_list_states_no_machine_timestamp(client):
    """No ISO-8601 instant reaches the reader; the exact value is still one hover away."""
    seed_row("leave-approval", updated_at="2026-08-25T12:36:48Z")
    body = client.get("/").text
    listing = body[body.index("<ul class="):]
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.sub(r"<[^>]*>", " ", listing)), (
        "a raw ISO-8601 instant reached the reader on the one screen that translates machine vocabulary away")
    assert "2026-08-25T12:36:48Z" in listing, "the exact instant has to survive in a title attribute"


def test_a_recent_session_reads_as_recent_rather_than_as_a_date(client):
    """Must fire for the filter itself: something readable has to be asserted present."""
    minutes_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=7)
    seed_row("just-now", updated_at=minutes_ago.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    assert "7 minutes ago" in client.get("/").text


@pytest.mark.parametrize("stamp, expected", [
    ("2026-08-25T11:59:30Z", "just now"), ("2026-08-25T11:59:00Z", "1 minute ago"),
    ("2026-08-25T11:00:00Z", "1 hour ago"), ("2026-08-24T12:00:00Z", "yesterday"),
    ("2026-08-22T12:00:00Z", "3 days ago"), ("2026-01-02T09:30:00Z", "2 Jan 2026"),
])
def test_human_time_translates_a_stamp_it_can_read(stamp, expected):
    assert human_time(stamp, now="2026-08-25T12:00:00Z") == expected


def test_human_time_hands_back_a_stamp_it_could_not_read_rather_than_hiding_it():
    """The filter's other two states: nothing to say for an unreadable row, and a stamp it could not parse."""
    assert human_time("") == "" and human_time(None) == ""
    assert human_time("not-a-timestamp") == "not-a-timestamp"
    assert human_time("2026-13-45T99:99:99Z") == "2026-13-45T99:99:99Z"
