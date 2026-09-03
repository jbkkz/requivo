"""End-to-end tests of `requivo.deterministic.doctor` — the `doctor`, `schema` and `context` verbs.

Split out of `test_cli_deterministic.py` by #141, which mirrored the package #73 created. The shared
harness is `tests/_cli_harness.py`.

Two tests here drive `session list` and the store's own three-way partition rather than `doctor`.
They are the same finding read from the other side — a listing must not grow a row for something
that is not a session — and they share `_lock_ghost` and the #67 narrative with the block above
them, so they stay where that argument is written down.
"""
from __future__ import annotations

import io
import json
import os
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _cli_harness import _run, _run_json

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.errors import InvalidSlugError
from requivo.services.sessions import SessionService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


# ── doctor ──────────────────────────────────────────────────────────────────────


def test_doctor_runs_without_api_key(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    # Since #334 the credential guard asks the SDK, which also discovers an active profile on disk --
    # so clearing the environment is no longer enough to describe a credential-free install: on a
    # machine whose developer has one, these tests would read that. The SDK's discovery entry point
    # is neutralised too. `raising=False` because the older majors in `anthropic>=0.42.0,<2` have no
    # such chain. The full reasoning, including why `ANTHROPIC_CONFIG_DIR` is not the lever it looks
    # like, is on `_no_credentials` in `tests/test_provider.py`.
    monkeypatch.setattr("anthropic._client.default_credentials", lambda **kw: None, raising=False)
    r = _run_json(["doctor", "--json"])
    assert r["schema"]["ok"] and r["schema"]["slots"] > 0
    # Missing key / SDK must never be reported as a hard failure.
    assert r["provider_anthropic"]["api_key_present"] is False
    assert "sessions" in r["workspace"]


def test_doctor_reports_the_model_source_as_env_when_requivo_model_is_set(workspace, monkeypatch):
    """#268 renamed the model override's primary name, and `doctor_report()` used to decide
    `model.source` by reading bare `MODEL` a second time rather than asking `current_model_name()`
    how it actually resolved -- so a reporter who set only `REQUIVO_MODEL` (the name every doc now
    teaches) would have `doctor` call their override "default", the exact "right until the day the
    default moved" drift the comment above the old check named as the risk of a second copy.
    """
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    r = _run_json(["doctor", "--json"])
    assert r["model"] == {"name": "claude-opus-4-8", "source": "env"}


def test_doctor_reports_a_bearer_token_as_a_credential_present(workspace, monkeypatch):
    """#332: doctor read `ANTHROPIC_API_KEY` alone while the runner (`new_client`) also accepts
    `ANTHROPIC_AUTH_TOKEN` (#201), so a working bearer-token install reported
    `api_key_present: false`. Paired with `test_doctor_runs_without_api_key` above, whose negative
    assertion this positive one keeps honest -- a probe that always said "present" would pass that
    test too.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-whatever")
    r = _run_json(["doctor", "--json"])
    assert r["provider_anthropic"]["api_key_present"] is True


# ── #365: a second reader wants more than the bool ──────────────────────────

_NEEDS_CHAIN = pytest.mark.skipif(
    not hasattr(__import__("anthropic._client", fromlist=["default_credentials"]), "default_credentials"),
    reason=(
        "the installed anthropic SDK has no profile/federation discovery chain (the floor of "
        "`anthropic>=0.42.0,<2` predates it). UNTESTED ON THIS SDK: an unloadable ANTHROPIC_PROFILE "
        "is not reachable to name in doctor's output. See test_provider.py's own _NEEDS_CHAIN for "
        "the full reasoning -- this is the identical gate, one file over."
    ),
)


@_NEEDS_CHAIN
def test_doctor_names_the_remedy_for_an_unloadable_profile_rather_than_no_api_key(workspace, monkeypatch):
    """#365: `doctor` used to call `credential_present()` bare, which flattens "no credential" and "a
    credential that is configured and unloadable" onto the same False -- so the verb whose whole job
    is diagnosing the install told the reader to set an environment variable when the fault was a
    profile file the SDK could not read. Setting the variable would not have fixed it, and the reader
    had no way to learn that from the output.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_PROFILE", "a-profile-that-does-not-exist")

    r = _run_json(["doctor", "--json"])
    assert r["provider_anthropic"]["api_key_present"] is False
    problem = r["provider_anthropic"]["credential_problem"]
    assert problem is not None
    assert "could not load the credential configuration" in problem
    assert "a-profile-that-does-not-exist" in problem, "the SDK's own reason names the profile"

    text = _run(["doctor"])
    assert "no API key" not in _check_line(text, "anthropic"), (
        "the wrong remedy for an unloadable profile -- setting a variable will not fix a file the "
        "SDK could not read, and nothing here tells the reader that"
    )
    assert "could not be loaded" in text
    assert "a-profile-that-does-not-exist" in text


def test_doctor_still_says_no_api_key_when_none_is_configured_at_all(workspace, monkeypatch):
    """The must-not-fire twin, genuinely reached (not merely asserted against silence): with no
    credential from any source, the existing message is unchanged. Without this, a fix that stopped
    saying "no API key" for every False case would pass the test above and silently break the far
    more common one -- a "must not say X" assertion alone passes on a harness that produces no
    message at all.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("anthropic._client.default_credentials", lambda **kw: None, raising=False)

    r = _run_json(["doctor", "--json"])
    assert r["provider_anthropic"]["api_key_present"] is False
    assert r["provider_anthropic"]["credential_problem"] is None

    text = _run(["doctor"])
    assert "no API key" in _check_line(text, "anthropic")


# ── doctor's own failures must not render as green ticks (#12) ──────────────────
#
# Every test in this block asserts that the *healthy* and the *broken* case produce **different**
# output. A test that only showed the broken case producing something would pass equally well
# against a doctor that reports a problem for everything — and the defect here was never that doctor
# is silent, it is that two of its states are spelled the same way.


def _check_line(text: str, name: str) -> str:
    """The status line for the named doctor check — the one carrying a tick.

    Matched on the two-space indent a check line has, because the indented detail lines beneath it
    mention the same words (`     sessions        <path>` sits right above `  ✅ sessions …`), and a
    tick asserted against the wrong line is an assertion about nothing."""
    return next(ln for ln in text.splitlines()
                if ln.startswith("  ") and not ln.startswith("   ") and name in ln)


def test_doctor_reports_where_the_write_lock_lives(workspace):
    """#113 moved the write lock out of the session directory, and a convention the diagnostic does
    not report is one it answers about the wrong shape.

    `.requivo/locks/` is the one path in a workspace that every write touches and that nothing else
    names. When it is not writable, every verb fails at once with `could not open the write lock for
    session '<slug>'` and no directory to look at, so `doctor` — the verb that answers for the
    install rather than for a session — has to say where it is.

    Additive, and the sibling key is asserted alongside it: `workspace.sessions` is published, and a
    consumer reading it must be unaffected."""
    r = _run_json(["doctor", "--json"])["workspace"]
    assert r["locks"] == str(store.lock_root())
    assert r["sessions"] == str(store.session_root()), "the published key must not have moved"
    assert not store.lock_root().is_relative_to(store.session_root()), (
        "a lock root under the session root would be a permanent non-session entry there")
    assert "locks" in _run(["doctor"]), "the human rendering says it too, not only --json"


def test_doctor_tells_a_loaded_context_dir_from_a_lost_one_and_from_an_unreadable_one(workspace):
    """Three states, three renderings. `available_cards()` failing used to be written into
    `schema["error"]` — a *different* check's field — with `schema["ok"]` left True and the message
    printed nowhere, while the card line printed a tick unconditionally. A wheel that ships `assets/`
    but loses `assets/context/` therefore showed three green ticks and reasoned with no product
    context at all."""
    from requivo.deterministic import doctor as det

    def _unreadable():
        raise OSError("boom")

    healthy = _run_json(["doctor", "--json"])
    assert healthy["context"]["ok"] is True, "fixture is blind: the bundled cards did not load"
    assert healthy["context"]["status"] == "ok"
    assert healthy["context"]["count"] > 0 and healthy["context"]["error"] is None
    healthy_text = _run(["doctor"])

    # (a) the directory is gone — `_card_paths` skips what does not exist and returns nothing.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "available_cards", list)
        empty = _run_json(["doctor", "--json"])
        empty_text = _run(["doctor"])
    assert empty["context"]["ok"] is False
    assert empty["context"]["status"] == "empty"
    assert empty["context"]["count"] == 0
    assert empty["schema"]["ok"] is True and empty["schema"]["error"] is None

    # (b) the directory cannot be read at all — a different answer again, and it must not be
    #     laundered through a neighbouring check's field.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "available_cards", _unreadable)
        broken = _run_json(["doctor", "--json"])
        broken_text = _run(["doctor"])
    assert broken["context"]["ok"] is False
    assert broken["context"]["status"] == "unreadable"
    assert "boom" in (broken["context"]["error"] or "")
    assert broken["schema"]["ok"] is True and broken["schema"]["error"] is None, (
        "a context-card failure must not be reported as a schema failure")

    # The human rendering distinguishes them too — the JSON being right is no use to a reader
    # counting ticks.
    assert "✅" in _check_line(healthy_text, "context cards")
    assert "✅" not in _check_line(empty_text, "context cards")
    assert "✅" not in _check_line(broken_text, "context cards")
    assert "boom" in broken_text, "the captured error was never shown to the reader"
    assert healthy_text != empty_text and empty_text != broken_text


def test_doctor_tells_an_empty_workspace_from_an_unreadable_one(workspace):
    """`_session_health` caught every exception and returned `{"total": 0, "inconsistent": {}}` —
    byte-identical to a genuinely empty workspace. Twelve unreachable sessions then read as "you have
    no sessions", and the user concludes they were deleted rather than that a directory is
    unreadable."""
    from requivo.deterministic import doctor as det

    def _unreadable():
        raise PermissionError("Permission denied")

    empty = _run_json(["doctor", "--json"])["sessions"]
    assert empty["total"] == 0 and empty["readable"] is True and empty["error"] is None
    assert empty["non_sessions"] == [], "we looked and there was nothing else here"
    empty_text = _run(["doctor"])

    with pytest.MonkeyPatch.context() as mp:
        # `scan_session_root`, because that is the one listing `_session_health` makes since #67.
        # Patching `list_session_slugs` — which it no longer calls — left this simulating nothing
        # while still asserting; the failure is what said so, which is the point of asserting that
        # the two renderings *differ* rather than that the broken one says something.
        mp.setattr(det.store, "scan_session_root", _unreadable)
        unreadable = _run_json(["doctor", "--json"])["sessions"]
        unreadable_text = _run(["doctor"])
    assert unreadable["readable"] is False
    assert unreadable["total"] is None, "0 is a claim about the workspace; we could not look"
    assert unreadable["non_sessions"] is None, (
        "an empty list here reads as `we looked and found nothing else` — the same conflation one "
        "key along, in the arm where the root could not be listed at all (#67)")
    assert "Permission denied" in (unreadable["error"] or "")

    assert "✅" in _check_line(empty_text, "sessions")
    assert "✅" not in _check_line(unreadable_text, "sessions")
    assert "0 in this workspace" in empty_text
    assert "0 in this workspace" not in unreadable_text
    assert "unreadable" in unreadable_text and "Permission denied" in unreadable_text


def _deny_read(directory: Path) -> None:
    """Make `directory` genuinely unreadable, or skip loudly naming what went untested.

    `chmod 000` is not a read denial everywhere: Windows ignores POSIX mode bits entirely, and root
    bypasses them. Branching silently on that would leave a test that *passes* on those runs while
    asserting nothing — a green leg nobody re-reads, reporting a coverage it does not have. So it
    skips instead, and says which platform or condition the assertion did not reach."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny reads on Windows — the unreadable-card-directory "
                    "path is untested on this platform")
    directory.chmod(0o000)
    try:
        list(directory.iterdir())
    except OSError:
        return                                  # the denial took: the assertion below is real
    directory.chmod(0o755)
    pytest.skip("chmod 000 did not deny reads here (running as root?) — the "
                "unreadable-card-directory path is untested on this run")


