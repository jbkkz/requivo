"""Invariant 15 on the CLI: `requivo session list` survives its own members (#62)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from _fakes import forge_meta, run_cli_exit, seed_session

from requivo.cli import EXIT_INTERRUPTED, EXIT_RENDER_FAILED
from requivo.core.persistence import SESSION_FORMAT_VERSION, canonical_dir
from requivo.deterministic import EXIT_DEGRADED
from requivo.paths import session_root
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

HEALTHY, AWAITING, BROKEN_META, BROKEN_REQUEST, BROKEN_MODEL = (
    "healthy-analysed", "healthy-awaiting", "broken-meta", "broken-request", "broken-model")


def _list(*argv):
    return run_cli_exit(["session", "list", *argv])


def _broken_meta(slug: str = BROKEN_META, **fields) -> str:
    """A session written by a newer Requivo (or carrying `fields`), which `read_meta` refuses."""
    seed_session(slug, analysed=not fields)
    forge_meta(slug, fields or {"format_version": SESSION_FORMAT_VERSION + 1})
    return slug


def break_request(slug: str) -> None:
    """`request.md` replaced by a directory: `IsADirectoryError` on POSIX, `PermissionError` on Windows."""
    p = canonical_dir(slug) / "request.md"
    p.unlink()
    p.mkdir()


def break_model(slug: str) -> None:
    (canonical_dir(slug) / "model.json").write_text('{"summary": {"objec', encoding="utf-8")


BELOW_METADATA = {BROKEN_REQUEST: break_request, BROKEN_MODEL: break_model}


def test_break_meta_defeats_the_strict_read():
    """Must fire: `list_sessions()` is the strict read and is supposed to raise here."""
    _broken_meta()
    with pytest.raises(Exception) as ei:   # noqa: PT011 - the type is persistence's, not ours
        SessionService().list_sessions()
    assert "format" in str(ei.value).lower()


def test_one_unreadable_session_no_longer_takes_the_listing_down():
    """`SessionService.list_entries` is the source of the rows; the reason is the remedy (#62)."""
    seed_session(HEALTHY)
    seed_session(AWAITING, analysed=False)
    _broken_meta()
    out, code = _list()
    assert HEALTHY in out and "rev 1" in out and AWAITING in out, out   # must fire: rendered in full
    assert BROKEN_META in out and "could not be read" in out.lower(), out
    assert "upgrade requivo" in out.lower(), out
    assert code == EXIT_DEGRADED


def test_the_degraded_row_states_no_fact_it_could_not_read():
    """No revision, no timestamp: a plausible `rev 0` on a session nobody could open is the quiet form of the bug."""
    seed_session(HEALTHY)
    _broken_meta()
    out, _ = _list()
    rows = {slug: next(ln for ln in out.splitlines() if slug in ln) for slug in (HEALTHY, BROKEN_META)}
    assert re.search(r"\brev \d", rows[HEALTHY]) and re.search(r"20\d\d-\d\d-\d\dT", rows[HEALTHY])
    assert not re.search(r"\brev \d", rows[BROKEN_META]), rows[BROKEN_META]
    assert not re.search(r"20\d\d-\d\d-\d\dT", rows[BROKEN_META]), rows[BROKEN_META]


@pytest.mark.parametrize(("seed", "expected"), [
    (None, "No sessions under"),
    ((AWAITING, False), "rev 0"),
    ((HEALTHY, True), f"  {HEALTHY:<40} rev 1"),
], ids=["empty", "revision-zero", "analysed"])
def test_a_clean_listing_is_a_clean_exit(seed, expected):
    """Empty, *not analysed yet* and analysed are all clean rows, never degraded ones."""
    if seed:
        seed_session(seed[0], analysed=seed[1])
    out, code = _list()
    assert expected in out and code == 0, out
    assert "could not be read" not in out.lower()


@pytest.mark.parametrize("slug", sorted(BELOW_METADATA))
def test_a_break_below_the_metadata_does_not_reach_this_listing(slug):
    """`request.md` and `model.json` are not read by this row, so the command is a clean success."""
    seed_session(HEALTHY)
    BELOW_METADATA[slug](seed_session(slug))
    out, code = _list()
    assert HEALTHY in out and slug in out and code == 0, out
    assert "could not be read" not in out.lower()


def test_json_keeps_every_key_on_every_row():
    """A degraded row is a complete census entry with the same key set, `null` where the fact is missing."""
    seed_session(HEALTHY)
    seed_session(AWAITING, analysed=False)
    _broken_meta()
    out, code = _list("--json")
    rows = {r["slug"]: r for r in json.loads(out)["sessions"]}
    assert code == EXIT_DEGRADED and rows.keys() == {HEALTHY, AWAITING, BROKEN_META}
    assert rows[HEALTHY].keys() == rows[BROKEN_META].keys()
    assert (rows[HEALTHY]["revision"], rows[HEALTHY]["readable"], rows[HEALTHY]["error"]) == (1, True, None)
    assert rows[HEALTHY]["updated_at"]
    assert rows[BROKEN_META]["readable"] is False
    assert (rows[BROKEN_META]["revision"], rows[BROKEN_META]["provider"], rows[BROKEN_META]["updated_at"]) == (None,) * 3
    assert "format" in rows[BROKEN_META]["error"].lower()


def test_json_is_an_object_so_it_can_ever_gain_a_top_level_field():
    """#87: `{"sessions": [...], "degraded": n, "session_root": "..."}`, and an empty workspace is the same object."""
    assert json.loads(_list("--json")[0]) == {"sessions": [], "degraded": 0, "session_root": str(session_root())}
    seed_session(HEALTHY)
    out, code = _list("--json")
    clean = json.loads(out)
    assert code == 0 and clean.keys() == {"sessions", "degraded", "session_root"}
    assert clean["degraded"] == 0 and clean["session_root"] == str(session_root())
    assert [r["slug"] for r in clean["sessions"]] == [HEALTHY]
    _broken_meta()
    out, code = _list("--json")
    degraded = json.loads(out)
    assert code == EXIT_DEGRADED and degraded["degraded"] == 1
    assert {r["slug"] for r in degraded["sessions"]} == {HEALTHY, BROKEN_META}


def test_the_three_outcomes_have_three_exit_codes():
    """`0`, `4` and `1`: listed cleanly, listed with a hole, could not read (#86)."""
    seed_session(HEALTHY)
    assert _list()[1] == 0
    _broken_meta()
    assert _list()[1] == EXIT_DEGRADED
    assert run_cli_exit(["session", "show", BROKEN_META])[1] == 1   # the strict read's ordinary clean failure


def test_the_degraded_code_collides_with_nothing():
    """The two exit-code constants live in two modules (#206)."""
    assert EXIT_DEGRADED not in {0, 1, 2, EXIT_RENDER_FAILED, EXIT_INTERRUPTED}
    assert EXIT_INTERRUPTED not in {0, 1, 2, EXIT_RENDER_FAILED}


def test_the_degraded_exit_code_is_published_as_a_value_not_as_a_name():
    """What `docs/compatibility.md` promises is the number 4, never the symbol (#145)."""
    page = (Path(__file__).resolve().parents[1] / "docs" / "compatibility.md").read_text(encoding="utf-8")
    assert EXIT_DEGRADED == 4
    assert re.search(r"^\| 4 \| ", page, re.MULTILINE), "the exit-code table no longer publishes 4"
    not_stable = page.split("## What is explicitly *not* stable")
    assert len(not_stable) == 2, "the not-stable section was renamed; the claim below cannot be checked"
    assert "`requivo.deterministic`" in not_stable[1], "`requivo.deterministic` left the not-stable list"


# ── the slug and the error are untrusted text (#40) ──────────────────────────


def test_a_slug_carrying_a_control_character_cannot_forge_a_line():
    hostile = "evil\nTOTAL: 0 sessions, nothing to see"   # no `/`: that would nest the directory
    d = session_root() / hostile
    try:
        d.mkdir(parents=True)
    except (OSError, ValueError):
        pytest.skip("this filesystem refuses a directory name containing a newline; the slug half "
                    "of the display_token guard is untested here")
    (d / "session.json").write_text("{ not json", encoding="utf-8")
    out, code = _list()
    assert code == EXIT_DEGRADED          # must fire: the fixture really is unreadable
    assert not any(line.startswith("TOTAL:") for line in out.splitlines()), out
    assert "evil" in out


@pytest.mark.parametrize("field", ("slug", "provider", "updated_at"))
def test_a_readable_row_cannot_forge_a_line_from_session_json(field):
    """Every text field the readable row prints comes out of `session.json`'s body (#40, #62)."""
    seed_session(HEALTHY)
    seed_session("tampered", analysed=False)
    forge_meta("tampered", {field: f"tampered\n  forged-row{' ' * 30} rev 999  (trusted, 2026-01-01T00:00:00Z)"})
    out, code = _list()
    assert code == 0 and re.search(rf"  {HEALTHY}\s+rev \d", out), out   # readable, and the sibling renders
    assert not any(ln.lstrip().startswith("forged-row") for ln in out.splitlines()), out


def test_json_never_lets_session_json_forge_a_line():
    """The `--json` sibling, safe for a different reason: escaped rather than dropped."""
    seed_session("tampered", analysed=False)
    forge_meta("tampered", {"provider": "x\nFORGED"})
    out, _ = _list("--json")
    assert not any(ln.startswith("FORGED") for ln in out.splitlines()), out
    assert json.loads(out)["sessions"][0]["provider"] == "x\nFORGED"


def test_a_multi_line_error_stays_one_row():
    """A pydantic `ValidationError` whose message is four lines is still one degraded row."""
    seed_session(HEALTHY)
    _broken_meta(current_revision="not an integer")
    out, code = _list()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert code == EXIT_DEGRADED
    assert sum(BROKEN_META in ln for ln in lines) == 1 and sum(HEALTHY in ln for ln in lines) == 1, out
    assert len(lines) == 4, out   # header + two rows + the footer
