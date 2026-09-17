"""Invariant 15 — a listing survives its own members (#7)."""

from __future__ import annotations

import json

import pytest

from requivo.core.errors import InvalidSessionError
from requivo.core.persistence import SESSION_FORMAT_VERSION, canonical_dir
from requivo.services.sessions import SessionService
from requivo.web.viewmodels.labels import UNREADABLE_HINT
from tests.web.conftest import full_model

HEALTHY_ANALYSED = "healthy-analysed"
HEALTHY_AWAITING = "healthy-awaiting"
BROKEN_META = "broken-meta"
BROKEN_REQUEST = "broken-request"
BROKEN_MODEL = "broken-model"


def _seed(slug: str, *, analysed: bool = True) -> str:
    """A session, offline. `analysed` decides whether it has a model."""
    svc = SessionService()
    svc.create_session(f"A request about {slug}", slug=slug)
    if analysed:
        svc.update_model(slug, json.dumps({
            "model": full_model(), "questions": [],
            "summary": {"objective": f"Objective for {slug}"}}))
    return slug


# ── the three break modes ─────────────────────────────────────────────────────
# Each is a real on-disk state a user can reach, not a monkeypatched raise.

def break_meta(slug: str) -> None:
    """A session written by a newer Requivo."""
    p = canonical_dir(slug) / "session.json"
    # Explicit encoding on both halves.
    data = json.loads(p.read_text(encoding="utf-8"))
    data["format_version"] = SESSION_FORMAT_VERSION + 1
    p.write_text(json.dumps(data), encoding="utf-8")


def break_request(slug: str) -> None:
    """`request.md` replaced by a directory, so `request_text` cannot read it."""
    p = canonical_dir(slug) / "request.md"
    p.unlink()
    p.mkdir()


def break_model(slug: str) -> None:
    """A crash mid-write leaves `model.json` truncated -- scenario B, the sharpest of three."""
    (canonical_dir(slug) / "model.json").write_text('{"summary": {"objec', encoding="utf-8")


BREAKERS = {BROKEN_META: break_meta, BROKEN_REQUEST: break_request, BROKEN_MODEL: break_model}


@pytest.fixture
def mixed_workspace():
    """Two healthy sessions and three broken ones, each broken a different way."""
    _seed(HEALTHY_ANALYSED, analysed=True)
    _seed(HEALTHY_AWAITING, analysed=False)
    for slug, breaker in BREAKERS.items():
        _seed(slug, analysed=True)
        breaker(slug)
    return SessionService()


# ── the breakers really break something ───────────────────────────────────────

@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_each_breaker_defeats_the_strict_read(slug):
    """Must fire. `list_sessions()` is the strict read and is *supposed* to raise here."""
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)
    svc = SessionService()
    with pytest.raises(Exception) as ei:      # noqa: PT011 - the type is the platform's, not ours
        svc.list_sessions()
        svc.request_text(slug)
        svc.status(slug)
    assert ei.value is not None


# ── the service: the source of the rows degrades per member ───────────────────

def test_list_entries_degrades_only_the_unreadable_member(mixed_workspace):
    svc = mixed_workspace
    entries = {e.slug: e for e in svc.list_entries()}

    # must fire: the healthy pair is readable and carries real metadata
    assert entries[HEALTHY_ANALYSED].readable and entries[HEALTHY_ANALYSED].meta is not None
    assert entries[HEALTHY_AWAITING].readable and entries[HEALTHY_AWAITING].meta is not None

    # the member whose own metadata will not load is reported, not raised
    assert not entries[BROKEN_META].readable
    assert entries[BROKEN_META].meta is None
    assert entries[BROKEN_META].error                      # the reason is stated, not dropped
    assert entries[BROKEN_META].slug == BROKEN_META        # …and it names which session

    # a member broken *below* the metadata is still readable at this layer.
    assert entries[BROKEN_REQUEST].readable
    assert entries[BROKEN_MODEL].readable


def test_list_entries_is_a_complete_census(mixed_workspace):
    """Every slug on disk gets an entry. A degrading listing that *drops* the broken member is the same
    absence one step quieter: the reader is told nothing is wrong, and the session is gone."""
    assert {e.slug for e in mixed_workspace.list_entries()} == set(BREAKERS) | {
        HEALTHY_ANALYSED, HEALTHY_AWAITING}


def test_a_healthy_workspace_has_no_degraded_entries():
    """The clean-path control at the service layer: no session is reported unreadable when none is."""
    _seed(HEALTHY_ANALYSED)
    _seed(HEALTHY_AWAITING, analysed=False)
    entries = SessionService().list_entries()
    assert len(entries) == 2
    assert all(e.readable and e.error is None for e in entries)


# ── the home page: one bad session cannot take the list down ──────────────────

