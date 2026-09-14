"""The third outcome of the session-root partition: an entry that could not be examined (#80).

`_scan_session_root` decides whether each name under the session root is a session by probing
`<name>/session.json`. That probe can *fail* — `Path.exists()` re-raises `EACCES`, which is not in
`pathlib`'s ignored set — and one entry the process cannot stat into therefore aborted the partition
for every entry: `session list` exited 1 with an empty stdout and a raw `PermissionError` traceback,
and every healthy session in the workspace was invisible.

Invariant 15 one layer below where #7 and #62 put the guard. `SessionService.list_entries()` degrades
a row it *has*; this failure happens in the scan that produces the row set, before any row exists.

**A session, not a session, and *could not tell*.** The third is not routable into either of the
others, and this module asserts both halves of that: routed into the non-sessions bucket the entry
would not come back from `list_session_slugs` and `session list` would silently omit it — the
invisible entry #67 exists to close, one function along; routed into the slugs bucket the listing
would claim it *is* a session, which is the one thing nobody established.

Every case runs against a fixture that also holds a **healthy** session and asserts it still renders
in full, because a fix that lost the healthy session would pass an exit-code assertion on its own.
"""

from __future__ import annotations

import io
import json
import os
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from requivo.core import persistence as store
from requivo.deterministic import EXIT_DEGRADED
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService

HEALTHY = "leave-approval"
BLOCKED = "blocked-entry"


def _run(argv):
    """`app()` with stdout captured, returning `(text, exit_code)`.

    Both halves are returned because both are asserted: a fix that exits 4 with an empty stdout is
    the same defect wearing the right exit code, and a helper that surfaced only the exception would
    let it pass.
    """
    from requivo.cli import app
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            app(argv, client=None)   # client=None -> any accidental API use would blow up
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


def _seed_healthy() -> None:
    SessionService().create_session("We would like a leave approval system.", slug=HEALTHY)


@pytest.fixture
def blocked(workspace, request):
    """A directory under the session root that the process cannot stat into — or a loud skip.

    `chmod 000` denies the `x` bit, so `stat` on any *child* fails with `EACCES` while the parent
    listing still succeeds. That is precisely the shape of the defect: the root was listed, one
    entry in it could not be examined.

    Two ways this fixture can fail to break anything, and neither may pass silently:

    * **Windows** — POSIX mode bits do not deny traversal there, so the class is unreachable and the
      skip names what went untested. A test that trivially passed on that leg would report coverage
      of the third state that it does not have, and a green leg is the one nobody re-reads.
    * **root** — `chmod 000` denies a privileged process nothing. Probed rather than assumed, for
      the same reason: the probe is the must-fire half of every assertion below.

    The mode is restored on teardown whatever happens, because pytest cannot remove a `tmp_path` it
    may not traverse.
    """
    _seed_healthy()
    d = store.session_root() / BLOCKED
    d.mkdir(parents=True, exist_ok=True)
    request.addfinalizer(lambda: d.chmod(0o755))
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: that an "
                    "entry whose examination raises reaches the caller as a fact rather than as "
                    "an exception. Every other platform runs it.")
    d.chmod(0o000)
    try:
        (d / "session.json").exists()
    except PermissionError:
        return d
    pytest.skip("chmod 000 did not deny the session.json probe on this run (running as root?). "
                "UNTESTED HERE: the could-not-examine arm of the partition.")


# -- the fixture really breaks something --------------------------------------


def test_the_probe_the_partition_makes_really_raises_here(blocked):
    """Must fire. Every assertion below is about what happens when the partition probe raises; if
    it stopped raising, this whole module would turn green while proving nothing."""
    with pytest.raises(PermissionError):
        (blocked / "session.json").exists()


# -- the partition has three outcomes -----------------------------------------


