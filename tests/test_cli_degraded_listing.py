"""Invariant 15 on the CLI — `requivo session list` survives its own members (#62).

The web half of this shipped in #7; `deterministic.py` was held by another lane that round and never
got the guard. The shape of the failure is the same and the shape of the test has to be too, so this
module is the CLI sibling of `tests/web/test_degraded_listing.py` and borrows its discipline:

* every degradation case runs against a fixture that also holds **healthy** sessions and asserts they
  still render in full — a fix that degrades every row passes the degradation half on its own;
* the breaker asserts it really broke something, through the strict read that is *supposed* to raise,
  so a fixture that quietly stopped breaking anything cannot turn this file green;
* *could not be read* and *not analysed yet* are asserted to render differently, because a row at
  revision 0 is a normal row and the whole point of the third state is that it is not one.

**One correction to the issue as filed, measured rather than assumed.** #62 carries #7's table of
three break modes and says all three "apply to it unchanged". They do not. The web row calls
`request_text` and `status()`; the CLI row reads nothing but the metadata `list_entries` already
loaded, so only the `read_meta` mode reaches this command. The other two are kept here as controls
(`test_a_break_below_the_metadata_does_not_reach_this_listing`) because that is a fact about the
current row shape rather than a guarantee: a future row that reads the request or the status needs
the per-row guard the web viewmodel has, and the control is what will say so when it stops holding.
"""

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


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


def _run(argv):
    """`app()` with stdout captured, returning `(text, exit_code)`.

    The exit code is half of what this module asserts, so it is returned rather than allowed past the
    caller: a degraded listing exits non-zero *and still prints the listing*, and a helper that only
    surfaced the exception would let a fix that exits 4 with an empty stdout pass.
    """
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
    """A session written by a newer Requivo. `read_meta` refuses it, from inside the comprehension
    `list_sessions()` is — before any row exists to degrade. This is the mode that reaches the CLI."""
    p = canonical_dir(slug) / "session.json"
    # Explicit encoding on both halves: `read_text()` defaults to the locale's codec, cp1252 on a
    # default Windows console, and the store writes UTF-8.
    data = json.loads(p.read_text(encoding="utf-8"))
    data["format_version"] = SESSION_FORMAT_VERSION + 1
    p.write_text(json.dumps(data), encoding="utf-8")


def break_request(slug: str) -> None:
    """`request.md` replaced by a directory. `IsADirectoryError` on POSIX, `PermissionError` on
    Windows — nothing here asserts on the type, only that neither reaches this listing today."""
    p = canonical_dir(slug) / "request.md"
    p.unlink()
    p.mkdir()


def break_model(slug: str) -> None:
    """A crash mid-write leaves `model.json` truncated — a pydantic `ValidationError`, which is not a
    `RequivoError` and so escapes `app()`'s own handler as well."""
    (canonical_dir(slug) / "model.json").write_text('{"summary": {"objec', encoding="utf-8")


BELOW_METADATA = {BROKEN_REQUEST: break_request, BROKEN_MODEL: break_model}


# ── the breaker really breaks something ──────────────────────────────────────

def test_break_meta_defeats_the_strict_read(workspace):
    """Must fire. `list_sessions()` is the strict read and is *supposed* to raise here. Without this,
    a format version this build grew to accept would turn every case below green while proving
    nothing."""
    _seed(BROKEN_META)
    break_meta(BROKEN_META)
    with pytest.raises(Exception) as ei:   # noqa: PT011 - the type is persistence's, not ours
        SessionService().list_sessions()
    assert "format" in str(ei.value).lower()


# ── the listing survives, and names what it could not read ───────────────────

def test_one_unreadable_session_no_longer_takes_the_listing_down(workspace):
    """`SessionService.list_entries` is the *source* of the rows, and that is where invariant 15 has
    to be enforced: guarding the calls a row makes leaves the comprehension that produced the rows
    unguarded, which is the line that breaks first. `read_meta` refuses an unreadable `session.json`
    and a `format_version` newer than this build — so a user who ran a newer Requivo once, or
    imported a colleague's archive, lost the listing of every *other* session too."""
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
    """*Written by a newer Requivo, upgrade* is a remedy; a flattened `unreadable` is not. The row
    keeps the underlying error text, exactly as the web row does."""
    _seed(BROKEN_META)
    break_meta(BROKEN_META)
    out, _ = _run(["session", "list"])
    assert "upgrade requivo" in out.lower()