def test_a_card_directory_that_cannot_be_read_is_unreadable_not_empty(workspace, tmp_path):
    """The `unreadable` state has to be reachable by the thing that actually makes a directory
    unreadable, and it was not.

    `_card_paths()` enumerated with `Path.glob("*.md")`, and `glob` **swallows `PermissionError` and
    yields nothing**. So a card directory denied by permissions — the ordinary way one becomes
    unreadable — produced an empty card list and no exception: `doctor` said `empty` (or, with a
    second readable root, a confident `ok` at a smaller count), and a session naming a card in that
    directory was told `unknown_context_card`, whose remedy is "put the card back" when the card is
    right there and merely unreadable. That is #12's own defect class one layer under #12's fix.

    Both halves are here, on the same directory, with only its mode changing.
    """
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "walled-domain.md").write_text("# Walled domain\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _run(["session", "init", "Something.", "--slug", "s", "--context", "walled-domain", "--json"])

        # ── readable: the must-fire control ───────────────────────────────────
        healthy = _run_json(["doctor", "--json"])
        assert healthy["context"]["status"] == "ok"
        assert "walled-domain" in healthy["context_cards"]
        assert healthy["sessions"]["cards_checked"] is True
        assert healthy["sessions"]["unresolved_cards"] == {}

        _deny_read(cards)
        try:
            broken = _run_json(["doctor", "--json"])
            broken_text = _run(["doctor"])
        finally:
            cards.chmod(0o755)

    assert broken["context"]["status"] == "unreadable", (
        "a permission-denied card directory is not an install with no cards; the remedy differs")
    assert broken["context"]["ok"] is False
    assert "walled-domain" not in broken["context_cards"]

    # The session must not be accused of naming a card that does not exist — it does exist, and we
    # could not read it. `checked` false is the honest answer, and it must not read as clean.
    assert broken["sessions"]["cards_checked"] is False
    assert broken["sessions"]["unresolved_cards"] == {}
    assert "✅" not in _check_line(broken_text, "context cards")
    assert "✅" not in _check_line(broken_text, "sessions"), (
        "the sessions line ticked while nobody had checked their product context")
    assert "not checked" in _check_line(broken_text, "sessions")


# ── something under the session root that is not a session (#67) ────────────────
#
# The state under test cannot be produced by the current code: #22 stopped `session_lock` creating
# the session directory it opened `.lock` inside, which is exactly why these are only ever found on
# disk and never in a fresh run. So the fixture builds one by hand. Going through `session_lock`
# instead would assert against a state this version cannot reach, and would go green on the day the
# report stopped working.


def _lock_ghost(name: str = "leave-approval") -> Path:
    """A session directory as an older Requivo left one: the name taken, holding only `.lock`."""
    d = store.session_root() / name
    d.mkdir(parents=True)
    (d / ".lock").touch()
    return d


def test_doctor_names_what_is_under_the_session_root_and_is_not_a_session(workspace):
    """Nothing could see one of these. `list_session_slugs` filters on `session.json`, and `doctor`
    and `session verify` both reason over the slugs it returns, so a directory holding only `.lock`
    reached no verb at all — `doctor` printed a green `0 in this workspace` straight over the top of
    it.

    Both halves are on the same workspace with only the directory appearing, so the finding cannot
    become a line that everybody sees.
    """
    clean = _run_json(["doctor", "--json"])["sessions"]
    assert clean["non_sessions"] == [], "the control: an untouched workspace must produce no finding"
    assert "other entries" not in _run(["doctor"])

    _lock_ghost()
    found = _run_json(["doctor", "--json"])["sessions"]

    # What was found, never what it was taken to mean: there is no `is_lock_ghost` key anywhere. A
    # half-extracted archive is this shape too, and the directory is the only evidence there is.
    assert [e["name"] for e in found["non_sessions"]] == ["leave-approval"]
    entry = found["non_sessions"][0]
    assert entry["kind"] == "directory"
    assert entry["entries"] == [".lock"] and entry["entry_count"] == 1
    assert entry["error"] is None
    assert entry["slug_shaped"] is True, "the name is one `create_session` can be asked for"

    # It is still not a session, and the session count must not quietly absorb it.
    assert found["total"] == 0 and found["readable"] is True
    assert found["inconsistent"] == {}

    text = _run(["doctor"])
    assert "leave-approval" in text and ".lock" in text
    assert "✅" in _check_line(text, "sessions"), "0 sessions is still the honest count"
    assert "🟡" in _check_line(text, "other entries")


def test_the_silent_slug_substitution_the_report_names_is_the_one_that_happens(workspace):
    """The finding is only worth a line because of what it costs, so the cost is pinned rather than
    described. `create_session`'s rename is the only claim on a slug (invariant 11) and it loses to a
    non-empty directory, after which `SessionService` falls through to its hash-suffixed candidate:
    the user gets a session under a name they did not ask for, with nothing saying why."""
    _lock_ghost()
    assert "will not get it" in _run(["doctor"]), "the report names the finding but not its cost"

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    assert meta.slug != "leave-approval"
    assert meta.slug.startswith("leave-approval-")


def test_a_file_where_a_session_name_would_go_costs_the_same_and_is_named_as_a_file(workspace):
    """Swept rather than assumed: the rename onto an existing *file* fails too, `d.exists()` is true,
    and the caller gets the identical substitution. Reporting only directories would have left an
    identical symptom with an identical remedy invisible, so each entry says what it is instead of
    the report assuming they are all directories."""
    store.session_root().mkdir(parents=True)
    (store.session_root() / "leave-approval").write_text("half a download\n", encoding="utf-8")

    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert entry["kind"] == "file"
    assert entry["entries"] is None and entry["entry_count"] is None
    assert entry["error"] is None, "nothing failed here; there is simply nothing to look inside"
    assert entry["slug_shaped"] is True

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    assert meta.slug.startswith("leave-approval-")


def _deny_listing(directory: Path) -> None:
    """Make `directory` traversable but not listable — `--x`, the mode under which `stat` on a child
    succeeds and `iterdir` does not — or skip loudly naming what went untested.

    Deliberately not `chmod 000`, which denies the `session.json` probe in `_scan_session_root` as
    well and so exercises a *different* state: the entry never reaches `_describe_non_session` at
    all, because the partition above it could not decide what the entry is. That is #80, fixed since,
    and it has its own module — `tests/test_unexaminable_entries.py`. What this fixture is for is the
    entry the partition *did* place, whose contents then could not be listed."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny listing on Windows — the entry-level "
                    "could-not-look arm is untested on this platform")
    directory.chmod(0o111)
    try:
        list(directory.iterdir())
    except OSError:
        return                                  # the denial took: the assertion below is real
    directory.chmod(0o755)
    pytest.skip("chmod --x did not deny listing here (running as root?) — the entry-level "
                "could-not-look arm is untested on this run")


def test_a_symlink_is_reported_as_one_and_its_target_is_not_read(workspace, tmp_path):
    """`Path.is_dir()` follows a symlink. So a link at a slug name pointing anywhere else reported
    `kind: "directory"`, and the `iterdir` beneath it listed the **target's** filenames into a report
    about this workspace — an answer about something that is not a directory here, carrying names
    from somewhere the user did not ask about. Found by review; a symlink is a third shape, and this
    module already treats one as the single case a containment guard has to answer for (invariant
    17)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret-project.md").touch()
    store.session_root().mkdir(parents=True)
    try:
        (store.session_root() / "leave-approval").symlink_to(elsewhere, target_is_directory=True)
    except OSError:                            # pragma: no cover - Windows without developer mode
        pytest.skip("this platform refuses an unprivileged symlink — the symlink arm is untested "
                    "on this run")

    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert entry["kind"] == "symlink", "is_dir() follows the link; this answer must not"
    assert entry["entries"] is None and entry["entry_count"] is None
    assert entry["error"] is None, "nothing failed — we declined to follow it"
    assert entry["slug_shaped"] is True, "the name is still taken, whatever it points at"
    assert "secret-project.md" not in _run(["doctor"]), (
        "the target's contents were listed into a report about this workspace")


def test_a_name_too_long_to_be_a_slug_is_not_marked_as_taken(workspace):
    """`slug_shaped` asked `_SLUG_RE` alone, and validity is the pattern **and** the length: an
    81-character kebab-case directory matched the pattern and was marked `[name taken]`, under a
    sentence promising a silent hash-suffixed substitution. `canonical_dir` refuses that name outright
    and loudly instead, so the promise was false in the one direction that matters — it told a reader
    to expect silence from a call that raises. Found by review.

    The 80-character sibling beside it is the must-fire control: same shape, one character shorter,
    and it *is* reachable."""
    over = "a" * (store.MAX_SLUG_LENGTH + 1)
    at_limit = "b" * store.MAX_SLUG_LENGTH
    for name in (over, at_limit):
        (store.session_root() / name).mkdir(parents=True)
        (store.session_root() / name / ".lock").touch()

    by_name = {e["name"]: e for e in _run_json(["doctor", "--json"])["sessions"]["non_sessions"]}
    assert by_name[at_limit]["slug_shaped"] is True, "the control: this one really is reachable"
    assert by_name[over]["slug_shaped"] is False

    # And the claim the flag stands for is the one the code makes: a refusal, not a substitution.
    with pytest.raises(InvalidSlugError):
        store.canonical_dir(over)


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory literally named "
                     "'con' already on disk, which Windows itself refuses to create at the OS level "
                     "regardless of anything Requivo's own code does (see core/persistence.py's "
                     "comment above _RESERVED_DEVICE_NAMES). REASONED, NOT OBSERVED on an actual "
                     "Windows machine; it follows from the documented behaviour #221 already relies "
                     "on for the reserved-name refusal itself. UNTESTED ON WINDOWS: the whole of "
                     "#408, and unreachable there, since a `con` directory can never exist to be "
                     "described in the first place.")