def test_the_partition_answers_in_three_states_and_the_third_is_neither_neighbour(blocked):
    """Routed into `others`, the entry never comes back from `list_session_slugs` and `session list`
    omits it silently — #67's invisible entry, one function along. Routed into `slugs`, the listing
    claims it is a session. It is in its own bucket, and in neither of theirs."""
    slugs, others, unexaminable = store._scan_session_root()

    assert slugs == [HEALTHY]                              # must fire: the healthy half survives
    assert [p.name for p in others] == []
    assert [e.name for e in unexaminable] == [BLOCKED]
    # The error names the path it could not stat, which is the part a user acts on. Asserted on the
    # path rather than on the words `permission denied`: `str(OSError)` embeds `strerror`, which the
    # C library translates under a non-English locale, so a CI leg with `LANG` set would fail this
    # for a reason that has nothing to do with the code. The path is the same string everywhere.
    assert "session.json" in unexaminable[0].error, unexaminable[0].error
    assert BLOCKED in unexaminable[0].error, unexaminable[0].error


def test_list_session_slugs_still_answers_only_what_is_known_to_be_a_session(blocked):
    """The contract that must not widen. `doctor`, `session verify` and every read path reason over
    these names, and an entry nobody could examine is not one of them."""
    assert store.list_session_slugs() == [HEALTHY]
    assert [e.name for e in store.list_unexaminable_entries()] == [BLOCKED]

    # The second part has no reader of its own since #300 — `list_non_session_entries` was
    # production-dead and this assertion was one of its two callers. Taken from
    # `scan_session_root`, which is what `doctor` calls, so the claim is about the partition the
    # product actually reads rather than about a function only this test ran.
    #
    # **The old line was `[... for e in list_non_session_entries()] == []` and it could not fire.**
    # Asserting a bucket is empty passes when the scan looked and found nothing *and* when the
    # scan is broken and always answers nothing — this repository's own defect class, sitting
    # inside a test written to pin it, and carried across unchanged by #300's first cut until
    # review named it. So the bucket is *populated* here rather than asserted empty: a stray
    # directory is created after the fixture, which is what a non-session entry actually is, and
    # it has to come back from the second part and from nowhere else. The empty answer is no
    # longer an answer this test accepts.
    (store.session_root() / "not-a-session").mkdir()

    slugs, others, blind = store.scan_session_root()
    assert (slugs, [e.name for e in others], [e.name for e in blind]) == (
        [HEALTHY], ["not-a-session"], [BLOCKED])
    # ...and the contract this test is named for still holds with one there: a non-session widens
    # neither of its neighbours.
    assert store.list_session_slugs() == [HEALTHY]
    assert [e.name for e in store.list_unexaminable_entries()] == [BLOCKED]


def test_the_repository_exposes_the_third_bucket(blocked):
    """The service layer cannot reach `core.persistence` directly — storage is injected — so the
    third state needs a seam of its own or it stops at Core."""
    repo = FileSessionRepository()
    assert repo.list_slugs() == [HEALTHY]
    assert [e.name for e in repo.list_unexaminable()] == [BLOCKED]


# -- session list ---------------------------------------------------------------


def test_one_unexaminable_entry_no_longer_takes_the_whole_listing_down(blocked):
    out, code = _run(["session", "list"])

    # must fire: the healthy session is rendered in full, not lost with the traceback
    assert HEALTHY in out, out
    assert "rev 0" in out
    # ...and the entry that could not be examined is named rather than dropped or fatal
    assert BLOCKED in out, out
    assert "could not be read" in out.lower()
    assert code == EXIT_DEGRADED

    # The footer counts the entry without calling it a session. Every degraded row used to come from
    # `list_session_slugs`, so `1 session could not be read` was true of all of them; it is not true
    # of this one, and the footer is the last line a reader takes away. Both halves asserted, because
    # `"session" not in footer` alone would pass on a footer that stopped being printed.
    footer = next(ln for ln in out.splitlines() if "could not be read." in ln)
    assert footer.startswith("1 entry could not be read."), footer


