"""The third outcome of the session-root partition: an entry that could not be examined (#80)."""

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
    """`app()` with stdout captured, returning `(text, exit_code)`."""
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
    """A directory under the session root that the process cannot stat into — or a loud skip."""
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
    """Must fire. Every assertion below is about what happens when the partition probe raises."""
    with pytest.raises(PermissionError):
        (blocked / "session.json").exists()


# -- the partition has three outcomes -----------------------------------------


def test_the_partition_answers_in_three_states_and_the_third_is_neither_neighbour(blocked):
    """Routed into `others`, the entry never comes back from `list_session_slugs` and `session list` omits it
    silently — #67's invisible entry, one function along."""
    slugs, others, unexaminable = store._scan_session_root()

    assert slugs == [HEALTHY]                              # must fire: the healthy half survives
    assert [p.name for p in others] == []
    assert [e.name for e in unexaminable] == [BLOCKED]
    # The error names the path it could not stat, which is the part a user acts on.
    assert "session.json" in unexaminable[0].error, unexaminable[0].error
    assert BLOCKED in unexaminable[0].error, unexaminable[0].error


def test_list_session_slugs_still_answers_only_what_is_known_to_be_a_session(blocked):
    """The contract that must not widen. `doctor`, `session verify` and every read path reason over these
    names, and an entry nobody could examine is not one of them."""
    assert store.list_session_slugs() == [HEALTHY]
    assert [e.name for e in store.list_unexaminable_entries()] == [BLOCKED]

    # The second part has no reader of its own since #300.
    #
    # **The old line was `[... for e in list_non_session_entries()] == []` and it could not fire.** Asserting a bucket is empty passes when the scan looked and found nothing *and* when the scan is broken and always answers nothing — this repository's own defect class, sitting inside a test written to pin it, and carried across unchanged by #300's first cut until review named it.
    (store.session_root() / "not-a-session").mkdir()

    slugs, others, blind = store.scan_session_root()
    assert (slugs, [e.name for e in others], [e.name for e in blind]) == (
        [HEALTHY], ["not-a-session"], [BLOCKED])
    # ...and the contract this test is named for still holds with one there.
    assert store.list_session_slugs() == [HEALTHY]
    assert [e.name for e in store.list_unexaminable_entries()] == [BLOCKED]


def test_the_repository_exposes_the_third_bucket(blocked):
    """The service layer cannot reach `core.persistence` directly."""
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

    # The footer counts the entry without calling it a session.
    footer = next(ln for ln in out.splitlines() if "could not be read." in ln)
    assert footer.startswith("1 entry could not be read."), footer


def test_the_row_carries_the_reason_because_the_reason_is_the_remedy(blocked):
    """*Permission denied on this path* is something a user can act on; a flattened `unreadable` is not."""
    out, _ = _run(["session", "list"])
    row = next(ln for ln in out.splitlines() if BLOCKED in ln)
    assert "could not be read" in row
    assert "session.json" in row, row


def test_the_row_states_no_fact_it_could_not_read(blocked):
    """No revision, no provider, no timestamp. A plausible `rev 0` on an entry nobody could open is the
    quiet-wrong-answer form of the same bug."""
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
    """A `PermissionError` here is an ordinary condition, not a bug in Requivo."""
    out, code = _run(["session", "list"])
    assert "Traceback" not in out
    assert code == EXIT_DEGRADED


# -- doctor ---------------------------------------------------------------------


def test_doctor_reports_the_entry_instead_of_declaring_the_whole_root_unreadable(blocked):
    """`sessions unreadable — <path>/blocked-entry/session.json` with `could not be listed` beneath it is a
    claim broader than what failed."""
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
    """`iterdir()` itself failing is genuinely the whole root, and that arm must survive the change (#12)."""
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
    """The control. A clean workspace earns no row and the sessions line keeps its tick."""
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
    """The other control, on the other surface."""
    _seed_healthy()
    out, code = _run(["session", "list"])
    assert code == 0
    assert "could not be read" not in out.lower()
    assert HEALTHY in out


# -- the name is untrusted text (#40) -------------------------------------------


def test_an_unexaminable_name_carrying_a_control_character_cannot_forge_a_line(workspace, request):
    """The directory name is created by whoever holds the workspace and reaches both surfaces raw (#40)."""
    if os.name == "nt":
        pytest.skip("NTFS refuses a control character in a filename, and POSIX mode bits do not "
                    "deny traversal here either. UNTESTED HERE: the render guard on an "
                    "unexaminable entry's name, on both surfaces. `display_token` itself is "
                    "asserted on every platform by tests/test_cli_degraded_listing.py.")
    _seed_healthy()
    # No path separator in it: a `/` would nest the directory rather than name it.
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
    """`session_exists` raises rather than answering `False` on a probe it could not make (#82)."""
    from requivo.core.errors import SessionUnreadableError

    with pytest.raises(SessionUnreadableError) as caught:
        store.session_exists(BLOCKED)
    assert caught.value.details["slug"] == BLOCKED
    # The positive control: the healthy session in the same workspace still answers normally.
    assert store.session_exists(HEALTHY) is True
    assert store.session_exists("no-such-session-anywhere") is False