def test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken(workspace):
    """#408. `_describe_non_session` asked `is_slug` for `slug_shaped` -- the unconditional,
    creation-time refusal -- so a `con` directory holding no `session.json` read `slug_shaped:
    False` and `doctor` stayed silent about it. `create_session('con', ...)` disagrees:
    `canonical_dir('con')` reads straight through the very same directory under #372's conditional
    read-time rule, and the rename that follows loses to it -- the exact consequence `[name taken]`
    exists to name.

    `leave-approval` shares the fixture as the must-fire control that must not have broken: an
    ordinary non-reserved taken name was already correctly marked before this fix and must stay
    marked after it."""
    store.session_root().mkdir(parents=True)
    (store.session_root() / "con").mkdir()
    (store.session_root() / "con" / ".lock").touch()   # non-empty: rename(2) would collide, not win
    (store.session_root() / "leave-approval").mkdir()

    by_name = {e["name"]: e for e in _run_json(["doctor", "--json"])["sessions"]["non_sessions"]}
    assert by_name["con"]["slug_shaped"] is True, (
        "create_session would lose its rename to this directory, exactly like any other taken name"
    )
    assert by_name["leave-approval"]["slug_shaped"] is True, "the control: unaffected by the fix"

    text = _run(["doctor"])
    assert "[name taken]" in text

    meta = SessionService().create_session("A request.", slug="con")
    assert meta.slug != "con"
    assert meta.slug.startswith("con-"), "the silent hash-suffixed substitution the hint warns of"