def test_the_row_carries_the_reason_because_the_reason_is_the_remedy(blocked):
    """*Permission denied on this path* is something a user can act on; a flattened `unreadable` is
    not. The row keeps the underlying error text, exactly as every other degraded row does.

    Asserted on the path the error names rather than on the words, because `strerror` is translated
    under a non-English locale and the path is not."""
    out, _ = _run(["session", "list"])
    row = next(ln for ln in out.splitlines() if BLOCKED in ln)
    assert "could not be read" in row
    assert "session.json" in row, row


def test_the_row_states_no_fact_it_could_not_read(blocked):
    """No revision, no provider, no timestamp. A plausible `rev 0` on an entry nobody could open is
    the quiet-wrong-answer form of the same bug — and `rev 0` would be doubly wrong here, because it
    is also exactly what a real un-analysed session shows."""
    out, _ = _run(["session", "list"])
    rows = {name: next(ln for ln in out.splitlines() if name in ln) for name in (HEALTHY, BLOCKED)}

    # must fire: the healthy row carries both patterns, so their absence below is about the row
    assert re.search(r"\brev \d", rows[HEALTHY])
    assert re.search(r"20\d\d-\d\d-\d\dT", rows[HEALTHY])

    assert not re.search(r"\brev \d", rows[BLOCKED]), rows[BLOCKED]
    assert not re.search(r"20\d\d-\d\d-\d\dT", rows[BLOCKED]), rows[BLOCKED]


def test_json_keeps_every_key_on_the_row_and_claims_nothing(blocked):
    out, code = _run(["session", "list", "--json"])
    payload = json.loads(out)
    rows = {r["slug"]: r for r in payload["sessions"]}

    assert code == EXIT_DEGRADED
    assert rows.keys() == {HEALTHY, BLOCKED}
    assert rows[HEALTHY].keys() == rows[BLOCKED].keys()
    assert payload["degraded"] == 1

    # must fire: the healthy row is unchanged in every field it always had
    assert rows[HEALTHY]["readable"] is True
    assert rows[HEALTHY]["revision"] == 0
    assert rows[HEALTHY]["updated_at"]

    assert rows[BLOCKED]["readable"] is False
    assert rows[BLOCKED]["revision"] is None
    assert rows[BLOCKED]["provider"] is None
    assert rows[BLOCKED]["updated_at"] is None
    assert rows[BLOCKED]["error"]


def test_no_traceback_reaches_the_user(blocked):
    """A `PermissionError` here is an ordinary condition, not a bug in Requivo. The answer is a row,
    so nothing is raised out of the command at all beyond the exit code."""
    out, code = _run(["session", "list"])
    assert "Traceback" not in out
    assert code == EXIT_DEGRADED


# -- doctor ---------------------------------------------------------------------


def test_doctor_reports_the_entry_instead_of_declaring_the_whole_root_unreadable(blocked):
    """`sessions unreadable — <path>/blocked-entry/session.json` with `could not be listed` beneath
    it is a claim broader than what failed. The root *was* listed. One entry in it could not be
    examined, and that is what the report has to say."""
    out, _ = _run(["doctor", "--json"])
    h = json.loads(out)["sessions"]

    assert h["readable"] is True, h
    assert h["total"] == 1, "the count is what could be confirmed, and the healthy session is in it"
    assert h["error"] is None
    assert h["non_sessions"] == [], "not a non-session: nobody established what this is"
    assert [e["name"] for e in h["unexaminable"]] == [BLOCKED]
    assert h["unexaminable"][0]["error"]

    text, _ = _run(["doctor"])
    assert BLOCKED in text
    assert "could not be listed" not in text, text
    assert "could not be examined" in text.lower(), text


