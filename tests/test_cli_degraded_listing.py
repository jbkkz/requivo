"""Invariant 15 on the CLI — `requivo session list` survives its own members (#62)."""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from requivo.cli import EXIT_INTERRUPTED, EXIT_RENDER_FAILED, app
from requivo.core.persistence import SESSION_FORMAT_VERSION, canonical_dir
from requivo.deterministic import EXIT_DEGRADED
from requivo.paths import session_root
from requivo.services.sessions import SessionService

HEALTHY = "healthy-analysed"
AWAITING = "healthy-awaiting"
BROKEN_META = "broken-meta"
BROKEN_REQUEST = "broken-request"
BROKEN_MODEL = "broken-model"


def _run(argv):
    """`app()` with stdout captured, returning `(text, exit_code)`."""
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            app(argv, client=None)   # client=None → any accidental API use would blow up
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


def _seed(slug: str, *, analysed: bool = True) -> str:
    svc = SessionService()
    svc.create_session(f"A request about {slug}", slug=slug)
    if analysed:
        from _cli_harness import _full_model
        svc.update_model(slug, json.dumps(_full_model()))
    return slug


# ── the break modes, as on-disk states rather than patched raises ─────────────

def break_meta(slug: str) -> None:
    """A session written by a newer Requivo. `read_meta` refuses it."""
    p = canonical_dir(slug) / "session.json"
    # Explicit encoding on both halves: `read_text()` defaults to the locale's codec.
    data = json.loads(p.read_text(encoding="utf-8"))
    data["format_version"] = SESSION_FORMAT_VERSION + 1
    p.write_text(json.dumps(data), encoding="utf-8")


def break_request(slug: str) -> None:
    """`request.md` replaced by a directory. `IsADirectoryError` on POSIX, `PermissionError` on Windows."""
    p = canonical_dir(slug) / "request.md"
    p.unlink()
    p.mkdir()


def break_model(slug: str) -> None:
    """A crash mid-write leaves `model.json` truncated."""
    (canonical_dir(slug) / "model.json").write_text('{"summary": {"objec', encoding="utf-8")


BELOW_METADATA = {BROKEN_REQUEST: break_request, BROKEN_MODEL: break_model}


# ── the breaker really breaks something ──────────────────────────────────────

def test_break_meta_defeats_the_strict_read(workspace):
    """Must fire. `list_sessions()` is the strict read and is *supposed* to raise here."""
    _seed(BROKEN_META)
    break_meta(BROKEN_META)
    with pytest.raises(Exception) as ei:   # noqa: PT011 - the type is persistence's, not ours
        SessionService().list_sessions()
    assert "format" in str(ei.value).lower()


# ── the listing survives, and names what it could not read ───────────────────

def test_one_unreadable_session_no_longer_takes_the_listing_down(workspace):
    """`SessionService.list_entries` is the *source* of the rows, and that is where invariant 15 has to be
    enforced."""
    _seed(HEALTHY)
    _seed(AWAITING, analysed=False)
    _seed(BROKEN_META)
    break_meta(BROKEN_META)

    out, code = _run(["session", "list"])

    # must fire: the healthy rows are rendered in full, not degraded alongside the broken one
    assert HEALTHY in out, out
    assert "rev 1" in out
    assert AWAITING in out
    # …and the broken one is named rather than dropped or fatal
    assert BROKEN_META in out
    assert "could not be read" in out.lower()
    assert code == EXIT_DEGRADED


def test_the_degraded_row_carries_the_reason_because_the_reason_is_the_remedy(workspace):
    """*Written by a newer Requivo, upgrade* is a remedy; a flattened `unreadable` is not."""
    _seed(BROKEN_META)
    break_meta(BROKEN_META)
    out, _ = _run(["session", "list"])
    assert "upgrade requivo" in out.lower()


def test_the_degraded_row_states_no_fact_it_could_not_read(workspace):
    """No revision, no provider, no timestamp. A plausible `rev 0` on a session nobody could open is the
    quiet-wrong-answer form of the same bug."""
    _seed(HEALTHY)
    _seed(BROKEN_META)
    break_meta(BROKEN_META)
    out, _ = _run(["session", "list"])
    rows = {slug: next(ln for ln in out.splitlines() if slug in ln)
            for slug in (HEALTHY, BROKEN_META)}

    # must fire: the same two patterns are present on the healthy row.
    assert re.search(r"\brev \d", rows[HEALTHY])
    assert re.search(r"20\d\d-\d\d-\d\dT", rows[HEALTHY])

    # Regexes rather than substrings: the error text is free-form.
    assert not re.search(r"\brev \d", rows[BROKEN_META]), rows[BROKEN_META]
    assert not re.search(r"20\d\d-\d\d-\d\dT", rows[BROKEN_META]), rows[BROKEN_META]