def test_an_empty_directory_is_still_reported_and_still_marked(workspace):
    """The one shape whose cost is platform-dependent, and the report deliberately does not try to be
    clever about it. POSIX `rename(2)` replaces an empty destination, so `create_session` still wins
    the name here; on Windows `os.rename` is `MoveFileEx` without `MOVEFILE_REPLACE_EXISTING` and
    refuses any existing destination, so it does not. `slug_shaped` therefore does not exempt an empty
    directory — a marker that is right on one platform and silently absent on another is a worse
    answer than one that is occasionally conservative.

    Both arms below assert a real outcome. Neither is the vacuous kind of platform branch that
    reports coverage it does not have."""
    store.session_root().mkdir(parents=True)
    (store.session_root() / "leave-approval").mkdir()

    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert entry["kind"] == "directory"
    assert entry["entries"] == [] and entry["entry_count"] == 0
    assert entry["error"] is None, "we looked, and it is empty — not the same as could not look"
    assert entry["slug_shaped"] is True
    assert "an empty directory" in _run(["doctor"])

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    if os.name == "nt":                                     # pragma: no cover - platform-dependent
        assert meta.slug.startswith("leave-approval-"), "os.rename refuses any existing destination"
    else:
        assert meta.slug == "leave-approval", "rename(2) replaces an empty destination directory"


def test_the_name_taken_hint_names_what_import_does_about_it(workspace):
    """#114, and the composition defect it would otherwise have shipped.

    The `[name taken]` hint closed with *which is the only symptom any of this has*. #114 gave that
    state a second and much louder symptom — `session import` refuses the name outright — and a
    diagnostic that still claims there is only one answers confidently against a rule the product no
    longer follows. Neither diff could see it alone: one taught a verb to refuse, the other simply did
    not change.

    The hint is asserted against the code `session import` really raises rather than against a
    remembered spelling, so the two cannot drift apart silently.
    """
    from requivo.core.errors import ImportDestinationOccupiedError

    store.session_root().mkdir(parents=True)
    (store.session_root() / "leave-approval").mkdir()
    hint = _run(["doctor"])

    assert "[name taken]" in hint, "the control: the marked row and its hint really were printed"
    assert ImportDestinationOccupiedError.code in hint, (
        "the hint does not say what `session import` now does about this directory")
    assert "only symptom" not in hint, "the hint still claims the hash-suffixed name is the only one"
    # the older consequence is still true and must not have been dropped on the way
    assert "plus a hash" in hint


def test_an_entry_that_could_not_be_looked_inside_is_not_reported_as_empty(workspace):
    """The third state one level below the one `_session_health` already has: the root listed fine,
    this directory did not. `entries: []` would say we looked and it holds nothing — the one reading
    that makes the finding worthless, since on POSIX a directory holding nothing is the single shape
    that does not cost the caller its slug at all (`rename(2)` replaces an empty destination)."""
    d = _lock_ghost()

    # The must-fire control, on the same directory, with only its mode changing.
    readable = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert readable["entries"] == [".lock"] and readable["error"] is None

    _deny_listing(d)
    try:
        denied = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
        denied_text = _run(["doctor"])
    finally:
        d.chmod(0o755)

    assert denied["kind"] == "directory", "we can stat it; we cannot list it"
    assert denied["entries"] is None and denied["entry_count"] is None
    assert "Permission denied" in (denied["error"] or "")
    assert "empty directory" not in denied_text
    assert "Permission denied" in denied_text


def test_a_name_read_off_disk_cannot_forge_a_line_of_the_report_that_names_it(workspace):
    """#40 in a new render site. The entry's own name and the names it holds are both read off disk,
    untrusted exactly as a stored context-card name is. Printed bare, one carrying a newline does not
    merely look odd: it ends the line and starts another at whatever column it chooses, immediately
    under a row of `doctor`'s own output."""
    d = _lock_ghost()
    try:
        (d / "x\n  ✅ forged          all clear").touch()
    except OSError:                            # pragma: no cover - filesystem-dependent
        pytest.skip("this filesystem refuses a newline in a filename (Windows, notably) — the "
                    "escaping of an entry name is untested on this run")

    text = _run(["doctor"])
    assert "\\n" in text, "the newline reached the terminal unescaped"
    assert "  ✅ forged          all clear" not in text.splitlines()

    # `--json` was never affected and must stay that way: json.dumps escapes a control character
    # before it can reach a line of its own, so the finding keeps its bytes verbatim.
    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert any("\n" in n for n in entry["entries"])

    # The other permutation, on the entry's *own* name rather than a name it holds. The two reach
    # the report through different f-strings, so one covering the other is an assumption.
    (store.session_root() / "y\n  ✅ forged          all clear").mkdir()
    both = _run(["doctor"])
    assert both.count("\\n") >= 2, "the directory's own name reached the terminal unescaped"
    assert "  ✅ forged          all clear" not in both.splitlines()


def test_a_forged_name_that_holds_a_session_json_cannot_forge_a_line_either(workspace):
    """The permutation the tests either side of this one do not reach, and the gap was real.

    The test above forges a **non-session** name — a bare file, and a directory with no
    `session.json` — so it exercises `_print_non_sessions`, which escapes. The card-name tests further
    down forge a *card* under a legitimate slug. Neither drives a control-charactered name that
    **holds a `session.json`**, and that is the one landing in the *sessions* bucket:
    `_scan_session_root` partitions on `(p / "session.json").exists()` alone, and `_session_health`'s
    `except Exception` turns a name `validate_slug` would refuse into an ordinary `unreadable` row.

    Reproduced against `main` before the fix — the name wrote two further lines of `doctor`'s own
    report at column 0, indented exactly like real rows. Found by the pre-1.0 release audit, which
    reasoned the reachability from four code locations and said it had not executed it; running the
    repro is what settled it.
    """
    forged = "evil\n     └─ ok: all clear"
    try:
        d = store.session_root() / forged
        d.mkdir(parents=True)
    except OSError:                            # pragma: no cover - filesystem-dependent
        pytest.skip("this filesystem refuses a newline in a filename (Windows, notably) — the "
                    "escaping of a session name is untested on this run")
    (d / "session.json").write_text('{"not": "valid session metadata"}', encoding="utf-8")

    text = _run(["doctor"])
    lines = text.splitlines()

    # must fire: the entry really did reach the *sessions* bucket rather than the non-session one.
    # Without this the assertions below would pass against a report that never mentioned it at all.
    assert any("inconsistent" in ln for ln in lines), text

    assert "\\n" in text, "the newline reached the terminal unescaped"
    assert "     └─ ok: all clear`" not in lines, "a stored name wrote a line of doctor's report"
    # One row for one entry. The forged text is built to look like a second, so counting the rows is
    # what separates *escaped* from *merely reordered*.
    assert len([ln for ln in lines if ln.startswith("     └─ ")]) == 1, text