def test_doctor_keeps_the_whole_root_arm_for_the_case_that_really_is_the_whole_root(workspace,
                                                                                   monkeypatch):
    """`iterdir()` itself failing is genuinely the whole root, and that arm must survive the change:
    a fix that turned every listing failure into a per-entry row would answer `0 sessions` about a
    workspace nobody could look into, which is #12's F3."""
    from requivo.deterministic import doctor as det

    def _unreadable():
        raise OSError("boom")

    monkeypatch.setattr(det.store, "scan_session_root", _unreadable)
    h = json.loads(_run(["doctor", "--json"])[0])["sessions"]

    assert h["readable"] is False
    assert h["total"] is None, "0 would say the workspace is empty, which we do not know"
    assert h["non_sessions"] is None
    assert h["unexaminable"] is None, "[] here would read as 'we looked and there was nothing'"
    assert "boom" in h["error"]

    text, _ = _run(["doctor"])
    assert "could not be listed" in text


def test_doctor_on_a_clean_workspace_says_nothing_about_any_of_this(workspace):
    """The control. A clean workspace earns no row and the sessions line keeps its tick — otherwise
    the finding above is a line everybody sees and nobody reads."""
    _seed_healthy()
    h = json.loads(_run(["doctor", "--json"])[0])["sessions"]
    assert h["unexaminable"] == [], "looked and found nothing — not the `None` above"
    assert h["readable"] is True and h["total"] == 1

    text, _ = _run(["doctor"])
    line = next(ln for ln in text.splitlines()
                if ln.startswith("  ") and not ln.startswith("   ") and "sessions" in ln)
    assert "✅" in line, line
    assert "could not be examined" not in text.lower()


def test_a_clean_workspace_lists_cleanly_and_exits_zero(workspace):
    """The other control, on the other surface. Nothing about the ordinary listing moves."""
    _seed_healthy()
    out, code = _run(["session", "list"])
    assert code == 0
    assert "could not be read" not in out.lower()
    assert HEALTHY in out


# -- the name is untrusted text (#40) -------------------------------------------


def test_an_unexaminable_name_carrying_a_control_character_cannot_forge_a_line(workspace, request):
    """The directory name is created by whoever holds the workspace and reaches both surfaces raw:
`session list` prints it as a degraded row's slug, `doctor` prints it under the sessions check. A
name carrying a newline would otherwise write what reads as a second, authoritative line of
Requivo's own output at column 0 — the shape #40 found in `doctor`."""
    if os.name == "nt":
        pytest.skip("NTFS refuses a control character in a filename, and POSIX mode bits do not "
                    "deny traversal here either. UNTESTED HERE: the render guard on an "
                    "unexaminable entry's name, on both surfaces. `display_token` itself is "
                    "asserted on every platform by tests/test_cli_degraded_listing.py.")
    _seed_healthy()
    # No path separator in it: a `/` would nest the directory rather than name it, and the fixture
    # would then be testing nothing at all.
    hostile = "evil\nTOTAL: 0 sessions, nothing to see"
    d: Path = store.session_root() / hostile
    try:
        d.mkdir(parents=True)
    except (OSError, ValueError):
        pytest.skip("this filesystem refuses a directory name containing a newline. UNTESTED "
                    "HERE: the render guard on an unexaminable entry's name.")
    request.addfinalizer(lambda: d.chmod(0o755))
    d.chmod(0o000)
    try:
        (d / "session.json").exists()
    except PermissionError:
        pass
    else:
        pytest.skip("chmod 000 did not deny the probe on this run (running as root?). UNTESTED "
                    "HERE: the render guard on an unexaminable entry's name.")

    for argv in (["session", "list"], ["doctor"]):
        out, _ = _run(argv)
        assert not any(ln.startswith("TOTAL:") for ln in out.splitlines()), (argv, out)
        assert "evil" in out, (argv, out)          # must fire: the name did reach the output


# -- #97: the same unguarded probe, one function along -------------------------