def test_the_degraded_row_states_no_fact_it_could_not_read(workspace):
    """No revision, no provider, no timestamp. A plausible `rev 0` on a session nobody could open is
    the quiet-wrong-answer form of the same bug."""
    _seed(HEALTHY)
    _seed(BROKEN_META)
    break_meta(BROKEN_META)
    out, _ = _run(["session", "list"])
    rows = {slug: next(ln for ln in out.splitlines() if slug in ln)
            for slug in (HEALTHY, BROKEN_META)}

    # must fire: the same two patterns are present on the healthy row, so an assertion that they are
    # absent from the degraded one is about the row and not about a regex that never matches.
    assert re.search(r"\brev \d", rows[HEALTHY])
    assert re.search(r"20\d\d-\d\d-\d\dT", rows[HEALTHY])

    # Regexes rather than substrings: the error text is free-form, and a bare `"rev" not in row`
    # would be asserting about the wording of the message as much as about the row.
    assert not re.search(r"\brev \d", rows[BROKEN_META]), rows[BROKEN_META]
    assert not re.search(r"20\d\d-\d\d-\d\dT", rows[BROKEN_META]), rows[BROKEN_META]


def test_a_session_at_revision_zero_is_a_normal_row_not_a_degraded_one(workspace):
    """*Not analysed yet* and *we could not look* are two states. Rendering them alike is the
    misinformation this third state exists to prevent."""
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
    """The correction to the issue, pinned. `request.md` and `model.json` are not read by this row,
    so these two sessions list normally and the command is a clean success.

    When a future row starts reading either, this test fails — and the fix is then the per-row
    `except Exception` the web viewmodel carries, not a change to this assertion.
    """
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
    """The compatibility statement, asserted. A degraded row carries the *same key set* as a healthy
    one with `null` where the fact is missing — not a shortened dict, which would turn a consumer's
    `row["revision"]` into a `KeyError` on a payload it was handed deliberately."""
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
    """#87. The payload is `{"sessions": [...], "degraded": n, "session_root": "..."}`.

    It was a bare array, alone among the CLI's `--json` outputs, and an array has no top level: no
    field can be added to it, ever, without the type change this test pins. The rows themselves are
    unchanged — the wrap is the whole difference — so a consumer's `jq '.[] | .slug'` becomes
    `jq '.sessions[] | .slug'` and nothing else moves.

    `degraded` is **not** here to recover a fact stdout was missing: `_session_list_row` already
    gives every row `readable` and `error`, so the count has always been derivable. It is here so
    exit 4 is *readable* on stdout rather than only signalled, which is the same argument that makes
    a degraded row name its session instead of disappearing.

    Both halves are asserted on the same fixture, because `degraded: 0` on a clean workspace is what
    tells a reader that `degraded: 1` means something — an assertion that the count is present would
    pass against a field hardcoded to any number.
    """
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
    """No sessions is not a special case and must not be a special shape. A caller reading
    `payload["sessions"]` has to keep working on a workspace nobody has used yet — the bare-array
    payload answered `[]` here, which at least had one meaning; an object that dropped its keys
    would have none."""
    out, code = _run(["session", "list", "--json"])
    empty = json.loads(out)
    assert code == 0
    assert empty == {"sessions": [], "degraded": 0, "session_root": str(session_root())}


# ── the exit code says which of the three happened ───────────────────────────

def test_the_three_outcomes_have_three_exit_codes(workspace):
    """`0`, `4` and `1` — listed cleanly, listed with a hole, could not list.

    Collapsing the middle one into either neighbour is invariant 15's own defect in the one channel a
    script that does not parse stdout can read: `0` says nothing is wrong, `1` says nothing was
    listed, and both are false about a listing that degraded a row.
    """
    _seed(HEALTHY)
    assert _run(["session", "list"])[1] == 0

    _seed(BROKEN_META)
    break_meta(BROKEN_META)
    assert _run(["session", "list"])[1] == EXIT_DEGRADED

    # could not read this one session at all: `session show` is the strict read and is the ordinary
    # clean failure. The point is that it is a *different* number from the one above.
    assert _run(["session", "show", BROKEN_META])[1] == 1


def test_the_degraded_code_collides_with_nothing():
    """The two exit-code constants live in two modules — `cli.py` imports `deterministic/`, so the
    dependency cannot run the other way — and nothing but this test stops them being given the same
    number. 0, 1 and 2 are taken by success, `RequivoError` and argparse; 130 joined the set at #206
    under the same rule, for the conventional SIGINT code."""
    assert EXIT_DEGRADED not in {0, 1, 2, EXIT_RENDER_FAILED, EXIT_INTERRUPTED}
    assert EXIT_INTERRUPTED not in {0, 1, 2, EXIT_RENDER_FAILED}