def test_session_list_does_not_call_one_of_these_a_session(workspace):
    """The other half of the partition, and why this is not `session list`'s finding to report: a
    listing of sessions must not grow a row for something that is not one. The real session beside it
    is the must-fire control — without it this passes against a listing that lists nothing at all."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()

    rows = _run_json(["session", "list", "--json"])["sessions"]
    assert [r["slug"] for r in rows] == ["real"]
    assert store.list_session_slugs() == ["real"]

    text = _run(["session", "list"])
    assert "real" in text and "leave-approval" not in text


def test_the_parts_of_the_session_root_are_one_partition(workspace):
    """The three parts of the session root come out of one predicate, and are only worth having as a
    set while nothing can fall between them — a name in none of them is precisely the state #67 is
    about.

    Three parts rather than two since #80: the predicate can *fail*, and an entry it could not
    decide about belongs in neither of the other two. The third is empty in this fixture and
    asserted as empty for that reason — it is populated in `tests/test_unexaminable_entries.py`,
    which needs a platform skip this test does not.

    Read through `scan_session_root` since #300, which is what `doctor` itself calls and, since the
    production-dead `list_non_session_entries` was deleted, the only way to the second part at all.
    That also makes the partition claim stronger than it was: taking the three from one listing is
    the shape the assertions below are actually about, where three separate scans were three
    instants and could have agreed by luck.

    Staging directories are in none of the three on purpose: they are `create_session` in flight
    rather than something left behind, and reporting one is a race the reader cannot act on."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()
    (store.session_root() / ".real.new-1-abcdef12").mkdir()

    scanned_slugs, scanned_others, scanned_blind = store.scan_session_root()
    slugs = set(scanned_slugs)
    others = {e.name for e in scanned_others}
    blind = {e.name for e in scanned_blind}
    on_disk = {p.name for p in store.session_root().iterdir()}

    # must fire: the slugs half of the scan agrees with the dedicated reader, so the partition
    # asserted below is over the same names every other call path sees.
    assert slugs == set(store.list_session_slugs())

    assert slugs == {"real"} and others == {"leave-approval"}
    assert blind == set(), "nothing here is unexaminable; the populated case is its own module"
    assert slugs & others == set() and slugs & blind == set() and others & blind == set()
    assert on_disk - (slugs | others | blind) == {".real.new-1-abcdef12"}


def test_doctor_and_verify_flag_a_session_whose_context_card_is_gone(workspace, tmp_path):
    """A session's `context_cards` are validated once, at creation. The cards live *outside* the
    session directory, so the answer can change afterwards without the session changing — and since
    `load_context` refuses an unresolvable selection (#13), the session is hard-stopped at its next
    (paid) turn while doctor still calls it healthy.

    Both halves are in this one fixture: the same session, checked twice, with only the card moving.
    """
    cards = tmp_path / "cards"
    cards.mkdir()
    card = cards / "lost-domain.md"
    card.write_text("# Lost domain\n\nSome product context.\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _run(["session", "init", "Something.", "--slug", "s", "--context", "lost-domain", "--json"])

        # ── healthy: the card is where the session left it ────────────────────
        healthy_doctor = _run_json(["doctor", "--json"])["sessions"]
        assert healthy_doctor["unresolved_cards"] == {}
        assert healthy_doctor["inconsistent"] == {}
        healthy_verify = _run_json(["session", "verify", "s", "--json"])
        assert healthy_verify["ok"] is True
        assert healthy_verify["context_cards"]["checked"] is True
        assert healthy_verify["context_cards"]["problem"] is None
        healthy_text = _run(["session", "verify", "s"])

        # ── broken: the card is gone, and nothing else changed ────────────────
        card.unlink()

        broken_doctor = _run_json(["doctor", "--json"])["sessions"]
        assert "s" in broken_doctor["unresolved_cards"]
        assert broken_doctor["unresolved_cards"]["s"]["code"] == "unknown_context_card"
        assert "lost-domain" in broken_doctor["unresolved_cards"]["s"]["details"]["unknown"]
        # It is not an *integrity* problem: the directory still tells the truth about itself.
        assert broken_doctor["inconsistent"] == {}
        assert "✅" not in _check_line(_run(["doctor"]), "sessions")

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit) as e:
            app(["session", "verify", "s", "--json"], client=None)
        assert e.value.code == 1
        report = json.loads(buf.getvalue())
        assert report["ok"] is False
        assert report["problems"] == []            # nothing is wrong *inside* the directory
        assert report["context_cards"]["checked"] is True
        assert report["context_cards"]["problem"]["code"] == "unknown_context_card"

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit):
            app(["session", "verify", "s"], client=None)
        broken_text = buf.getvalue()

    assert healthy_text != broken_text
    assert "lost-domain" in broken_text and "lost-domain" not in healthy_text
    assert "REQUIVO_CONTEXT_DIR" in broken_text, "the reader is not told how to recover"


def test_doctor_reports_a_locked_session_as_could_not_check_not_as_broken(workspace, monkeypatch):
    """#263/#265, caught by review before this shipped: a first draft of the `SessionLockedError`
    handler in `_session_health` still built a default-severity `IntegrityProblem`, so `blocking()`
    kept it and it landed straight in `inconsistent` -- driving the identical ❌ glyph a genuinely
    broken session gets, which is exactly the accusation shape this whole issue family exists to
    remove. A lock timeout must land in its own bucket and the warning glyph, never the failure one.

    Must-fire control: the same session, unpatched, still reports ✅ with an empty `inconsistent`."""
    from requivo.core.errors import SessionLockedError
    from requivo.deterministic import doctor as doctor_mod

    _run(["session", "init", "A real one.", "--slug", "locked-one", "--json"])

    healthy = _run_json(["doctor", "--json"])["sessions"]
    assert healthy["inconsistent"] == {}
    assert healthy.get("locked", {}) == {}
    assert "✅" in _check_line(_run(["doctor"]), "sessions")

    real_inspect = doctor_mod.inspect_session

    def locked_for_our_slug(slug):
        if slug == "locked-one":
            raise SessionLockedError(
                "session 'locked-one' is locked by another process; retry in a moment",
                details={"slug": slug})
        return real_inspect(slug)

    monkeypatch.setattr(doctor_mod, "inspect_session", locked_for_our_slug)

    found = _run_json(["doctor", "--json"])["sessions"]
    assert found["inconsistent"] == {}, (
        f"a lock timeout must not be reported as an integrity problem: {found['inconsistent']}")
    assert "locked-one" in found.get("locked", {})

    text = _run(["doctor"])
    sessions_line = _check_line(text, "sessions")
    assert "❌" not in sessions_line, (
        "a session that is merely locked must not earn the same glyph as a broken one")
    assert "🟡" in sessions_line
    assert "locked" in sessions_line.lower()


def test_context_can_be_asked_for_by_session(workspace):
    # A session's card selection is held constant across its turns; a later turn that reads every card
    # reasons from a wider context than the model was built on. Asking by session makes that unmissable.
    _run(["session", "init", "Something.", "--slug", "narrow", "--context", "b2b-platform", "--json"])
    _run(["session", "init", "Something else.", "--slug", "wide", "--json"])
    narrow = _run(["context", "--session", "narrow"])
    wide = _run(["context", "--session", "wide"])
    assert "## b2b-platform" in narrow
    assert len(narrow) < len(wide)          # the subset really is a subset
    assert narrow == _run(["context", "--cards", "b2b-platform"])

    with pytest.raises(SystemExit):         # the two selectors are alternatives
        _run(["context", "--session", "narrow", "--cards", "b2b-platform"])


