"""The third outcome of the session-root partition: an entry that could not be examined (#80, #97, #90, #40)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from _fakes import run_cli_exit, seed_session
from test_persistence import deny_access

from requivo.core import persistence as store
from requivo.core.errors import SessionNotFoundError, SessionUnreadableError
from requivo.deterministic import EXIT_DEGRADED
from requivo.deterministic.doctor import _non_session_detail
from requivo.services.repository import FileSessionRepository

pytestmark = pytest.mark.usefixtures("workspace")
HEALTHY = "leave-approval"
BLOCKED = "blocked-entry"


def _seed_healthy() -> None:
    seed_session(HEALTHY, "We would like a leave approval system.", analysed=False)


@pytest.fixture
def blocked(request) -> Path:
    """A directory under the session root that the process cannot stat into, or a loud skip."""
    _seed_healthy()
    d = store.session_root() / BLOCKED
    d.mkdir(parents=True, exist_ok=True)
    return deny_access(d, request, "the could-not-examine arm of the partition")


# ── the partition has three outcomes ─────────────────────────────────────────


def test_the_partition_answers_in_three_states_and_the_third_is_neither_neighbour(blocked):
    """Routed into `others`, the entry would vanish from `list_session_slugs`: #67's invisible entry, one function along."""
    slugs, others, unexaminable = store._scan_session_root()
    assert (slugs, [p.name for p in others], [e.name for e in unexaminable]) == ([HEALTHY], [], [BLOCKED])
    assert "session.json" in unexaminable[0].error and BLOCKED in unexaminable[0].error   # what a user acts on


def test_list_session_slugs_still_answers_only_what_is_known_to_be_a_session(blocked):
    """The contract that must not widen, asserted with one entry in each bucket so an empty scan cannot pass it."""
    (store.session_root() / "not-a-session").mkdir()
    slugs, others, blind = store.scan_session_root()
    assert (slugs, [e.name for e in others], [e.name for e in blind]) == ([HEALTHY], ["not-a-session"], [BLOCKED])
    assert store.list_session_slugs() == [HEALTHY]
    assert [e.name for e in store.list_unexaminable_entries()] == [BLOCKED]


def test_the_repository_exposes_the_third_bucket(blocked):
    """The service layer cannot reach `core.persistence` directly."""
    repo = FileSessionRepository()
    assert repo.list_slugs() == [HEALTHY] and [e.name for e in repo.list_unexaminable()] == [BLOCKED]


# ── session list ──────────────────────────────────────────────────────────────


def test_one_unexaminable_entry_no_longer_takes_the_whole_listing_down(blocked):
    """Invariant 15: the healthy row is complete, the blocked one is named with its reason and states no fact."""
    out, code = run_cli_exit(["session", "list"])
    assert code == EXIT_DEGRADED and "Traceback" not in out
    rows = {name: next(ln for ln in out.splitlines() if name in ln) for name in (HEALTHY, BLOCKED)}
    assert re.search(r"\brev \d", rows[HEALTHY]) and re.search(r"20\d\d-\d\d-\d\dT", rows[HEALTHY])
    assert "could not be read" in rows[BLOCKED] and "session.json" in rows[BLOCKED], rows[BLOCKED]
    assert not re.search(r"\brev \d|20\d\d-\d\d-\d\dT", rows[BLOCKED]), rows[BLOCKED]
    footer = next(ln for ln in out.splitlines() if "could not be read." in ln)
    assert footer.startswith("1 entry could not be read."), footer


def test_json_keeps_every_key_on_the_row_and_claims_nothing(blocked):
    out, code = run_cli_exit(["session", "list", "--json"])
    payload = json.loads(out)
    rows = {r["slug"]: r for r in payload["sessions"]}
    assert (code, payload["degraded"], rows.keys()) == (EXIT_DEGRADED, 1, {HEALTHY, BLOCKED})
    assert rows[HEALTHY].keys() == rows[BLOCKED].keys()
    assert rows[HEALTHY]["readable"] is True and rows[HEALTHY]["revision"] == 0 and rows[HEALTHY]["updated_at"]
    assert rows[BLOCKED]["readable"] is False and rows[BLOCKED]["error"]
    assert (rows[BLOCKED]["revision"], rows[BLOCKED]["provider"], rows[BLOCKED]["updated_at"]) == (None, None, None)


# ── doctor ────────────────────────────────────────────────────────────────────


def test_doctor_reports_the_entry_instead_of_declaring_the_whole_root_unreadable(blocked):
    h = json.loads(run_cli_exit(["doctor", "--json"])[0])["sessions"]
    assert (h["readable"], h["total"], h["error"], h["non_sessions"]) == (True, 1, None, [])
    assert [e["name"] for e in h["unexaminable"]] == [BLOCKED] and h["unexaminable"][0]["error"]
    text, _ = run_cli_exit(["doctor"])
    assert BLOCKED in text and "could not be listed" not in text and "could not be examined" in text.lower()


def test_doctor_keeps_the_whole_root_arm_for_the_case_that_really_is_the_whole_root(monkeypatch):
    """`iterdir()` itself failing is genuinely the whole root (#12): `None`, not `[]`, for what was never looked at."""
    from requivo.deterministic import doctor as det

    def _unreadable():
        raise OSError("boom")

    monkeypatch.setattr(det.store, "scan_session_root", _unreadable)
    h = json.loads(run_cli_exit(["doctor", "--json"])[0])["sessions"]
    assert (h["readable"], h["total"], h["non_sessions"], h["unexaminable"]) == (False, None, None, None)
    assert "boom" in h["error"]
    assert "could not be listed" in run_cli_exit(["doctor"])[0]