def test_the_home_page_renders_every_row_when_three_are_broken(client, mixed_workspace):
    r = client.get("/")
    assert r.status_code == 200, r.text[:400]

    # must fire — the healthy rows are *fully* rendered, not degraded alongside the broken ones
    assert f"/sessions/{HEALTHY_ANALYSED}" in r.text
    assert f"A request about {HEALTHY_ANALYSED}" in r.text
    assert "Awaiting analysis" in r.text                  # the revision-0 row kept its own state

    # every broken session is on the page, named
    for slug in BREAKERS:
        assert slug in r.text, f"{slug} vanished from the listing"


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_one_break_mode_at_a_time_still_leaves_the_healthy_row(client, slug):
    """The test that separates a real fix from a partial one."""
    _seed(HEALTHY_ANALYSED, analysed=True)
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)

    r = client.get("/")
    assert r.status_code == 200, f"{slug} took the whole page down: {r.text[:400]}"
    assert f"A request about {HEALTHY_ANALYSED}" in r.text   # must fire: the healthy row is intact
    assert slug in r.text                                     # …and the broken one names itself


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_a_degraded_row_says_it_could_not_be_read_and_names_the_slug(client, slug):
    """Neither surface named the failing session before this."""
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)

    r = client.get("/")
    assert r.status_code == 200
    row = [line for line in r.text.splitlines() if slug in line]
    assert row, f"{slug} is not on the page at all"
    assert "could not be read" in r.text.lower()


def test_a_degraded_row_is_not_dressed_as_an_ordinary_state(client):
    """`unreadable` must not render as `awaiting`, `ready` or `in progress`."""
    _seed(HEALTHY_AWAITING, analysed=False)
    _seed(BROKEN_MODEL, analysed=True)
    break_model(BROKEN_MODEL)

    from requivo.web.viewmodels.sessions import session_list
    rows = {r["slug"]: r for r in session_list(SessionService())}

    assert rows[HEALTHY_AWAITING]["state"] == "awaiting"          # must fire
    assert rows[BROKEN_MODEL]["state"] == "unreadable"
    assert rows[BROKEN_MODEL]["state"] != rows[HEALTHY_AWAITING]["state"]
    assert rows[BROKEN_MODEL]["error"]


def test_a_degraded_row_states_no_facts_it_could_not_read(client):
    """A row we could not read must not claim a timestamp, a question count or a freshness verdict."""
    _seed(BROKEN_META, analysed=True)
    break_meta(BROKEN_META)

    from requivo.web.viewmodels.sessions import session_list
    row = session_list(SessionService())[0]
    assert row["slug"] == BROKEN_META
    assert row["updated_at"] == ""            # not a fabricated timestamp
    assert row["open_questions"] is None      # not 0 — we did not count, we could not
    assert row["needs_update"] is False       # no artifact claim either way


def test_the_metadata_failure_is_reported_as_the_error_it_was(mixed_workspace):
    """The degraded entry keeps the reason. `InvalidSessionError` says *newer format* here."""
    entry = next(e for e in mixed_workspace.list_entries() if e.slug == BROKEN_META)
    assert "format" in entry.error.lower()
    with pytest.raises(InvalidSessionError):
        SessionService().meta(BROKEN_META)     # must fire: the strict read still refuses


# ── the fourth break mode, one layer below the other three (#80) ──────────────


def test_an_entry_that_could_not_be_examined_is_a_row_and_not_a_broken_page(client, request):
    """The three modes above break a member the listing already has (#80)."""
    import os

    _seed(HEALTHY_ANALYSED, analysed=True)
    from requivo.core.persistence import session_root
    d = session_root() / "blocked-entry"
    d.mkdir()
    request.addfinalizer(lambda: d.chmod(0o755))
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: that the home "
                    "page survives an entry whose examination raises. The CLI sibling of this case "
                    "is tests/test_persistence_scan.py, skipped there for the same reason.")
    d.chmod(0o000)
    try:
        (d / "session.json").exists()
    except PermissionError:
        pass
    else:
        pytest.skip("chmod 000 did not deny the probe on this run (running as root?). UNTESTED "
                    "HERE: the home page's fourth break mode.")

    r = client.get("/")
    assert r.status_code == 200
    assert f"A request about {HEALTHY_ANALYSED}" in r.text   # must fire: the healthy row is intact
    assert "blocked-entry" in r.text                          # …and the entry names itself
    assert "could not be read" in r.text.lower()


# ── #240: the third state speaks the product's language, and loses nothing ────
#
# The row was already correct about *what* it did not know (invariant 15, and the whole block above).
#
# The trap this section is shaped against is the obvious over-correction.

# What must never appear on the home page: machine text, and — separately asserted — the absolute path.
#
# **Every token here occurs in `str(exc)` for at least one break mode (#240).
#
# `_the_leak_is_reachable` below is the positive control that keeps this honest on every platform.
_ENGINE_INTERNALS = ("Errno", "ValidationError", "pydantic")


def _row(slug: str) -> dict:
    """One home-page row, as the view model builds it."""
    from requivo.web.viewmodels.sessions import session_list
    return next(r for r in session_list(SessionService()) if r["slug"] == slug)