def test_a_session_at_revision_zero_is_a_normal_row_not_a_degraded_one(workspace):
    """*Not analysed yet* and *we could not look* are two states."""
    _seed(AWAITING, analysed=False)
    out, code = _run(["session", "list"])
    assert "rev 0" in out
    assert "could not be read" not in out.lower()
    assert code == 0                    # must fire: a clean listing is still a clean exit


def test_a_healthy_workspace_is_unchanged(workspace):
    """The clean-path control. Nothing about the ordinary listing moves — same line, same exit."""
    _seed(HEALTHY)
    out, code = _run(["session", "list"])
    assert code == 0
    assert "could not be read" not in out.lower()
    assert f"  {HEALTHY:<40} rev 1" in out


def test_an_empty_workspace_still_says_so(workspace):
    out, code = _run(["session", "list"])
    assert "No sessions under" in out
    assert code == 0


@pytest.mark.parametrize("slug", sorted(BELOW_METADATA))
def test_a_break_below_the_metadata_does_not_reach_this_listing(workspace, slug):
    """The correction to the issue, pinned. `request.md` and `model.json` are not read by this row, so these
    two sessions list normally and the command is a clean success."""
    _seed(HEALTHY)
    _seed(slug)
    BELOW_METADATA[slug](slug)
    out, code = _run(["session", "list"])
    assert HEALTHY in out
    assert slug in out
    assert code == 0
    assert "could not be read" not in out.lower()


# ── --json: a public output, and the degraded row's shape in it ──────────────

def test_json_keeps_every_key_on_every_row(workspace):
    """The compatibility statement, asserted. A degraded row carries the *same key set* as a healthy one with
    `null` where the fact is missing."""
    _seed(HEALTHY)
    _seed(BROKEN_META)
    break_meta(BROKEN_META)

    out, code = _run(["session", "list", "--json"])
    rows = {r["slug"]: r for r in json.loads(out)["sessions"]}
    assert code == EXIT_DEGRADED
    assert rows.keys() == {HEALTHY, BROKEN_META}
    assert rows[HEALTHY].keys() == rows[BROKEN_META].keys()

    # must fire: the healthy row is unchanged in every field it always had
    assert rows[HEALTHY]["revision"] == 1
    assert rows[HEALTHY]["readable"] is True
    assert rows[HEALTHY]["error"] is None
    assert rows[HEALTHY]["updated_at"]

    # the degraded row claims nothing — null, never a plausible 0 or ""
    assert rows[BROKEN_META]["readable"] is False
    assert rows[BROKEN_META]["revision"] is None
    assert rows[BROKEN_META]["provider"] is None
    assert rows[BROKEN_META]["updated_at"] is None
    assert "format" in rows[BROKEN_META]["error"].lower()


def test_json_is_still_a_complete_census(workspace):
    """A listing that *drops* the member it could not read is the same absence one step quieter."""
    _seed(HEALTHY)
    _seed(AWAITING, analysed=False)
    _seed(BROKEN_META)
    break_meta(BROKEN_META)
    out, _ = _run(["session", "list", "--json"])
    assert {r["slug"] for r in json.loads(out)["sessions"]} == {HEALTHY, AWAITING, BROKEN_META}


def test_json_is_an_object_so_it_can_ever_gain_a_top_level_field(workspace):
    """#87. The payload is `{"sessions": [...], "degraded": n, "session_root": "..."}`."""
    _seed(HEALTHY)

    out, code = _run(["session", "list", "--json"])
    clean = json.loads(out)
    assert code == 0
    assert clean.keys() == {"sessions", "degraded", "session_root"}
    assert clean["degraded"] == 0
    assert clean["session_root"] == str(session_root())
    assert [r["slug"] for r in clean["sessions"]] == [HEALTHY]

    _seed(BROKEN_META)
    break_meta(BROKEN_META)

    out, code = _run(["session", "list", "--json"])
    degraded = json.loads(out)
    assert code == EXIT_DEGRADED
    assert degraded["degraded"] == 1                      # must fire
    assert {r["slug"] for r in degraded["sessions"]} == {HEALTHY, BROKEN_META}
    assert degraded["session_root"] == str(session_root())


def test_json_on_an_empty_workspace_is_the_same_object(workspace):
    """No sessions is not a special case and must not be a special shape."""
    out, code = _run(["session", "list", "--json"])
    empty = json.loads(out)
    assert code == 0
    assert empty == {"sessions": [], "degraded": 0, "session_root": str(session_root())}


# ── the exit code says which of the three happened ───────────────────────────

def test_the_three_outcomes_have_three_exit_codes(workspace):
    """`0`, `4` and `1` — listed cleanly, listed with a hole, could not list (#86, moved here from CLAUDE.md
    by #286)."""
    _seed(HEALTHY)
    assert _run(["session", "list"])[1] == 0

    _seed(BROKEN_META)
    break_meta(BROKEN_META)
    assert _run(["session", "list"])[1] == EXIT_DEGRADED

    # could not read this one session at all: `session show` is the strict read and is the ordinary clean failure.
    assert _run(["session", "show", BROKEN_META])[1] == 1