# ── lock-root residue (#180) ──────────────────────────────────────────────────
#
# #113/#179 moved the write lock outside the session directory to
# `.requivo/locks/<slug>.lock`. `session delete` (#238) unlinks it as the last step of a normal
# delete, but a session removed by hand (`rm -rf`, bypassing that verb) or by an older Requivo with
# no delete verb at all leaves the lock file behind, empty, claiming a slug nobody has any more.
# `doctor` reports that residue the same way #67 reports a non-session entry under the
# session root: what is there, in three states, and never a conclusion the directory alone cannot
# support. `session_lock` only ever creates `<slug>.lock` for a slug that had a session *at that
# instant*, so a lock whose slug currently names no session is candidate residue — never printed as
# "orphan", because the lock scan and the session scan run a moment apart and a session created or
# removed in that gap would read the same way for a tick without being residue at all.


def _take_lock(slug: str) -> None:
    """Materialise `<slug>.lock` on disk the way `session_lock` actually does: enter and leave the
    context manager once. Nothing inside it ever deletes the file it created."""
    with store.session_lock(slug):
        pass


def test_a_clean_workspace_reports_no_lock_residue(workspace):
    """The must-not-fire control: no lock files at all, so the check ticks and nothing is named."""
    r = _run_json(["doctor", "--json"])["locks"]
    assert r == {"readable": True, "error": None, "total": 0, "sessions_checked": True,
                 "unmatched": [], "unexpected": [], "unexaminable": []}
    text = _run(["doctor"])
    assert "✅" in _check_line(text, "locks")
    assert "no matching session" not in text
    assert "orphan" not in text.lower()