def _leaks(slug: str) -> bool:
    """Whether this break mode's untruncated failure actually contains anything the page is then checked not
    to print."""
    raw = _row(slug)["error"]
    return any(t in raw for t in _ENGINE_INTERNALS) or str(canonical_dir(slug)) in raw


def test_the_leak_this_section_checks_for_is_reachable_at_all():
    """Must fire: the control the first draft of this section lacked."""
    for slug in sorted(BREAKERS):
        _seed(slug, analysed=True)
        BREAKERS[slug](slug)
    leaky = [slug for slug in sorted(BREAKERS) if _leaks(slug)]
    assert leaky, (
        "no break mode produces text carrying an engine token or an absolute path, so nothing in "
        "this section can fail: " + repr({s: _row(s)["error"] for s in sorted(BREAKERS)})
    )


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_a_degraded_row_shows_one_human_line_and_no_engine_internals(client, slug):
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)

    r = client.get("/")
    assert r.status_code == 200
    for token in _ENGINE_INTERNALS:
        assert token not in r.text, f"{slug}: the home page is still printing {token}"
    # An absolute path is the other half of the same leak, and the half that carries every arm.
    assert str(canonical_dir(slug)) not in r.text

    hint = _row(slug)["hint"]
    assert "\n" not in hint, f"{slug}: the row hint is not one line — {hint!r}"
    assert len(hint) <= 200, f"{slug}: the row hint is a paragraph, not a line — {hint!r}"
    assert hint in r.text


def test_a_failure_already_written_for_a_reader_survives_to_the_row(client):
    """The over-correction this issue had to avoid: `read_meta` refusing a newer `format_version` says
    *session format v2 is newer than this Requivo understands."""
    _seed(BROKEN_META, analysed=True)
    break_meta(BROKEN_META)

    hint = _row(BROKEN_META)["hint"]
    assert "upgrade requivo" in hint.lower(), hint      # the remedy, on the first screen
    assert "format" in hint.lower()
    assert hint != UNREADABLE_HINT                       # must fire: not the generic sentence

    r = client.get("/")
    assert "upgrade requivo" in r.text.lower()

    # …and the control, one row over: a failure that IS machine-shaped is still replaced.
    _seed(BROKEN_MODEL, analysed=True)
    break_model(BROKEN_MODEL)
    assert _row(BROKEN_MODEL)["hint"] == UNREADABLE_HINT


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_humanising_the_row_did_not_flatten_the_third_state(client, slug):
    """Must fire, and the reason this is a separate test."""
    _seed(HEALTHY_ANALYSED, analysed=True)
    _seed(HEALTHY_AWAITING, analysed=False)
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)

    from requivo.web.viewmodels.sessions import session_list
    rows = {r["slug"]: r for r in session_list(SessionService())}
    assert rows[slug]["state"] == "unreadable"
    assert rows[slug]["status_label"] != rows[HEALTHY_AWAITING]["status_label"]
    assert rows[slug]["status_label"] != rows[HEALTHY_ANALYSED]["status_label"]
    # The full text is still carried — it is simply not what the home page prints.
    assert rows[slug]["error"]
    assert rows[slug]["error"] != rows[slug]["hint"]

    r = client.get("/")
    assert "Could not be read" in r.text


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_the_full_detail_is_reachable_where_the_row_says_it_is(client, slug):
    """`home.html` has promised since #7 that "the session screen is where the full error is stated"."""
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)

    r = client.get(f"/sessions/{slug}")
    assert slug in r.text                                  # it names the session
    assert "could not be read" in r.text.lower()
    assert "session verify" in r.text                      # …and the remedy the CLI already names


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_opening_an_unreadable_session_answers_with_the_status_it_always_did(client, slug):
    """The humanised page is not a 200. `session_page` reported 409 for a session written by a newer Requivo
    and 500 for a store failure."""
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)

    expected = 409 if slug == BROKEN_META else 500
    r = client.get(f"/sessions/{slug}")
    assert r.status_code == expected


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_the_unreadable_session_page_is_logged_for_whoever_has_to_fix_it(client, caplog, slug):
    """The page is for the reader; the log is for the operator."""
    import logging

    _seed(slug, analysed=True)
    BREAKERS[slug](slug)
    with caplog.at_level(logging.ERROR, logger="requivo.web"):
        client.get(f"/sessions/{slug}")

    messages = [rec.getMessage() for rec in caplog.records]
    assert any(slug in m and "could not be read" in m for m in messages), messages
    # …and the failure itself, not only that one happened.
    detail = _row(slug)["error"]
    assert any(detail in m for m in messages), (detail, messages)


def test_a_healthy_session_page_is_untouched(client):
    """The must-fire control for the whole section: the unreadable branch must not swallow an ordinary session
    on its way past."""
    _seed(HEALTHY_ANALYSED, analysed=True)
    r = client.get(f"/sessions/{HEALTHY_ANALYSED}")
    assert r.status_code == 200
    assert "could not be read" not in r.text.lower()
    assert "What Requivo understood" in r.text