def test_read_meta_answers_could_not_tell_through_the_error_channel(blocked):
    """#264, the identical class one function further along."""
    from requivo.core.errors import SessionNotFoundError, SessionUnreadableError

    with pytest.raises(SessionUnreadableError) as caught:
        store.read_meta(BLOCKED)
    assert caught.value.details["slug"] == BLOCKED

    # Must-fire control: a genuinely absent session still answers "not found", not "unreadable".
    with pytest.raises(SessionNotFoundError):
        store.read_meta("no-such-session-anywhere")

    # And the healthy session in the same workspace still reads normally.
    assert store.read_meta(HEALTHY).slug == HEALTHY


def test_absent_is_still_false_because_absent_is_a_real_answer(workspace):
    """`ENOENT` must keep returning `False`. It is the commonest answer."""
    _seed_healthy()
    assert store.session_exists("nothing-here") is False
    assert store.legacy_exists("nothing-here") is False


def test_verify_says_it_could_not_look_and_exits_4_not_1(blocked):
    """The pairing #80 created and #97 closes. `session list` renders a degraded row for an entry it could not
    examine and prints a footer telling the reader to run `session verify <slug>`."""
    text, code = _run(["session", "verify", BLOCKED])
    assert code == EXIT_DEGRADED, text
    assert "could not examine" in text.lower(), text
    # The sentence that stops a reader taking silence for a clean bill of health.
    assert "not a report that it is sound" in text, text
    assert "Traceback" not in text


def test_verify_json_carries_the_third_state_as_a_field_not_as_an_empty_list(blocked):
    """`problems: []` spells both *checked, nothing wrong* and *nothing was checked*."""
    text, code = _run(["session", "verify", BLOCKED, "--json"])
    assert code == EXIT_DEGRADED, text
    payload = json.loads(text)
    assert payload["session"]["checked"] is False
    assert payload["session"]["error"]
    assert payload["ok"] is False
    assert payload["problems"] == []   # <- exactly why `session.checked` has to exist


def test_a_healthy_session_reports_checked_true_so_the_field_is_not_a_constant(blocked):
    """The positive control for the field above."""
    text, code = _run(["session", "verify", HEALTHY, "--json"])
    assert code == 0, text
    payload = json.loads(text)
    assert payload["session"] == {"checked": True, "error": None}
    assert payload["ok"] is True


# -- #90: the *error* on that line is untrusted text too -------------------------


def test_the_error_text_on_a_non_session_line_cannot_forge_a_line_either():
    """`_non_session_detail` interpolates `error` beside names that all go through `display_token` (#40)."""
    from requivo.deterministic.doctor import _non_session_detail

    forged = "boom\nTOTAL: 0 sessions, all clear"
    for entry in ({"kind": "unknown", "error": forged},
                  {"kind": "directory", "error": forged, "entry_count": 0, "entries": []}):
        detail = _non_session_detail(entry)
        assert "\n" not in detail, (entry["kind"], detail)
        assert "TOTAL: 0 sessions, all clear" not in detail.splitlines()[0].split(" — ")[0]
        assert "boom" in detail, "the escaped text still has to be readable"


def test_a_plain_error_is_not_mangled_by_the_wrap():
    """The positive control. `display_token` on ordinary text must leave it alone."""
    from requivo.deterministic.doctor import _non_session_detail

    detail = _non_session_detail({"kind": "unknown", "error": "[Errno 13] Permission denied"})
    assert "[Errno 13] Permission denied" in detail


def test_no_error_string_reaches_a_printed_line_unwrapped():
    """The class guard, added because fixing the two named instances left four siblings (#90)."""
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
    """The positive control the guard above shipped without, which is this class's own tell."""
    pattern = re.compile(r"\{([^{}]*\berror\b[^{}]*)\}")

    def fires(line: str) -> bool:
        return any("display_token(" not in m.group(1) for m in pattern.finditer(line))

    assert fires("""    print(f"x {c['error']}")"""), "the subscript spelling"
    assert fires('    print(f"x {error}")'), "the bare-local spelling, which v1 of this guard missed"
    assert fires("""    print(f"x {entry['error'] or _NO_DETAIL}")"""), "the or-expression spelling"
    assert not fires("""    print(f"x {display_token(c['error'])}")""")
    assert not fires("""    print(f"x {display_token(entry['error'] or _NO_DETAIL)}")""")
    assert not fires('    print(f"x {slug}")'), "an unrelated interpolation must stay quiet"