def test_session_exists_answers_could_not_tell_through_the_error_channel(blocked):
    """`session_exists` raises rather than answering `False` on a probe it could not make. Same defect
as the partition above, in the function every other verb opens with. The bool is deliberately
*not* widened: `cli.py` and `session import --force` read this to decide whether to create or
overwrite, so `False` on a permissions fault is *there is nothing here* followed by a write.
Three states, two returns, so the third leaves as an error. See #82."""
    from requivo.core.errors import SessionUnreadableError

    with pytest.raises(SessionUnreadableError) as caught:
        store.session_exists(BLOCKED)
    assert caught.value.details["slug"] == BLOCKED
    # The positive control: the healthy session in the same workspace still answers normally, so a
    # blanket `raise` would fail here rather than pass this file.
    assert store.session_exists(HEALTHY) is True
    assert store.session_exists("no-such-session-anywhere") is False


def test_read_meta_answers_could_not_tell_through_the_error_channel(blocked):
    """#264, the identical class one function further along. `session_exists` was fixed by routing its
existence probe through `_probe`; `read_meta`'s own `if not p.exists(): raise _no_session(slug)`
sat outside the `try` that wraps `OSError`, so a session.json the process cannot stat still
escaped as a raw `PermissionError` instead of `SessionUnreadableError`"""
    from requivo.core.errors import SessionNotFoundError, SessionUnreadableError

    with pytest.raises(SessionUnreadableError) as caught:
        store.read_meta(BLOCKED)
    assert caught.value.details["slug"] == BLOCKED

    # Must-fire control: a genuinely absent session still answers "not found", not "unreadable" --
    # collapsing the two the other way is the bug on the far side of this fix.
    with pytest.raises(SessionNotFoundError):
        store.read_meta("no-such-session-anywhere")

    # And the healthy session in the same workspace still reads normally.
    assert store.read_meta(HEALTHY).slug == HEALTHY


def test_absent_is_still_false_because_absent_is_a_real_answer(workspace):
    """`ENOENT` must keep returning `False`. It is the commonest answer, `Path.exists()` already
    swallows it, and turning it into an exception would make every `session init` raise."""
    _seed_healthy()
    assert store.session_exists("nothing-here") is False
    assert store.legacy_exists("nothing-here") is False


def test_verify_says_it_could_not_look_and_exits_4_not_1(blocked):
    """The pairing #80 created and #97 closes. `session list` renders a degraded row for an entry it
could not examine and prints a footer telling the reader to run `session verify <slug>`. That
verb opened with `session_exists` and crashed with a bare `PermissionError` on the one slug it
had just been pointed at. **4, not 1.** Exit 1 says *I checked and it is broken*; nothing checked
anything here. See #86."""
    text, code = _run(["session", "verify", BLOCKED])
    assert code == EXIT_DEGRADED, text
    assert "could not examine" in text.lower(), text
    # The sentence that stops a reader taking silence for a clean bill of health.
    assert "not a report that it is sound" in text, text
    assert "Traceback" not in text


def test_verify_json_carries_the_third_state_as_a_field_not_as_an_empty_list(blocked):
    """`problems: []` spells both *checked, nothing wrong* and *nothing was checked*. So the payload
gains `session: {checked, error}`, a sibling of `context_cards` carrying the same two keys for
the same reason. A consumer must branch on `session.checked`, never on the emptiness of
`problems` — this asserts the field is there and that the empty list alone would have misled."""
    text, code = _run(["session", "verify", BLOCKED, "--json"])
    assert code == EXIT_DEGRADED, text
    payload = json.loads(text)
    assert payload["session"]["checked"] is False
    assert payload["session"]["error"]
    assert payload["ok"] is False
    assert payload["problems"] == []   # <- exactly why `session.checked` has to exist


def test_a_healthy_session_reports_checked_true_so_the_field_is_not_a_constant(blocked):
    """The positive control for the field above: on a session that *was* examined it reads `True`
    with a `null` error, and the verb still exits 0. A payload hard-coding `False` would pass every
    assertion in the test above and fail here."""
    text, code = _run(["session", "verify", HEALTHY, "--json"])
    assert code == 0, text
    payload = json.loads(text)
    assert payload["session"] == {"checked": True, "error": None}
    assert payload["ok"] is True