def test_a_clean_workspace_says_nothing_about_any_of_this_on_either_surface():
    """The control: no row, the sessions line keeps its tick, `session list` exits zero."""
    _seed_healthy()
    h = json.loads(run_cli_exit(["doctor", "--json"])[0])["sessions"]
    assert (h["unexaminable"], h["readable"], h["total"]) == ([], True, 1)
    text, _ = run_cli_exit(["doctor"])
    line = next(ln for ln in text.splitlines() if ln.startswith("  ") and not ln.startswith("   ") and "sessions" in ln)
    assert "✅" in line and "could not be examined" not in text.lower()
    out, code = run_cli_exit(["session", "list"])
    assert code == 0 and "could not be read" not in out.lower() and HEALTHY in out


def test_an_unexaminable_name_carrying_a_control_character_cannot_forge_a_line(request):
    """#40: the directory name reaches both surfaces raw. `display_token` itself is pinned in test_cli_degraded_listing.py."""
    _seed_healthy()
    hostile = "evil\nTOTAL: 0 sessions, nothing to see"   # no separator: a `/` would nest rather than name
    d = store.session_root() / hostile
    try:
        d.mkdir(parents=True)
    except (OSError, ValueError):
        pytest.skip("this filesystem refuses a directory name containing a newline. UNTESTED HERE: the render "
                    "guard on an unexaminable entry's name.")
    deny_access(d, request, "the render guard on an unexaminable entry's name, on both surfaces")
    for argv in (["session", "list"], ["doctor"]):
        out, _ = run_cli_exit(argv)
        assert not any(ln.startswith("TOTAL:") for ln in out.splitlines()), (argv, out)
        assert "evil" in out, (argv, out)


# ── #97: the same unguarded probe, one function along ────────────────────────


def test_session_exists_answers_could_not_tell_through_the_error_channel(blocked):
    """#82: raise rather than answer `False`; absent is still `False`, because absent is a real answer."""
    with pytest.raises(SessionUnreadableError) as caught:
        store.session_exists(BLOCKED)
    assert caught.value.details["slug"] == BLOCKED
    assert store.session_exists(HEALTHY) is True
    assert store.session_exists("no-such-session-anywhere") is False
    assert store.legacy_exists("no-such-session-anywhere") is False


def test_read_meta_answers_could_not_tell_through_the_error_channel(blocked):
    """#264: the identical class one function further along; absent is still `not found`."""
    with pytest.raises(SessionUnreadableError) as caught:
        store.read_meta(BLOCKED)
    assert caught.value.details["slug"] == BLOCKED
    with pytest.raises(SessionNotFoundError):
        store.read_meta("no-such-session-anywhere")
    assert store.read_meta(HEALTHY).slug == HEALTHY


def test_verify_says_it_could_not_look_and_exits_4_not_1(blocked):
    """#97: `problems: []` spells both *nothing wrong* and *nothing checked*, so `session.checked` exists."""
    text, code = run_cli_exit(["session", "verify", BLOCKED])
    assert code == EXIT_DEGRADED and "Traceback" not in text
    assert "could not examine" in text.lower() and "not a report that it is sound" in text
    text, code = run_cli_exit(["session", "verify", BLOCKED, "--json"])
    payload = json.loads(text)
    assert code == EXIT_DEGRADED and payload["session"]["checked"] is False and payload["session"]["error"]
    assert (payload["ok"], payload["problems"]) == (False, [])
    text, code = run_cli_exit(["session", "verify", HEALTHY, "--json"])   # so the field is not a constant
    assert code == 0 and json.loads(text)["session"] == {"checked": True, "error": None}


# ── #90: the *error* on that line is untrusted text too ──────────────────────


def test_the_error_text_on_a_non_session_line_cannot_forge_a_line_either():
    """`_non_session_detail` interpolates `error` through `display_token`; ordinary text is left alone."""
    forged = "boom\nTOTAL: 0 sessions, all clear"
    for entry in ({"kind": "unknown", "error": forged},
                  {"kind": "directory", "error": forged, "entry_count": 0, "entries": []}):
        detail = _non_session_detail(entry)
        assert "\n" not in detail and "boom" in detail, (entry["kind"], detail)
    assert "[Errno 13] Permission denied" in _non_session_detail({"kind": "unknown", "error": "[Errno 13] Permission denied"})


_UNWRAPPED = re.compile(r"\{([^{}]*\berror\b[^{}]*)\}")


def _fires(line: str) -> bool:
    return any("display_token(" not in m.group(1) for m in _UNWRAPPED.finditer(line))


def test_no_error_string_reaches_a_printed_line_unwrapped():
    """The class guard (#90): fixing the two named instances left four siblings."""
    package = Path(__file__).resolve().parents[1] / "src" / "requivo" / "deterministic"
    modules = sorted(package.rglob("*.py"))
    assert modules, f"the guard found no modules under {package.as_posix()}"
    unwrapped = [f"{path.name}:{n}: {line.strip()[:90]}" for path in modules
                 for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if not line.strip().startswith("#") and ('f"' in line or "f'" in line) and _fires(line)]
    assert not unwrapped, "an error string is interpolated without display_token:\n  " + "\n  ".join(unwrapped)


def test_that_guard_really_fires():
    """MUST-FIRE control for the guard above: the subscript, bare-local and or-expression spellings."""
    assert _fires("""    print(f"x {c['error']}")""") and _fires('    print(f"x {error}")')
    assert _fires("""    print(f"x {entry['error'] or _NO_DETAIL}")""")
    assert not _fires("""    print(f"x {display_token(c['error'])}")""")
    assert not _fires("""    print(f"x {display_token(entry['error'] or _NO_DETAIL)}")""")
    assert not _fires('    print(f"x {slug}")')