def test_the_degraded_exit_code_is_published_as_a_value_not_as_a_name():
    """What `docs/compatibility.md` promises is the number 4. It has never promised the symbol (#145).

    `deterministic/__init__.py` used to say the page published `EXIT_DEGRADED` "under this name", and
    it does not: the page carries a `4` row in its exit-code table, and it lists `requivo.deterministic`
    among the Python internals that are explicitly *not* stable — which #144 added on the argument that
    the module is internal plumbing for the offline verbs rather than an interface. (#144 put that as
    the module's whole public job being the `register(sub)` the CLI binds through, which undercounted
    its `__all__` by two; #148 corrected the page and this sentence with it.)

    **Publishing the name was refused, not merely left unchosen.** A promised import costs a major
    version to move, and it would buy a consumer nothing the documented exit code does not already
    give them: a script gating on a degraded listing reads the process's status, not this package's
    namespace. The comment claiming otherwise invited exactly the two mistakes worth avoiding —
    importing it from outside, and treating a rename as a breaking change.

    This is what makes the corrected comment checkable rather than a second unguarded claim: promote
    the module to stable, or renumber the code without the page, and this goes red.
    """
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
    """A session directory is created by whoever holds the workspace, and `list_session_slugs` returns
    its name verbatim. A name carrying a newline would otherwise write what reads as a second,
    authoritative line of Requivo's own output at column 0 — the shape #40 found in `doctor`.

    `display_token` is the render-side guard for exactly this, and the degraded row is a new site for
    it: `session.json` is unreadable there, so the name being printed is the raw directory name.
    """
    # No path separator in it: a `/` would nest the directory rather than name it, and the fixture
    # would then test nothing at all — which it did on the first run of this test.
    hostile = "evil\nTOTAL: 0 sessions, nothing to see"
    d = session_root() / hostile
    try:
        d.mkdir(parents=True)
    except (OSError, ValueError):
        # NTFS refuses a control character in a filename outright, so this class is unreachable
        # there and the skip is loud rather than a silent pass — a test that trivially passed on
        # Windows would report coverage of the render guard that it does not have. `display_token`
        # itself is asserted on every platform by `test_a_multi_line_error_stays_one_row`, whose
        # untrusted text comes out of a file's *contents* rather than its name.
        pytest.skip("this filesystem refuses a directory name containing a newline; the slug half "
                    "of the display_token guard is untested here")
    (d / "session.json").write_text("{ not json", encoding="utf-8")

    out, code = _run(["session", "list"])
    assert code == EXIT_DEGRADED          # must fire: the fixture really is unreadable
    # no line of output begins with the forged text, and the name is still shown in escaped form
    assert not any(line.startswith("TOTAL:") for line in out.splitlines()), out
    assert "evil" in out


# The readable row's three text fields come out of `session.json`'s *body*, which is a different
# question from the directory name above and was got wrong in this file's first draft. The docstring
# on `_session_list_line` used to claim `read_meta` validated them; it validates the slug it is
# *called with* (the directory name, via `canonical_dir`) and returns `SessionMeta.slug` — a bare
# `str` with no pattern — straight out of the JSON. Found by the audit on this branch (#62).
FORGEABLE_META_FIELDS = ("slug", "provider", "updated_at")


@pytest.mark.parametrize("field", FORGEABLE_META_FIELDS)
def test_a_readable_row_cannot_forge_a_line_from_session_json(workspace, field):
    """Every text field the readable row prints is untrusted, not just the degraded row's.

    `session.json` is untrusted input every time it is read back — invariant 14's second door, the
    same argument that made a stored `context_cards` name a refusal rather than something to echo
    (#40). A hand-edited or imported file can put a newline in `slug`, `provider` or `updated_at`,
    and the row would then write what reads as a second, authoritative row of Requivo's own listing
    at column 0. The forged row can claim any revision and any provider it likes.

    `current_revision` is not in the set: it is an `int`, so `read_meta` already refuses a string.
    """
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
    """The `--json` sibling of the case above, kept separate because it is safe for a *different*
    reason: `json.dumps` defaults to `ensure_ascii=True`, so a control character is escaped by the
    encoder rather than by `display_token`. Asserted rather than assumed, because that default is
    the whole of the guarantee — a later `ensure_ascii=False` for prettier output would silently
    reopen it."""
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
    """A degraded row is one row. `read_meta` refusing a `session.json` whose `current_revision` is a
    string raises a pydantic `ValidationError` whose message is four lines long — printed raw, one
    session becomes four rows of listing and the reader cannot tell where the row ends.

    The healthy sibling is the must-fire half: the listing is *its* header, *its* row and this one,
    and nothing else. Asserting only "no extra lines" would pass on a listing that rendered nothing.
    """
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