# -- #90: the *error* on that line is untrusted text too -------------------------


def test_the_error_text_on_a_non_session_line_cannot_forge_a_line_either():
    """`_non_session_detail` interpolates `error` beside names that all go through `display_token`. The
names were wrapped and the error was not, for a release. `error` is `str(e)` from a deliberately
wide `except Exception` in the store — the docstring beside it says the set of ways a member can
be broken is open — so an open set of causes was feeding an unescaped interpolation, which is the
shape #40 was."""
    from requivo.deterministic.doctor import _non_session_detail

    forged = "boom\nTOTAL: 0 sessions, all clear"
    for entry in ({"kind": "unknown", "error": forged},
                  {"kind": "directory", "error": forged, "entry_count": 0, "entries": []}):
        detail = _non_session_detail(entry)
        assert "\n" not in detail, (entry["kind"], detail)
        assert "TOTAL: 0 sessions, all clear" not in detail.splitlines()[0].split(" — ")[0]
        assert "boom" in detail, "the escaped text still has to be readable"


def test_a_plain_error_is_not_mangled_by_the_wrap():
    """The positive control. `display_token` on ordinary text must leave it alone, or the fix trades
    a forgeable line for an unreadable one — and every operator-facing message on this surface would
    pay for a case nobody has reached."""
    from requivo.deterministic.doctor import _non_session_detail

    detail = _non_session_detail({"kind": "unknown", "error": "[Errno 13] Permission denied"})
    assert "[Errno 13] Permission denied" in detail


def test_no_error_string_reaches_a_printed_line_unwrapped():
    """The class guard, added because fixing the two named instances left four siblings (#90). The first
attempt at #90 wrapped the two sites the issue pointed at. Four more of exactly the same shape
stayed raw — three of them on `doctor`'s own report, the surface #40 and #90 are both about, and
one of them **eleven lines below** a sibling that had just been wrapped, in the same function.
See #73."""
    package = Path(__file__).resolve().parents[1] / "src" / "requivo" / "deterministic"
    modules = sorted(package.rglob("*.py"))
    assert modules, (
        f"the guard found no modules under {package}: it is not looking at the deterministic "
        f"surface, and a negative assertion over an empty set passes for the wrong reason")
    unwrapped = []
    for path in modules:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "f\"" not in line and "f'" not in line:
                continue
            for m in re.finditer(r"\{([^{}]*\berror\b[^{}]*)\}", line):
                if "display_token(" in m.group(1):
                    continue
                unwrapped.append(f"{path.name}:{lineno}: {m.group(0)}  |  {stripped[:90]}")
    assert not unwrapped, (
        "an error string is interpolated into a printed line without display_token:\n  "
        + "\n  ".join(unwrapped))


def test_that_guard_really_fires(tmp_path):
    """The positive control the guard above shipped without, which is this class's own tell. A guard
asserting `not []` over a scan that found nothing is an all-clear nobody earned — the same
argument `tests/test_boundaries.py` makes about an empty scan set. So both spellings that escaped
the first version are fed to the matcher here, and the wrapped forms beside them must not fire."""
    pattern = re.compile(r"\{([^{}]*\berror\b[^{}]*)\}")

    def fires(line: str) -> bool:
        return any("display_token(" not in m.group(1) for m in pattern.finditer(line))

    assert fires("""    print(f"x {c['error']}")"""), "the subscript spelling"
    assert fires('    print(f"x {error}")'), "the bare-local spelling, which v1 of this guard missed"
    assert fires("""    print(f"x {entry['error'] or _NO_DETAIL}")"""), "the or-expression spelling"
    assert not fires("""    print(f"x {display_token(c['error'])}")""")
    assert not fires("""    print(f"x {display_token(entry['error'] or _NO_DETAIL)}")""")
    assert not fires('    print(f"x {slug}")'), "an unrelated interpolation must stay quiet"