def test_the_degraded_code_collides_with_nothing():
    """The two exit-code constants live in two modules (#206)."""
    assert EXIT_DEGRADED not in {0, 1, 2, EXIT_RENDER_FAILED, EXIT_INTERRUPTED}
    assert EXIT_INTERRUPTED not in {0, 1, 2, EXIT_RENDER_FAILED}


def test_the_degraded_exit_code_is_published_as_a_value_not_as_a_name():
    """What `docs/compatibility.md` promises is the number 4, never the symbol (#145)."""
    page = (Path(__file__).resolve().parents[1] / "docs" / "compatibility.md").read_text(encoding="utf-8")
    assert EXIT_DEGRADED == 4
    assert re.search(r"^\| 4 \| ", page, re.MULTILINE), (
        "the exit-code table no longer publishes 4 — the promise this constant carries is that number"
    )
    not_stable = page.split("## What is explicitly *not* stable")
    assert len(not_stable) == 2, "the not-stable section was renamed; the claim below cannot be checked"
    assert "`requivo.deterministic`" in not_stable[1], (
        "`requivo.deterministic` left the not-stable list — if the module is now published, the "
        "refusal recorded in its own docstring has been reversed and has to be rewritten"
    )


# ── the slug and the error are untrusted text (#40) ──────────────────────────

def test_a_slug_carrying_a_control_character_cannot_forge_a_line(workspace):
    """A session directory is created by whoever holds the workspace (#40)."""
    # No path separator in it: a `/` would nest the directory rather than name it.
    hostile = "evil\nTOTAL: 0 sessions, nothing to see"
    d = session_root() / hostile
    try:
        d.mkdir(parents=True)
    except (OSError, ValueError):
        # NTFS refuses a control character in a filename outright, so this class is unreachable there and the skip is loud rather than a silent pass — a test that trivially passed on Windows would report coverage of the render guard that it does not have.
        pytest.skip("this filesystem refuses a directory name containing a newline; the slug half "
                    "of the display_token guard is untested here")
    (d / "session.json").write_text("{ not json", encoding="utf-8")

    out, code = _run(["session", "list"])
    assert code == EXIT_DEGRADED          # must fire: the fixture really is unreadable
    # no line of output begins with the forged text, and the name is still shown in escaped form
    assert not any(line.startswith("TOTAL:") for line in out.splitlines()), out
    assert "evil" in out


# The readable row's three text fields come out of `session.json`'s *body* (#62).
FORGEABLE_META_FIELDS = ("slug", "provider", "updated_at")


@pytest.mark.parametrize("field", FORGEABLE_META_FIELDS)
def test_a_readable_row_cannot_forge_a_line_from_session_json(workspace, field):
    """Every text field the readable row prints is untrusted (#40)."""
    _seed(HEALTHY)
    _seed("tampered", analysed=False)
    p = canonical_dir("tampered") / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data[field] = f"tampered\n  forged-row{' ' * 30} rev 999  (trusted, 2026-01-01T00:00:00Z)"
    p.write_text(json.dumps(data), encoding="utf-8")

    out, code = _run(["session", "list"])
    assert code == 0                                   # it is readable; this is not a degraded row
    # must fire: the healthy sibling is rendered normally, so this is not passing on an empty listing
    assert re.search(rf"  {HEALTHY}\s+rev \d", out), out
    # nothing the file put in that field reaches column 0 of a line of our own output
    assert not any(ln.startswith("forged-row") or ln.lstrip().startswith("forged-row")
                   for ln in out.splitlines()), out


def test_json_never_lets_session_json_forge_a_line(workspace):
    """The `--json` sibling of the case above, kept separate because it is safe for a *different* reason."""
    _seed("tampered", analysed=False)
    p = canonical_dir("tampered") / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["provider"] = "x\nFORGED"
    p.write_text(json.dumps(data), encoding="utf-8")

    out, _ = _run(["session", "list", "--json"])
    assert not any(ln.startswith("FORGED") for ln in out.splitlines()), out
    # must fire: the value did arrive, escaped rather than dropped
    assert json.loads(out)["sessions"][0]["provider"] == "x\nFORGED"


def test_a_multi_line_error_stays_one_row(workspace):
    """A degraded row is one row. `read_meta` refusing a `session.json` whose `current_revision` is a string
    raises a pydantic `ValidationError` whose message is four lines."""
    _seed(HEALTHY)
    _seed(BROKEN_META, analysed=False)
    p = canonical_dir(BROKEN_META) / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["current_revision"] = "not an integer"
    p.write_text(json.dumps(data), encoding="utf-8")

    out, code = _run(["session", "list"])
    assert code == EXIT_DEGRADED
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert sum(1 for ln in lines if BROKEN_META in ln) == 1, out
    assert sum(1 for ln in lines if HEALTHY in ln) == 1, out       # must fire
    # header + two rows + the footer, and nothing spilled between them
    assert len(lines) == 4, out