def test_a_lock_whose_session_still_exists_is_not_flagged(workspace):
    """The must-fire harness's positive control on the *matched* side: a lock for a live session is
    ordinary, not residue, even though it is the identical file shape as an abandoned one."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 1 and r["unmatched"] == []
    assert "✅" in _check_line(_run(["doctor"]), "locks")


def test_a_session_removed_through_session_delete_leaves_no_lock_residue(workspace):
    """The must-not-fire control against a fresh false positive: unlike the hand-deleted case below,
    `session delete` (#238) unlinks its own `<slug>.lock` as the last step, so it must not show up
    here at all -- the same clean report a workspace with no lock files ever had.

    Windows needed `_LockHandle.unlink_on_release` to get here (#469): the in-lock unlink that works
    on POSIX raises there, because a handle `os.open` opened permits no same-process delete, and the
    old code caught the raise and left the file. Deferring to `session_lock`'s own teardown is
    second-best and stated as such at that class. The window it opens -- release, close, unlink, with
    no Python between them -- is two syscalls wide where holding the lock makes it zero, and in that
    window a `create_session` for the same slug (lock-free by design, invariant 11) followed by a
    third actor's `session_lock` could open the inode this unlink then removes, after which a fourth
    actor's `O_CREAT` mints a new one and two holders each believe they hold the only lock. That is
    the same race `delete_session`'s docstring names, narrowed rather than eliminated; it is not the
    ordering #22 rejected, which put a whole teardown, a return into the caller and a fresh
    `lock_path` validation between the release and the unlink."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    _run(["session", "delete", "s", "--json"])

    r = _run_json(["doctor", "--json"])["locks"]
    assert r == {"readable": True, "error": None, "total": 0, "sessions_checked": True,
                 "unmatched": [], "unexpected": [], "unexaminable": []}
    assert "✅" in _check_line(_run(["doctor"]), "locks")


def test_a_lock_whose_session_was_deleted_by_hand_is_named_but_not_concluded(workspace):
    """The ordinary way this residue still arises, even with `session delete` (#238) unlinking its
    own lock file cleanly: a directory removed by hand (or by an older Requivo) goes, and the lock
    file — outside it since #113 — does not."""
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    shutil.rmtree(store.canonical_dir("s"))

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 1 and r["unmatched"] == ["s"]
    assert r["unexpected"] == [] and r["unexaminable"] == []

    text = _run(["doctor"])
    assert "🟡" in _check_line(text, "locks")
    assert "s" in text and "no matching session" in text
    # The load-bearing refusal (#180): this report never draws the conclusion the directory alone
    # cannot support.
    assert "orphan" not in text.lower()
    assert "leftover" not in text.lower()


def test_an_entry_under_lock_root_that_is_not_a_lock_file_is_named_as_unexpected(workspace):
    """Nothing but `session_lock` writes here, so anything else — a stray file, a subdirectory, a
    misnamed lock — is reported and never silently absorbed into the count of real locks."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "not-a-lock.txt").write_text("stray\n", encoding="utf-8")
    (store.lock_root() / "sub").mkdir()

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 0
    assert sorted(r["unexpected"]) == ["not-a-lock.txt", "sub"]

    text = _run(["doctor"])
    assert "🟡" in _check_line(text, "locks")
    assert "not-a-lock.txt" in text and "sub" in text


def test_an_ordinary_discover_leaves_no_lock_residue_doctor_flags(workspace):
    """#391: `_discovery_guard` (`services/discovery.py`, #209) writes `<slug>.discovering` into
    `lock_root()` and never unlinks it, correctly -- the same POSIX reasoning that leaves
    `session_lock`'s own `.lock` file behind for a deleted session. Before this fix `scan_lock_root`
    had never been taught the second shape, so that file read as `unexpected` -- "not a lock file
    Requivo recognises" -- about a file this release's own code had just written. Asserts the state
    (doctor stays green, the entry is not in `unexpected`), not the wording."""
    from requivo.services.discovery import _discovery_guard_path

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    guard = _discovery_guard_path("s", store.Store(store.workspace_root()))
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.touch()

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["unexpected"] == [], (
        "a file this release's own code wrote must not read as unrecognised residue"
    )

    text = _run(["doctor"])
    assert "✅" in _check_line(text, "locks")
    assert "s.discovering" not in text


def test_a_directory_shaped_like_a_discovery_guard_is_still_unexpected(workspace):
    """The must-fire control paired with the test above: recognising `.discovering` files must not
    become recognising anything ending in that suffix. `_discovery_guard` always opens a regular
    file (`os.open(..., os.O_RDWR | os.O_CREAT, ...)`), never a directory, so a directory at that
    name is not a shape it produces and stays reported."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "s.discovering").mkdir()

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["unexpected"] == ["s.discovering"]

    text = _run(["doctor"])
    assert "🟡" in _check_line(text, "locks")
    assert "s.discovering" in text


def test_a_malformed_discovering_stem_is_still_unexpected(workspace):
    """A `.discovering`-suffixed name whose stem is not a valid slug is not a shape
    `_discovery_guard_path` could ever produce -- it validates the slug before joining the suffix --
    so it stays reported rather than silently swallowed by the new suffix check."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "Not Valid.discovering").write_text("", encoding="utf-8")

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["unexpected"] == ["Not Valid.discovering"]


def test_a_symlink_at_a_lock_name_is_reported_and_not_followed(workspace):
    """The same symlink care `_scan_session_root`'s non-session partition carries (invariant 17): a
    symlink is named as one and its target is never read into this report."""
    if os.name == "nt":
        pytest.skip("os.symlink needs elevated privileges on Windows by default")
    store.lock_root().mkdir(parents=True)
    target = workspace / "elsewhere.txt"
    target.write_text("not a lock\n", encoding="utf-8")
    (store.lock_root() / "sneaky.lock").symlink_to(target)

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 0
    assert r["unexpected"] == ["sneaky.lock"]


def test_a_symlink_at_a_discovery_guard_name_is_reported_and_not_followed(workspace):
    """The `.discovering` sibling of the test above: `is_ordinary_file` (`p.is_file() and not
    p.is_symlink()`) is computed once and shared by both suffix branches (#391), so a symlink at a
    `.discovering` name must fail the same way a symlink at a `.lock` name already does -- reported,
    never followed, never recognised as a guard file `_discovery_guard` could have produced."""
    if os.name == "nt":
        pytest.skip("os.symlink needs elevated privileges on Windows by default")
    store.lock_root().mkdir(parents=True)
    target = workspace / "elsewhere.txt"
    target.write_text("not a guard file\n", encoding="utf-8")
    (store.lock_root() / "sneaky.discovering").symlink_to(target)

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 0
    assert r["unexpected"] == ["sneaky.discovering"]


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory literally named "
                     "'con' already on disk, which Windows itself refuses to create at the OS level "
                     "regardless of anything Requivo's own code does (see core/persistence.py's "
                     "comment above _RESERVED_DEVICE_NAMES). REASONED, NOT OBSERVED on an actual "
                     "Windows machine; it follows from the documented behaviour #221 already relies "
                     "on for the reserved-name refusal itself. UNTESTED ON WINDOWS: the whole of "
                     "#401 and #409, this test and its siblings below -- and unreachable there, "
                     "which is not the same claim and is why it is written out rather than assumed.")
def test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue(workspace):
    """#401, the third instance of #372's sweep gap and #391's defect one predicate over.

    `scan_lock_root` classified a lock-root entry with `is_slug`, which is `validate_slug` -- the
    unconditional, creation-time refusal. So for a session already on disk at a reserved Windows
    device name, both files this store's own code wrote for it read as `unexpected`: "not a lock
    file Requivo recognises... a name here did not come from `session_lock`". The wording is what a
    user reads, so this drives `doctor` rather than `scan_lock_root` alone.

    **Two must-not-fire controls share this fixture** -- a stray file and a malformed stem must
    still be reported as unexpected, so a fix that simply stopped classifying anything would not
    pass. `nul.lock` used to be a third such control, pinning `nul` as still-unexpected with no
    session on disk; #409 corrected that assumption (see the renamed sibling test below for why),
    so it is now the must-fire half instead, asserted below as an ordinary orphaned lock rather
    than residue."""
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    lr = store.lock_root()
    lr.mkdir(parents=True, exist_ok=True)
    (lr / "con.lock").write_text("", encoding="utf-8")          # what `session_lock` writes
    (lr / "con.discovering").write_text("", encoding="utf-8")   # what `_discovery_guard` writes
    (lr / "not-a-lock.txt").write_text("stray", encoding="utf-8")   # control: nothing wrote this
    (lr / "Bad Stem.lock").write_text("", encoding="utf-8")     # control: malformed stem
    (lr / "nul.lock").write_text("", encoding="utf-8")          # reserved, no `nul` session (#409)

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 2, "con's own lock file, and nul's -- both are lock-stem-shaped"
    assert r["unmatched"] == ["nul"], "con's session exists right now; nul's does not"
    assert r["unexpected"] == ["Bad Stem.lock", "not-a-lock.txt"], (
        "the two files this release's own code wrote for `con` must stop being reported as "
        "residue, `nul.lock` moves to `unmatched` rather than staying residue too (#409), and "
        "nothing else may stop being reported with them"
    )

    text = _run(["doctor"])
    assert "con.lock" not in text and "con.discovering" not in text
    assert "not a lock file Requivo recognises" in text  # said about the two remaining controls
    assert "not-a-lock.txt" in text
    assert "nul — no session currently named that" in text


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs files literally named 'nul.lock' "
                     "and 'nul.discovering' on disk, and Windows resolves a reserved device name "
                     "taken before the first dot -- `lock_path`'s own comment states that rule for "
                     "`con.lock`, and `_reserved_stem` enforces it -- so both writes would open the "
                     "NUL device, succeed, and leave nothing for `iterdir` to find. REASONED, NOT "
                     "OBSERVED on an actual Windows machine; it follows from the same documented "
                     "behaviour #221 relies on. UNTESTED ON WINDOWS: that a lock file for a "
                     "reserved stem with no session behind it is recognised, not residue. It is "
                     "also unreachable there -- the file cannot exist, and neither can the session "
                     "the sibling test above needs -- so neither #401 nor #409 changes anything on "
                     "that platform.")
def test_a_lock_file_for_a_reserved_name_with_no_session_on_disk_is_recognised_not_residue(
        workspace):
    """#409, correcting #401's own conditional control -- renamed from
    `..._is_still_unexpected`, which asserted the opposite of what this asserts now.

    #401 shipped `_is_lock_stem` asking `_refuse_new_reserved_slug` against `session_root()/stem`
    -- deliberately, on the reasoning that a shape-only predicate would wrongly recognise `nul.lock`
    with no `nul` session anywhere on disk, "a file no writer here could have produced". That
    reasoning does not hold: the *only* way `session_lock` ever writes a reserved-stem lock file at
    all is a session that occupied the name *at write time* (#372's conditional refusal) -- so a
    `nul.lock` with no `nul` session on disk now is exactly what this store's own code leaves behind
    once that session is later deleted, indistinguishable from any other orphaned lock. Asking
    whether a session currently matches made the classification depend on a directory this function
    does not take as an argument, which is precisely what `scan_lock_root`'s own docstring already
    forbade. Shape (`_is_lock_stem`, #409) is what decides it now, and what a session currently
    matches is `_lock_health`'s separate question -- `unmatched`, not `unexpected`."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "nul.lock").write_text("", encoding="utf-8")
    (store.lock_root() / "nul.discovering").write_text("", encoding="utf-8")

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 1, "nul.lock is a recognised lock; nul.discovering is not a lock at all"
    assert r["unmatched"] == ["nul"], "an ordinary orphaned lock -- no session claims it right now"
    assert r["unexpected"] == [], "not residue: a reserved stem is still a shape this store writes"


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory and lock files "
                     "literally named 'nul' / 'nul.lock' / 'nul.discovering' on disk, which "
                     "Windows itself refuses to materialise (see the sibling tests above). "
                     "REASONED, NOT OBSERVED on an actual Windows machine. UNTESTED ON WINDOWS: "
                     "the core mechanism #409 fixes, and unreachable there since a reserved-name "
                     "session can never exist to be deleted in the first place.")
def test_a_reserved_lock_stems_classification_survives_the_session_being_deleted(workspace):
    """#409's own mechanism, reproduced end to end: a lock file's provenance is a fact about the
    past, fixed the moment `session_lock` writes it, and its classification must not move when the
    directory that fact refers to is deleted afterwards.

    `nul.lock` is written by the real `session_lock`, against a real `nul` session -- the only way
    #372's conditional refusal ever lets a reserved-name lock exist -- and left behind exactly as
    `session_lock`'s own docstring says every lock file is (never unlinked). The must-fire control
    is the `before` snapshot, taken while the session still exists: without it, a fix that always
    answered `unmatched` regardless of the session's presence would still pass."""
    d = store.session_root() / "nul"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "nul", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    with store.session_lock("nul"):
        pass    # writes lock_root()/'nul.lock' via the real code path; never unlinks it on exit

    assert (store.lock_root() / "nul.lock").exists()

    before = _run_json(["doctor", "--json"])["locks"]
    assert before["total"] == 1 and before["unmatched"] == [], "the nul session still exists"
    assert before["unexpected"] == [], "the must-fire control: recognised while the session is live"

    shutil.rmtree(d)   # the session is gone; the lock file's provenance has not changed

    after = _run_json(["doctor", "--json"])["locks"]
    assert after["total"] == 1, "the lock file's own shape has not changed"
    assert after["unmatched"] == ["nul"], "an orphaned lock, exactly like any other -- not residue"
    assert after["unexpected"] == [], (
        "must not read as 'not a lock file Requivo recognises' just because nul's session is gone"
    )


@pytest.mark.skipif(os.name == "nt", reason="os.symlink needs elevated privileges on Windows by "
                    "default, and the fixture also needs a file literally named 'a.lock', which is "
                    "fine there -- the symlink is the blocker, not the name. UNTESTED ON WINDOWS: "
                    "that a stray symlink beside a guard file does not change how the guard file "
                    "itself is classified.")
def test_a_symlink_at_the_lock_name_does_not_sink_the_guard_file_beside_it(workspace):
    """A verdict about one entry must not be decided by a sibling entry's state (#401, found in
    review before the fix shipped).

    `_is_lock_stem` was first written as `lock_path(stem)` in a `try` -- the tidier "one rule, one
    place", and wrong, because `lock_path`'s third check is about a *path*: it ends with
    `is_contained(root / (stem + '.lock'), root)`. Asked the `.discovering` question it answered
    about a different file, so this fixture -- a real guard file, and an unrelated symlink at the
    `.lock` name pointing outside the root -- reported `a.discovering` as residue nobody
    recognises. That is invariant 17's shape one layer down, and it is the very symptom #401
    exists to remove, reproduced by the fix for it.

    `a.lock` itself is still reported: it is a symlink, which no writer here produces. `b.discovering`
    is the must-fire pair -- an identical guard file with no sibling at all, which must be recognised
    in the same scan, so this cannot pass by classifying nothing."""
    store.lock_root().mkdir(parents=True)
    outside = workspace / "elsewhere.txt"
    outside.write_text("not a lock", encoding="utf-8")
    (store.lock_root() / "a.discovering").write_text("", encoding="utf-8")
    (store.lock_root() / "a.lock").symlink_to(outside)
    (store.lock_root() / "b.discovering").write_text("", encoding="utf-8")

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["unexpected"] == ["a.lock"], (
        "only the symlink is a shape no writer here produces; both guard files are ordinary files "
        "`_discovery_guard` really writes"
    )
    assert r["total"] == 0  # a `.discovering` file is not a lock, and the symlink is not one either


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a file literally named 'con.lock' "
                    "on disk, and Windows resolves a reserved device name taken before the first "
                    "dot, so the write would open the CON device and leave nothing for `iterdir` "
                    "to find (`lock_path`'s own comment states that rule). REASONED, NOT OBSERVED "
                    "on an actual Windows machine. UNTESTED ON WINDOWS: that `_is_lock_stem` never "
                    "reaches `_probe` for a reserved stem any more -- and unreachable there, since "
                    "only a reserved stem was ever routed through it.")
def test_a_reserved_lock_stem_no_longer_probes_the_session_root(workspace, monkeypatch):
    """#409 removed `_is_lock_stem`'s call into `session_root()` entirely -- shape is all it asks
    now. This used to be the second source of `locks.unexaminable` (#401, superseded here): a
    session root the process could not stat into degraded one lock entry rather than the whole
    scan. That source is gone by design, not by accident, and this pins it -- even a `_probe` that
    would raise for every call must not stop `con.lock` classifying cleanly, because `_is_lock_stem`
    never reaches it any more."""
    store.lock_root().mkdir(parents=True)
    (store.lock_root() / "con.lock").write_text("", encoding="utf-8")

    def _always_raises(marker, slug):
        raise store.SessionUnreadableError(
            f"could not determine whether session {slug!r} exists: Permission denied")

    monkeypatch.setattr(store, "_probe", _always_raises)

    r = _run_json(["doctor", "--json"])["locks"]
    assert r["total"] == 1 and r["unmatched"] == ["con"]
    assert r["unexpected"] == [] and r["unexaminable"] == []


def test_the_lock_root_being_unlistable_is_not_reported_as_no_residue(workspace):
    """The same third state every other check in this report has: could-not-look must not render
    like looked-and-found-nothing."""
    from requivo.deterministic import doctor as det

    clean = _run_json(["doctor", "--json"])["locks"]
    assert clean["readable"] is True and clean["total"] == 0
    clean_text = _run(["doctor"])

    def _unreadable():
        raise PermissionError("Permission denied")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det.store, "scan_lock_root", _unreadable)
        broken = _run_json(["doctor", "--json"])["locks"]
        broken_text = _run(["doctor"])

    assert broken["readable"] is False
    assert broken["total"] is None and broken["unmatched"] is None
    assert "Permission denied" in (broken["error"] or "")
    assert "✅" in _check_line(clean_text, "locks")
    assert "✅" not in _check_line(broken_text, "locks")
    assert "unreadable" in broken_text


def test_a_lock_for_a_session_that_exists_but_is_unexaminable_is_not_claimed_as_unmatched(workspace):
    """`list_slugs()` answers *confirmed sessions* alone (#80's own distinction): a directory whose
    `session.json` probe raised EACCES lands in `list_unexaminable()`, not there. `_lock_health`
    used to check only `list_slugs()`, so a session sitting right there but merely unreadable told
    the identical false story the `sessions` check next to it exists to refuse: "no session
    currently named that" about a slug the workspace cannot confirm is empty. Found by review."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny reads on Windows")
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")
    d = store.canonical_dir("s")
    d.chmod(0o000)
    try:
        # The same root guard the sibling fixtures in tests/test_unexaminable_entries.py carry, and
        # the reason it is needed here too (#298): a runner whose process can read a 0o000 directory
        # makes the must-fire control below assert something the platform did not do. It fired on
        # the py3.14 leg the moment that leg existed, on a test nothing about 3.14 touches.
        try:
            (d / "session.json").exists()
        except PermissionError:
            pass
        else:
            pytest.skip("chmod 000 did not deny the session.json probe on this run (running as "
                        "root?). UNTESTED HERE: that a lock for a session the workspace could not "
                        "examine is not claimed as unmatched residue.")
        r = _run_json(["doctor", "--json"])
        text = _run(["doctor"])
    finally:
        d.chmod(0o755)

    assert r["sessions"]["unexaminable"] and r["sessions"]["unexaminable"][0]["name"] == "s", (
        "the must-fire control: the sessions check really does see this as unexaminable"
    )
    assert r["locks"]["unmatched"] == [], (
        "a session the workspace could not examine is not confirmed absent, so its lock must not "
        "be reported as residue from one that no longer exists"
    )
    assert "no matching session" not in text
    assert "✅" in _check_line(text, "locks")


def test_lock_matching_is_not_claimed_when_the_session_list_itself_could_not_be_read(workspace):
    """`unmatched` answers a question that needs the current session list, and a failure to read
    *that* is a third state of its own: not `readable: False` (the lock root scan itself worked) and
    not an empty `unmatched` (which would claim every lock was checked and matched)."""
    from requivo.deterministic import doctor as det

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _take_lock("s")

    def _unreadable(self):
        raise PermissionError("Permission denied")

    with pytest.MonkeyPatch.context() as mp:
        # On the `Store` class, not the module function (#272): `_lock_health` reaches this through
        # `SessionService().repo.list_slugs()` -> `FileSessionRepository.list_slugs()`, which calls
        # `self._resolve_store().list_session_slugs()` -- a `Store` method lookup, not the
        # module-level `list_session_slugs` name -- so patching the module function no longer
        # intercepts it.
        mp.setattr(det.store.Store, "list_session_slugs", _unreadable)
        r = _run_json(["doctor", "--json"])["locks"]
        text = _run(["doctor"])

    assert r["readable"] is True and r["total"] == 1
    assert r["sessions_checked"] is False
    assert r["unmatched"] is None, "0 or [] here would claim the lock was checked against sessions"
    assert "🟡" in _check_line(text, "locks")
    assert "not checked" in text
