"""Guards in `core/persistence/` -- the second half of the split #550 made of this file.

See `test_persistence_guards.py`'s module docstring for the seam and the full list of what lives on
each side. This half: `#238` (session delete), `#36` (the two display-only path joins), the
slug/model-loader group `#72` added at the foot, and `#261` (what a half-finished `save_revision`
leaves behind).

The fixtures and small helpers below are duplicated from the other half rather than imported across
the two test modules -- a handful of lines each, and a cross-module import between two files that
exist only to stay under a line-count ceiling would be a second, thinner coupling neither module
otherwise has any reason to carry.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import threading
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

# The one control in this repo that can actually move the ambient default encoding, measured rather
# than assumed. Borrowed rather than restated: two copies of a probe like this drift, and the copy
# that drifts is the one that silently stops firing.
from requivo.cli import _build_parser, _wrote
from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import ModelUnreadableError, RequivoError, SessionNotFoundError
from requivo.core.integrity import check_session
from requivo.core.persistence import derive_slug, load_model

# `_acquire`/`_release`/`_LOCK_TIMEOUT_SECONDS` moved to `core/persistence/lock.py` by #550, and
# `Store.session_lock` (in `lock.py`'s own `_LockMixin`) reads them off *that* module's globals --
# patching the package-level re-export (`store._acquire`) is a second binding that does not reach
# the call site, so the lock-behaviour tests below patch this module directly instead.
from requivo.core.persistence import lock as store_lock

# Same reasoning, for `_atomic_write`: `Store.save_revision`/`create_session`/`write_meta` (in
# `core/persistence/store.py`) import it from `atomic.py` and call the bare name, which resolves in
# `store.py`'s own globals -- not the package-level re-export, and not `atomic.py`'s own, either.
from requivo.core.persistence import store as store_module
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService


def _slot(completeness=0, confidence="empty", impact="low", value=""):
    return {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}


def _full_model(**overrides) -> dict:
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))  # isolate the legacy root too
    return tmp_path



def _session(slug: str) -> FileSessionRepository:
    """A session at revision 1, plus the repository an external consumer would hold."""
    svc = SessionService()
    svc.create_session("Something.", slug=slug)
    svc.update_model(slug, _full_model())
    return FileSessionRepository()



# Every shape that is not a filename: traversal, a bare dot segment, both separators, an absolute
# path, a dot-prefixed name (which the staging convention and `list_session_slugs` reserve), and the
# empty string. A backslash is a separator on Windows and an ordinary character on POSIX, so it is
# refused on both rather than only on the one where it happens to escape.
ESCAPES = [
    "../../../../ESCAPED.md",
    "..",
    "sub/nested.md",
    "sub\\nested.md",
    "/etc/passwd",
    ".hidden.md",
    "",
]



# ── #238: session delete ──────────────────────────────────────────────────────
#
# `Store.delete_session` removes a session's directory and its lock file, under the same lock every
# other compound mutation takes (invariant 9) -- the lock file itself lives outside the session
# directory since #113, so the directory removal is the write a concurrent writer has to serialise
# against, and unlinking the lock file is best-effort cleanup that runs only after the lock is fully
# released (see the method's own docstring for why that ordering, not "while still held", was
# chosen). Invariant 11's claim on a slug is `create_session`'s rename either way, so a slug a delete
# just freed has to be claimable again exactly as if nothing had ever occupied it.


def test_delete_session_removes_the_directory_and_the_lock_file(workspace):
    """The uncontended positive control every negative test below needs: deleting a session that
    nothing else is touching must actually remove both the directory and the lock file the issue's
    own acceptance criteria name, not merely refuse to error."""
    svc = SessionService()
    svc.create_session("A real request.", slug="gone-soon")
    svc.update_model("gone-soon", _full_model())
    assert store.canonical_dir("gone-soon").exists()
    assert store.lock_path("gone-soon").exists()

    store.delete_session("gone-soon")

    assert not store.canonical_dir("gone-soon").exists()
    assert not store.lock_path("gone-soon").exists()
    assert "gone-soon" not in store.list_session_slugs()


def test_deleting_a_nonexistent_slug_is_refused_with_session_not_found(workspace):
    with pytest.raises(RequivoError) as ei:
        store.delete_session("never-existed")
    assert ei.value.code == "session_not_found"
    # Refusing must not conjure anything -- no directory, no lock file, for a slug nothing ever
    # claimed (the same must-not-fire shape `test_a_lock_on_a_slug_with_no_session_leaves_no_trace`
    # already pins for the lock alone).
    assert not store.canonical_dir("never-existed").exists()
    assert not store.lock_path("never-existed").exists()


def test_deleting_then_recreating_the_same_slug_succeeds(workspace):
    """The issue's own acceptance criterion, verbatim: the slug claim is genuinely released.
    Invariant 11's claim on a slug is `create_session`'s rename -- if delete left anything behind
    that rename could lose to (a ghost directory, a stale lock treated as an occupant), re-creating
    the identical slug would either fail or silently inherit residue from the deleted session."""
    svc = SessionService()
    svc.create_session("The first occupant of this slug.", slug="reused")
    svc.update_model("reused", _full_model(**{"problem": _slot(80, "explicit", "high", "FIRST")}))
    store.delete_session("reused")

    meta = svc.create_session("A completely different request.", slug="reused")
    assert meta.current_revision == 0
    assert store.session_request("reused") == "A completely different request."
    assert store.list_session_slugs() == ["reused"]


def test_a_writer_racing_an_in_flight_delete_is_refused_rather_than_writing_into_a_half_removed_directory(
        workspace, monkeypatch):
    """The issue's own acceptance criterion: a delete racing a concurrent writer must not leave a
    half-removed directory. Forced into a deterministic ordering rather than raced for, the same
    technique `test_a_session_deleted_before_the_lock_is_granted_is_refused` already uses for the
    identical shape one call away: `_acquire` is patched to signal a waiting writer thread only once
    the delete already holds the lock, so the writer is guaranteed to contend for the *same* lock the
    delete is mid-critical-section on, never to win it first by chance.

    Invariant 9's guarantee is that the writer, once it does get the lock, meets a world where the
    session is already gone -- refused with `session_not_found`, never a directory a `rmtree` had
    only partly cleared. This is the must-conflict half; the paired must-succeed half is
    `test_delete_waits_for_a_concurrent_writer_then_removes_what_it_wrote` below."""
    SessionService().create_session("A real request.", slug="racer")
    writer_may_start = threading.Event()
    real_acquire = store_lock._acquire

    def signalling_acquire(fd, slug):
        result = real_acquire(fd, slug)
        writer_may_start.set()   # the delete now holds the lock; let the writer contend for it
        return result

    monkeypatch.setattr(store_lock, "_acquire", signalling_acquire)

    outcome: dict = {}
    writer_finished = threading.Event()

    def write_after_signal():
        writer_may_start.wait(timeout=5)
        try:
            with store.session_lock("racer"):
                pass  # pragma: no cover - must never be entered; the session is already gone
            outcome["ok"] = True
        except RequivoError as e:
            outcome["ok"] = False
            outcome["code"] = e.code
        finally:
            writer_finished.set()

    t = threading.Thread(target=write_after_signal, daemon=True)
    t.start()
    store.delete_session("racer")
    assert writer_finished.wait(timeout=5), "the racing writer never finished"

    assert outcome.get("ok") is False, f"a writer racing an in-flight delete must be refused: {outcome}"
    assert outcome["code"] == "session_not_found"
    assert not store.canonical_dir("racer").exists()


def test_the_lock_file_is_gone_before_the_lock_is_released_not_after(workspace, tmp_path,
                                                                    monkeypatch):
    """Found in review: the first draft unlinked the lock file *after* `session_lock`'s own release,
    which reopens the exact "unlinking a lock file a concurrent process may be holding" hazard
    `session_lock`'s own docstring says #22 rejected as a repair -- because invariant 11 lets a
    second actor `create_session` the identical slug the instant `session_exists` goes false, which
    is the moment `rmtree` returns, still inside this method's own critical section. A `session_lock`
    taken on that re-created slug *after* a release-then-unlink ordering would open a fresh inode at
    the just-vacated path and flock it uncontended -- a second "exclusive" holder the first ordering's
    own fd (still open on the old inode) never contends with. Unlinking while still holding the lock
    does not need a live race to prove: it only needs the unlink to have already happened by the
    moment `_release` runs, which this test observes directly rather than trying to win a footrace
    against the store's own critical section."""
    SessionService().create_session("A real request.", slug="ordered")
    lock_path = store.lock_path("ordered")
    real_release = store_lock._release
    observed: dict = {}

    def observing_release(fd):
        observed["lock_file_existed_at_release"] = lock_path.exists()
        return real_release(fd)

    monkeypatch.setattr(store_lock, "_release", observing_release)

    store.delete_session("ordered")

    if _platform_unlinks_a_file_it_still_holds(tmp_path):
        assert observed.get("lock_file_existed_at_release") is False, (
            "the lock file must already be gone by the time the lock is released, not unlinked "
            "afterwards -- see delete_session's own docstring for why the other ordering is unsafe")
    else:
        # The third state, and it is a claim rather than a shrug (#469). Where the platform refuses a
        # same-process unlink of a held file, the zero-window ordering above is not merely untested --
        # it is unreachable, and asserting it here would redden a store behaving as correctly as the
        # platform permits. What must still hold is that the file does not survive the call, which the
        # assertion below states for both branches. The fallback path's own guard,
        # test_a_delete_whose_in_lock_unlink_is_refused_still_removes_the_lock_file, stages this
        # refusal on every platform, so nothing about it goes unexercised on the legs that pass here.
        assert observed.get("lock_file_existed_at_release") is True, (
            "this platform refuses to unlink a file it still holds, so the in-lock unlink cannot "
            "have succeeded -- if it did, this probe is measuring the wrong thing")
    assert not lock_path.exists()
    assert not store.canonical_dir("ordered").exists()


def _platform_unlinks_a_file_it_still_holds(tmp_path) -> bool:
    """Does this platform permit unlinking a file this process holds an open fd on?

    Measured, not derived from `sys.platform`: the question is what the filesystem and the open mode
    actually allow here, and a name-based guess is the shape invariant 17's sequel was written about
    -- a check whose answer depends on where it runs rather than on what it looked at."""
    probe = tmp_path / "held.probe"
    fd = os.open(probe, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        probe.unlink()
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
        probe.unlink(missing_ok=True)


def test_a_delete_whose_in_lock_unlink_is_refused_still_removes_the_lock_file(workspace, tmp_path,
                                                                              monkeypatch):
    """#469. On Windows `os.open` takes no share-delete, so `delete_session`'s unlink of the lock
    file it is still holding is refused every time -- not "can raise instead", which is how the
    method's docstring first put it. The old code swallowed that `OSError` and left the file, so
    every Windows `session delete` left residue `doctor --json` then reported as `unmatched`: the
    install diagnostic accusing the user of a state Requivo had just created.

    Staged here on every platform rather than left to the one leg that reaches it naturally, because
    a fallback exercised only by Windows CI is a fallback whose next regression is found by Windows
    CI. The first unlink of the lock path raises, later ones pass -- which is exactly the platform's
    rule: refused while the handle is open, allowed once it is closed."""
    svc = SessionService()
    svc.create_session("A real request.", slug="refused")
    svc.update_model("refused", _full_model())   # create_session is lock-free (invariant 11); an
    lock_path = store.lock_path("refused")       # apply is what actually mints the lock file
    assert lock_path.exists()

    real_unlink = Path.unlink
    refusals: list = []

    def windows_shaped_unlink(self, *args, **kwargs):
        if self == lock_path and not refusals:
            refusals.append(self)
            raise PermissionError(13, "Access is denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", windows_shaped_unlink)

    store.delete_session("refused")

    assert refusals, (
        "the staged refusal never fired, so this test measured the ordinary path and says nothing "
        "about the fallback it exists for")
    assert not lock_path.exists(), (
        "a platform that refuses the in-lock unlink must still not be left holding the lock file -- "
        "it is removed as the lock tears down, see _LockHandle.unlink_on_release")
    assert not store.canonical_dir("refused").exists()


def test_a_lock_taken_without_a_delete_never_removes_its_lock_file(workspace):
    """The must-not-fire twin of the fallback above. `_LockHandle` records its request against the
    lock *key* on a thread-local, so a bug that let the flag survive one `session_lock` into the next
    would silently unlink the lock file of every subsequently locked session -- reopening exactly the
    "unlinking a lock file a concurrent process may be holding" hazard #22 rejected, from the
    opposite direction and with nothing else in the suite watching for it."""
    svc = SessionService()
    svc.create_session("A real request.", slug="deleted-one")
    svc.create_session("Another real request.", slug="survivor")
    svc.update_model("deleted-one", _full_model())
    store.delete_session("deleted-one")

    with store.session_lock("survivor"):
        pass
    assert store.lock_path("survivor").exists(), (
        "an ordinary lock take must leave its lock file behind; only a delete removes one")


def test_delete_waits_for_a_concurrent_writer_then_removes_what_it_wrote(workspace):
    """The paired must-succeed half of the race above: a writer already using the session when
    delete is asked for must not be interrupted mid-write, and delete must still succeed once the
    writer is done -- genuinely serialised, not merely lucky. The middle assertion is the one that
    would catch a regression to an unlocked or best-effort delete: it proves `delete_session` is
    still blocked while the writer holds the lock, not that it happens to finish after it."""
    svc = SessionService()
    svc.create_session("A real request.", slug="patient")
    svc.update_model("patient", _full_model())
    writer_holds_lock = threading.Event()
    writer_may_finish = threading.Event()

    def hold_and_write():
        with store.session_lock("patient"):
            store.save_session_artifact("patient", "brief", ARTIFACT_FILENAMES["brief"],
                                        "# Brief\n", source_revision=1)
            writer_holds_lock.set()
            writer_may_finish.wait(timeout=5)

    writer = threading.Thread(target=hold_and_write, daemon=True)
    writer.start()
    assert writer_holds_lock.wait(timeout=5), "the writer never took the lock"

    delete_finished = threading.Event()

    def do_delete():
        store.delete_session("patient")
        delete_finished.set()

    deleter = threading.Thread(target=do_delete, daemon=True)
    deleter.start()

    # Must genuinely be waiting on the writer's lock, not racing ahead of it.
    assert not delete_finished.wait(timeout=0.2), (
        "delete_session returned while the writer still held the lock -- it is not serialised")

    writer_may_finish.set()
    writer.join(timeout=5)
    assert delete_finished.wait(timeout=5), "delete_session never finished once the writer released"

    assert not store.canonical_dir("patient").exists()
    assert not store.lock_path("patient").exists()


# ── #36: a path that is only printed is still a path this code built ─────────────
#
# `deterministic/artifacts.py`'s `artifact save` and `cli.py`'s `_wrote` each re-joined
# `canonical_dir(slug) / "artifacts" / <recorded filename>` inline, so the chokepoint the two
# writes (#5) and the read (#23) were routed through was closed in three places and open in two.
# Neither of the two opens the file, which is exactly how they survived both sweeps — "it only
# prints it" reads as harmless. It is a different harm, not an absent one: a printed path is a
# disclosure in the plainest form there is, and the join is the same join.
#
# Nothing in-repo reaches either site with a name that is not an `ARTIFACT_FILENAMES` value, so both
# tests hand the site what a `SessionRepository` that is not this file backing would hand it. That is
# invariant 14's threat model verbatim, and the same reason
# `test_write_artifact_file_refuses_a_filename_that_is_not_a_filename` above drives Core directly.
#
# `session import` is NOT that route, and the difference is worth stating because the invariant's own
# sentence is about `context_cards` and reads as though it covered this field too. It does not:
# `check_session_dir` pins each recorded filename to its `ARTIFACT_FILENAMES` value and to
# containment, and import refuses the whole archive on either. A `session.json` edited in place is a
# live route — nothing re-validates the field when `read_meta` loads it back — but that is a
# different door, and naming the shut one as the open one is how a docstring stops being evidence.


def _recorded(filename: str) -> store.ArtifactStatus:
    """The `ArtifactStatus` a display site is handed — `filename` is an unconstrained `str` on it."""
    return store.ArtifactStatus(revision=1, filename=filename, updated_at="2026-08-19T00:00:00Z")


def _run_command(argv: list) -> str:
    """Run one deterministic verb through the real parser and command function, capturing stdout.

    Deliberately not through `app()`: its `except RequivoError` turns a refusal into a printed
    envelope and `SystemExit`, and what this test needs to see is which of the two the site produced.
    """
    ns = _build_parser().parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ns.func(ns, None)
    return buf.getvalue()


def test_artifact_save_reports_where_it_wrote_through_the_chokepoint(workspace, tmp_path, monkeypatch):
    """`artifact save`'s human branch printed the join itself. Routing it through `artifact_path`
    costs nothing on the ordinary path and refuses a name that is not a filename.

    The absence/refusal distinction #23 turned on survives here because there is nothing to confuse
    it with: this line runs immediately after the write, states where the content went, and never
    asks whether the file is there. `artifact_path` does not stat either, so a session with nothing
    generated is not newly an error — it never reached this line in the first place.
    """
    _session("say-where")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")
    doc = tmp_path / "brief.md"
    doc.write_text("# A brief\n", encoding="utf-8")
    argv = ["artifact", "save", "say-where", "--type", "brief", "--file", str(doc), "--revision", "1"]

    # Positive control first, and it is the load-bearing half: the ordinary save must still name the
    # real file under artifacts/. A site that raised on everything, or printed nothing at all, would
    # satisfy the refusal assertions below without ever having said anything true.
    out = _run_command(argv)
    assert str(store.artifact_path("say-where", ARTIFACT_FILENAMES["brief"])) in out

    # And the refusal. `ArtifactService.save` is the layer that hands this line a filename; a
    # repository that is not this repo's file backing is what can hand it one of these.
    for name in ESCAPES:
        monkeypatch.setattr(ArtifactService, "save", lambda *a, _n=name, **k: _recorded(_n))
        with pytest.raises(RequivoError) as ei:
            _run_command(argv)
        assert ei.value.code == "invalid_filename", name


def test_a_generated_document_reports_its_path_through_the_chokepoint(workspace):
    """`cli.py::_wrote` is the same join, and it is the one of the two that is shared: five generator
    verbs say where their document went through it, so one guard here covers all five.

    Driven directly rather than through a generator, for the reason the write-side test gives — every
    in-repo caller arrives with an `ARTIFACT_FILENAMES` value, and the caller that does not is the
    external consumer holding the services. `result` is a stand-in because `_wrote` reads exactly one
    field off it; a real generation result would only make the fixture longer.
    """
    _session("wrote-where")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")

    # Positive control: an ordinary generated document still names its real file.
    out = io.StringIO()
    with redirect_stdout(out):
        _wrote("wrote-where", SimpleNamespace(status=_recorded(ARTIFACT_FILENAMES["prd"])), "PRD")
    assert str(store.artifact_path("wrote-where", ARTIFACT_FILENAMES["prd"])) in out.getvalue()

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            _wrote("wrote-where", SimpleNamespace(status=_recorded(name)), "PRD")
        assert ei.value.code == "invalid_filename", name


def test_neither_display_site_can_be_made_to_print_a_path_outside_the_session(workspace, tmp_path,
                                                                             monkeypatch):
    """The consequence the two tests above are guards for, asserted as the thing a reader cares about
    rather than as an exception type: whatever these lines print stays under this session's
    `artifacts/`.

    **Both** sites, because the name says both. Each of the two above pins one, and a test whose name
    claims a pair while driving one of them is the overclaim this file exists to catch, one layer
    down in its own fixture. The shapes here are the ones the shared `ESCAPES` list does not carry.

    A backslash separator and a drive-letter path are in the list on every platform rather than
    behind a platform branch. On POSIX a backslash is an ordinary character, so there this asserts
    that the *name* is refused; on Windows it additionally asserts that the path could not have
    escaped — and the leg most likely to be handed one is the leg that could not have said so if the
    list were POSIX-only. The over-long name rides the same guard: it is the one vector the traversal
    shapes do not cover, and it fails as a bare OSError without the boundary. The uppercase name is
    here because `_FILENAME_RE` is deliberately lowercase-only, which is a refusal a reader is more
    likely to mistake for a bug than for the guard it is.
    """
    _session("stay-inside")
    artifacts = store.canonical_dir("stay-inside") / "artifacts"
    doc = tmp_path / "brief.md"
    doc.write_text("# A brief\n", encoding="utf-8")
    argv = ["artifact", "save", "stay-inside", "--type", "brief", "--file", str(doc), "--revision", "1"]

    for name in [r"..\..\ESCAPED.md", r"c:\windows\win.ini", "a" * 300 + ".md", "PRD.MD"]:
        with pytest.raises(RequivoError):
            _wrote("stay-inside", SimpleNamespace(status=_recorded(name)), "PRD")
        with monkeypatch.context() as m:
            m.setattr(ArtifactService, "save", lambda *a, _n=name, **k: _recorded(_n))
            with pytest.raises(RequivoError):
                _run_command(argv)

    # must fire, on both sites: each still prints, and prints inside artifacts/, for a real name.
    # Without this the block above is satisfied by two sites that refuse everything.
    out = io.StringIO()
    with redirect_stdout(out):
        _wrote("stay-inside", SimpleNamespace(status=_recorded(ARTIFACT_FILENAMES["epic"])), "epic")
    assert str(artifacts / ARTIFACT_FILENAMES["epic"]) in out.getvalue()
    assert str(artifacts / ARTIFACT_FILENAMES["brief"]) in _run_command(argv)


# ── the slug, and the loader that reads a model back ─────────────────────────


def test_slug_is_first_five_word_tokens():
    # The five-token rule survives #245; what changed is *which* five, because the tokens a request
    # opens with are almost never the ones that identify it. Here "we", "d", "like", "an" and "when"
    # go and the four words that name the thing stay.
    assert derive_slug("We'd like an invoice created automatically when signed") == (
        "invoice-created-automatically-signed")
    assert derive_slug("!!!") == "discovery"


def test_a_slug_carries_content_words_rather_than_the_request_opening():
    """#245. The slug is the handle a user retypes into `answer`, `status`, `brief` and `prd`, so a
    handle built from the phrase every request opens with is both unmemorable and collision-prone:
    two unrelated "We need a way to ..." requests differ only in a hash suffix. Filtering a fixed
    function-word list before taking five tokens is what makes the handle name its subject."""
    assert derive_slug("We need a way to track vendor invoices.") == "track-vendor-invoices"
    assert derive_slug("We need a leave approval system.") == "leave-approval-system"
    # Two requests that used to share the whole slug now describe themselves.
    assert derive_slug("We need a way to track vendor invoices") != derive_slug(
        "We need a way to archive old contracts")


def test_the_stopword_list_keeps_the_words_its_own_comment_promises_to_keep():
    """#245, and the guard the comment needed rather than a second copy of it.

    The rule above `_SLUG_STOPWORDS` is that a word is in it only if it is a function word in some
    in-scope language and **not a content word in any of them**, and the comment names the seven
    that were weighed and excluded on exactly that basis. `son` was in the list anyway -- the Spanish
    "(they) are", which is also an ordinary English noun -- so the paragraph claiming it was absent
    sat two lines above the line that contained it. Nothing checked, because the rule was prose.

    Asserted as the *class*, not the instance: the seven the comment names, so the next word added
    for one language's sake and refuted by another goes red under the comment that promised it
    would not."""
    from requivo.core.persistence import _SLUG_STOPWORDS

    for word in ("son", "hay", "sin", "man", "war", "bin", "hat"):
        assert word not in _SLUG_STOPWORDS, (
            f"{word!r} is an ordinary English content word and the comment above _SLUG_STOPWORDS "
            "says it was deliberately excluded")
    # Must fire: the list is the real one and is not empty, so the loop above is a real check.
    assert {"the", "nous", "der", "para"} <= _SLUG_STOPWORDS
    assert "son" in derive_slug("Track the son of the account owner").split("-")


def test_a_slug_folds_diacritics_rather_than_splitting_the_word():
    """#245. `[a-z0-9]+` treats an accented letter as a separator, so it does not merely drop the
    accent -- it cuts the word in half. 'systeme' arrived as 'syst' + 'me' and the slug read
    'nous-aimerions-un-syst-me'. Folding first keeps the word whole, and the emitted alphabet is
    unchanged, so `validate_slug` and every session already on disk stay valid."""
    fr = derive_slug("Nous aimerions un système d'approbation des congés payés").split("-")
    assert "systeme" in fr and "conges" in fr
    assert "syst" not in fr and "me" not in fr

    assert derive_slug("Podríamos automatizar la aprobación de vacaciones").split("-") == [
        "automatizar", "aprobacion", "vacaciones"]
    assert derive_slug("Ein Genehmigungssystem für Urlaubsanträge").split("-") == [
        "genehmigungssystem", "urlaubsantrage"]


def test_folding_expands_a_latin_letter_that_carries_no_combining_mark():
    """#245. NFKD decomposes a letter into base + mark and the ASCII fold then drops the mark. A
    letter with no mark to strip -- eszett, the ligatures, the stroked letters -- decomposes to
    itself, so the fold *deletes* it and mangles the word exactly the way the accents did, one
    letter along: 'strassenverkehr' would have arrived as 'straenverkehr'. They are spelled out
    first, so the fold never has a letter it can only discard."""
    assert "strassenverkehr" in derive_slug("Straßenverkehr melden").split("-")
    assert "oekosystem" in derive_slug("Œkosystem pflegen").split("-")


def test_a_request_of_nothing_but_stopwords_still_derives_a_usable_slug():
    """#245. Filtering can empty the token list, and an empty list means the `discovery` fallback --
    which is the collision case this change exists to reduce, reintroduced by the fix for it. Below
    two survivors the words as typed are used instead, so a terse request keeps a handle that says
    something and stays a valid slug."""
    assert derive_slug("We need it") == "we-need-it"
    assert derive_slug("We need a way to") == "we-need-a-way-to"
    from requivo.core.persistence import validate_slug
    validate_slug(derive_slug("We need it"))


def test_a_non_latin_request_still_derives_the_documented_discovery_fallback():
    """#245, and the residual limit stated rather than fixed. A script the ASCII fold cannot
    romanize leaves no tokens at all, so the slug is `discovery` and the second such session lands
    on `discovery-<hash>` -- two handles a user cannot tell apart. That is documented behaviour
    rather than an accident, which is why `derive_slug`'s own docstring says so; a transliterating
    dependency is the fix and it is not one this change takes on."""
    assert derive_slug("休暇承認システムが必要です") == "discovery"
    assert derive_slug("Нам нужна система одобрения отпусков") == "discovery"


def test_invalid_slug_is_rejected_before_touching_the_filesystem():
    # The traversal guard: an explicit slug that could escape the session root must raise in Core,
    # never build a path. Covers the separator, the dot segment, an absolute root, and the empty string.
    from requivo.core.errors import InvalidSlugError
    from requivo.core.persistence import canonical_dir, validate_slug
    for bad in ("../../escaped", "a/b", "..", ".", "", "/abs", "Upper", "under_score"):
        with pytest.raises(InvalidSlugError):
            validate_slug(bad)
        with pytest.raises(InvalidSlugError):
            canonical_dir(bad)
    assert validate_slug("leave-approval") == "leave-approval"   # the shape derive_slug() always emits


def test_reserved_windows_device_names_are_refused_as_slugs():
    # #221: con/nul/aux/prn/com1-9/lpt1-9 cannot be created as files or directories on Windows,
    # case-insensitively, whether bare or with an extension. A session slugged 'con' is legal on
    # macOS/Linux, exports fine, and cannot be materialized by `session import` on Windows -- a
    # portability hole the session format's own promise never mentions. Refused on every platform
    # (the check is platform-independent by design, invariant 17) so an archive created on POSIX
    # cannot be created and then found unopenable elsewhere.
    from requivo.core.errors import InvalidSlugError
    from requivo.core.persistence import validate_slug
    reserved = ("con", "prn", "aux", "nul",
                "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
                "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9")
    for name in reserved:
        with pytest.raises(InvalidSlugError):
            validate_slug(name)
        with pytest.raises(InvalidSlugError):
            validate_slug(name.upper())
    # Must-not-fire control: names that merely resemble the reserved set stay valid.
    for ok in ("console", "com0", "lpt", "con-approval", "prnter"):
        assert validate_slug(ok) == ok


def test_creating_a_reserved_slug_is_still_refused_through_canonical_dir_directly(workspace):
    # #372: `create_session` calls `canonical_dir`, never `validate_slug` directly, so the previous
    # test (which only exercises `validate_slug`) does not actually pin what stops a *new* 'con'
    # session from being created. This does. Must-fire, and it is the creation half of #372's split:
    # nothing exists at this name yet, so it is refused exactly as strictly as before the read half
    # of the fix landed.
    from requivo.core.errors import InvalidSlugError
    with pytest.raises(InvalidSlugError):
        store.canonical_dir("con")
    with pytest.raises(InvalidSlugError):
        store.create_session("con", "A request that would slug to a reserved name.")
    with pytest.raises(InvalidSlugError):
        with store.session_lock("nul"):
            pass  # pragma: no cover - refused before the body ever runs


@pytest.mark.skipif(store.fcntl is None, reason="the fixture cannot be built on this platform: "
                     "Windows refuses to create a directory literally named 'con' at the OS level, "
                     "independent of anything Requivo's own code does (see the module comment above "
                     "_RESERVED_DEVICE_NAMES) -- so a session already on disk under a reserved name "
                     "is a state only a platform that never enforced the restriction can reach. "
                     "REASONED, NOT OBSERVED: no Windows machine confirmed this by hand; it follows "
                     "from the documented Windows behaviour #221 already relies on.")
def test_a_session_already_on_disk_under_a_reserved_slug_is_readable_by_every_verb_that_named_it(
        workspace):
    # #372: a session already on disk under a Windows reserved name -- created before #221 shipped,
    # or on a platform that never refused one -- must stay reachable for reading, session export
    # included (the documented way to move it off the reserved name entirely). Built by hand rather
    # than through `create_session`, which must (and does, per the sibling test above) still refuse
    # to create one: this reproduces exactly what a pre-#221 directory looks like on disk today.
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    # Must-fire: every read path this issue named tolerates the existing directory.
    assert store.session_exists("con") is True
    assert store.canonical_dir("con") == d
    assert store.read_meta("con").slug == "con"
    assert store.session_request("con") == "A request captured before #221 shipped."
    assert "con" in store.list_session_slugs()
    # `session export`'s own read-consistency lock -- `lock_path`'s half of the fix, not just
    # `canonical_dir`'s -- must reach it too, or the one documented way off the reserved name stays
    # blocked even though every other read now works.
    with store.session_lock("con"):
        pass

    # Must-not-fire control, in the same fixture (a negative needs a positive beside it): a reserved
    # name nothing has created is still refused. The tolerance above is about what already exists on
    # disk, never a general relaxation of #221.
    from requivo.core.errors import InvalidSlugError
    with pytest.raises(InvalidSlugError):
        store.canonical_dir("nul")
    with pytest.raises(InvalidSlugError):
        with store.session_lock("nul"):
            pass  # pragma: no cover - refused before the body ever runs


@pytest.mark.skipif(store.fcntl is None, reason="same platform limit as the sibling test above: "
                     "the fixture needs a directory literally named 'con' already on disk, which "
                     "Windows itself refuses to create. REASONED, NOT OBSERVED.")
def test_idempotent_reinit_of_an_existing_reserved_slug_returns_it_rather_than_creating_one(
        workspace):
    # #372, the corollary that validates the read/creation split is drawn in the right place:
    # `session init` re-run against the identical request is documented as idempotent (returns the
    # existing session rather than erroring), and that must keep working for a session that happens
    # to sit at a reserved slug -- without ever taking the branch that *creates* a new 'con' directory.
    # `create_session`'s own rename is the sole claim on a slug (invariant 11); this proves the swap
    # never runs a second time by asserting the returned session_id is the one already on disk.
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("Some request.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "original-id", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    meta = SessionService().create_session("Some request.", slug="con")
    assert meta.session_id == "original-id"


def test_reserved_windows_device_names_are_refused_as_filename_stems():
    # validate_filename checks the stem before the first dot, so `con.md` and `con.tar.gz` are
    # equally reserved -- Windows refuses `CreateFile` on the device name regardless of extension.
    from requivo.core.errors import InvalidFilenameError
    from requivo.core.persistence import validate_filename
    for bad in ("con.md", "CON.MD", "nul.txt", "lpt1.json", "com9.tar.gz"):
        with pytest.raises(InvalidFilenameError):
            validate_filename(bad)
    # Must-not-fire control: an ordinary artifact filename, and one that merely starts with the
    # reserved word as a substring rather than the whole stem, stay valid.
    for ok in ("prd.md", "console.md", "config.md"):
        assert validate_filename(ok) == ok


def test_load_model_rejects_invalid_model(tmp_path):
    """Still a refusal, and no longer a `ValidationError` reaching the caller.

    This test used to assert exactly that -- `pytest.raises(ValidationError)` -- which is the defect
    #204 fixed, written down as an expectation. A pydantic error is not a `RequivoError`, so it went
    past `cli.app()`'s handler as a traceback and past the web error handler into a generic 500, on
    the file this product calls its durable output. What it *should* have been asserting all along
    is that the refusal arrives in the vocabulary every other malformed-session condition uses.
    """
    bad = tmp_path / "model.json"
    bad.write_text(json.dumps({"questions": [], "summary": {}}))  # required `model` missing
    with pytest.raises(ModelUnreadableError) as ei:
        load_model(bad)
    assert isinstance(ei.value, RequivoError), "a traceback here is the bug, not the guard"
    assert ei.value.details == {"path": str(bad)}, (
        "a bare model.json has no session and no revision; padding those keys with nulls would "
        "state facts nobody measured (see the family note in docs/compatibility.md)"
    )


@pytest.mark.parametrize("corruption", [
    "",                                            # empty
    "{",                                           # truncated mid-object
    '{"model": {}, "questions": [], "summary"',    # truncated after a valid prefix
    "not json at all",
])
def test_a_corrupt_model_is_a_structured_error_from_every_door(workspace, corruption):
    """Four ways of being corrupt, and -- the load-bearing half -- every door into a model.

    `load_model`, `load_session_model` and `load_revision_model` each read a model the same way, and
    which one a given verb reaches is not visible from the verb: `status` and `impact` come in
    through one, `model show` through another, an artifact freshness check through the third. A
    guard on some of them is the same defect one door along, so the assertion is over all of them.
    """
    svc = SessionService()
    slug = "corrupt-model"
    svc.create_session("A leave approval system.", slug=slug)
    svc.update_model(slug, _full_model())
    d = store.canonical_dir(slug)

    for target in (d / "model.json", d / "revisions" / "0001-model.json"):
        target.write_text(corruption, encoding="utf-8")

    for call in (lambda: store.load_session_model(slug),
                 lambda: store.load_revision_model(slug, 1),
                 lambda: load_model(d / "model.json")):
        with pytest.raises(ModelUnreadableError) as ei:
            call()
        assert str(d) in str(ei.value), "the message names the file that could not be read"

    # The two that know which session they are reading say so, and say where the history is.
    with pytest.raises(ModelUnreadableError) as ei:
        store.load_session_model(slug)
    msg = str(ei.value)
    assert f"requivo session verify {slug}" in msg
    assert "revisions/" in msg, "the remedy was on disk the whole time and nothing said so"
    assert ei.value.details["slug"] == slug

    with pytest.raises(ModelUnreadableError) as ei:
        store.load_revision_model(slug, 1)
    assert ei.value.details["revision"] == 1


def test_a_missing_model_is_not_reported_as_a_corrupt_one(workspace):
    """The distinction the wrapping must not flatten.

    "There is no model yet" is a session at revision 0 doing exactly what it should; "the model is
    unreadable" is a fact about the store with a recovery path. Catching `OSError` inside the reader
    would collapse the first into the second if the callers above stopped deciding it first, and the
    remedy printed would be a `revisions/` directory that is empty by definition.
    """
    SessionService().create_session("A leave approval system.", slug="no-model-yet")
    with pytest.raises(SessionNotFoundError):
        store.load_session_model("no-model-yet")   # revision 0: no model.json has been written


# --- The store's privacy .gitignore (#211) -------------------------------------------------------


def test_the_privacy_gitignore_is_written_once_and_never_restored(workspace):
    """`.requivo/` lands in the caller's workspace, which defaults to cwd -- for the Claude Code
    plugin that is the user's project repository by construction -- and `create_session` writes the
    client's request there verbatim. A routine `git add .` published it, silently, against the
    local-first confidentiality this product states as its wedge. This repository's own `.gitignore`
    covers `.requivo/`, which is why the maintainer was the one person who could not experience it.

    Two halves, and the second is the one that is easy to get wrong. The file is written on the call
    that brings the store root into existence, and *never again* -- because the trigger is the root
    being absent, not the marker being absent. A team that deletes it in order to commit sessions
    deliberately must stay committed; recreating it on the next session write would silently overrule
    them, which is the same disrespect in the other direction.
    """
    marker = workspace / ".requivo" / ".gitignore"
    assert not marker.exists()

    svc = SessionService()
    svc.create_session("A leave approval system", slug="first")

    assert marker.exists(), "creating the first session did not write the privacy .gitignore"
    assert marker.read_text(encoding="utf-8").splitlines()[-1] == "*", (
        "the ignore pattern must be the self-ignoring `*`, so nothing has to be added to the user's "
        "own .gitignore -- a file Requivo has no business editing"
    )

    # Deleted on purpose: the team wants these sessions committed. Nothing may bring it back.
    marker.unlink()
    svc.create_session("A room booking tool", slug="second")
    svc.update_model("second", _full_model())
    assert not marker.exists(), "a later session operation restored an ignore file the user deleted"

    # Edited on purpose: same branch, and the edit survives byte for byte.
    marker.write_text("sessions/secret-*\n", encoding="utf-8")
    svc.create_session("A third thing", slug="third")
    assert marker.read_text(encoding="utf-8") == "sessions/secret-*\n"


def test_no_store_directory_is_created_outside_ensure_store_dir():
    """The guard behind #211, because fixing every call site leaves the next one.

    A bare `mkdir(parents=True)` (or an `os.makedirs`) on a store path re-opens #211 for whichever
    verb reaches a fresh workspace first, and it does so silently — the session write succeeds, and
    only the absent ignore file says anything. So the rule is mechanical: under `src/requivo/`,
    creating a directory tree belongs to `ensure_store_dir` alone.

    **It walks the package, and fails when the walk finds nothing** (#320). It first scanned a
    hardcoded three-file list with a `continue` for a missing path — so a renamed file dropped out
    in silence and a store write added anywhere else was invisible, which is exactly what invariant
    7 says not to do: "a glob over a directory that no longer exists returns `[]`, and `assert not
    []` is an all-clear nobody earned". Both sibling guards in this repo already fail loudly on an
    empty scan set; this one now does too, and it recognises `os.makedirs`, which the name check let
    straight through.

    Exemptions are by (file, function) and asserted in both directions, so one whose call site is
    gone goes red as unchecked prose.
    """
    import ast

    src = Path(__file__).resolve().parent.parent / "src" / "requivo"
    exempt = {
        # Creates the staging tree for a session in flight. Its parent is `session_root()`, which the
        # line above it has already ensured, so this cannot be the call that creates the store root.
        ("core/persistence/store.py", "create_session"),
        # `ensure_store_dir` is the one place allowed to do it — that is the whole rule.
        ("core/persistence/store.py", "ensure_store_dir"),
    }
    seen: set[tuple[str, str]] = set()
    offenders: list[str] = []
    scanned = 0
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                    node.func.id if isinstance(node.func, ast.Name) else "")
                if name == "mkdir" and any(k.arg == "parents" for k in node.keywords):
                    pass
                elif name == "makedirs":
                    pass
                else:
                    continue
                if (rel, fn.name) in exempt:
                    seen.add((rel, fn.name))
                    continue
                offenders.append(f"{rel}:{node.lineno} in {fn.name}()")

    assert scanned > 20, (
        f"the scan found only {scanned} modules under {src} — a guard that cannot see the package "
        f"reports no offenders for the wrong reason"
    )
    assert not offenders, (
        "these create a directory tree without going through `ensure_store_dir`, so on a fresh "
        "workspace they can bring `.requivo/` into existence with no privacy .gitignore (#211):\n  "
        + "\n  ".join(offenders)
    )
    assert seen == exempt, (
        "an exemption above no longer names a real call site, so it is unchecked prose: "
        f"{sorted(exempt - seen)}"
    )


def test_a_failed_marker_write_leaves_no_root_behind_to_suppress_the_next_attempt(workspace,
                                                                                 monkeypatch):
    """#320. The guarantee could be switched off permanently by one transient error.

    `ensure_store_dir` used to read `not root.exists()` before creating anything. So when `mkdir`
    succeeded and the marker write then failed — a full disk, an EACCES, a Windows scanner holding a
    handle, which invariant 18 already documents as real for a structurally identical operation —
    the call failed loudly but left `.requivo/` present and unignored. Every later call then read
    `fresh = False` and never tried again, and the resulting state was indistinguishable from a user
    who had deleted the file on purpose: the one state this design means to be irreversible.

    So the two states are now "root and marker" or "neither". A failure removes the root this call
    made, and the next attempt starts clean.
    """
    real_open = builtins.open

    def refuse_the_marker(path, mode="r", *a, **kw):
        if str(path).endswith(".gitignore") and "x" in mode:
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", refuse_the_marker)
    with pytest.raises(RequivoError) as ei:
        SessionService().create_session("A confidential client request.", slug="one")
    assert ei.value.code != "", "the failure must be structured, not a bare OSError"
    assert not (workspace / ".requivo").exists(), (
        "the store root outlived the failed marker write, so every later call takes the "
        "'already exists' branch and the privacy guarantee is off for good"
    )

    monkeypatch.setattr(builtins, "open", real_open)
    SessionService().create_session("A confidential client request.", slug="one")
    assert (workspace / ".requivo" / ".gitignore").exists(), (
        "the retry after a transient failure did not write the marker"
    )


def test_the_store_root_is_created_without_probing_whether_it_exists(workspace, monkeypatch):
    """The other half of #320, and the reason `exists()` had to go rather than be wrapped.

    `Path.exists()` re-raises `EACCES` instead of swallowing it — invariant 15's #80, one function
    along — and `PermissionError` is not a `RequivoError`, so `cli.app()` let it out as a traceback:
    the very first command run in such a workspace crashed instead of refusing. `mkdir` with no
    `exist_ok` answers the question that actually matters ("did *I* create it?") atomically, and
    probes nothing.

    The assertion is that no `exists()` call decides this, because wrapping the probe would have
    passed a test that only checked the error type.
    """
    called: list[str] = []
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self, *a, **kw: (called.append(str(self)),
                                                               real_exists(self, *a, **kw))[1])
    SessionService().create_session("Something.", slug="probe")
    assert not any(c.endswith(".requivo") for c in called), (
        f"the store root is still decided by an exists() probe, which can raise EACCES: {called}"
    )

    # And an OSError from the store is a structured refusal, not a traceback.
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **kw: (_ for _ in ()).throw(
        PermissionError(13, "Permission denied")))
    with pytest.raises(RequivoError):
        store.ensure_store_dir(workspace / ".requivo" / "sessions")


# ── #261: what a half-finished save_revision leaves behind ──────────────────────
#
# `save_revision` is three writes and no transaction: the frozen revision file, model.json, then
# session.json. A crash between any two of them is a real state a user can be left in, so the
# question is not *whether* it tears but *which* torn state each ordering produces. These three tests
# pin all of it — the window the write order makes benign, the window it does not, and the heal.


class _InjectedCrash(BaseException):
    """A process death, modelled. `BaseException` on purpose: an `Exception` could be swallowed by a
    handler on the way out, and then the test would be measuring the handler rather than the store."""


@contextmanager
def _crashing_after(after: int):
    """Let `after` writes through, then refuse the rest: ENOSPC, a SIGKILL, a pulled plug.

    Yields the record of attempted writes, so a test can assert the tear *happened*: an injection
    that fired before any write at all leaves a perfectly coherent session, and every consistency
    assertion below would then pass for that reason instead of the intended one. The count is checked
    here rather than left to each caller, because that assertion is the whole difference between
    these tests and three that pass on an untorn session.

    The patch is installed through a *scoped* `MonkeyPatch` rather than through the test fixture.
    `monkeypatch.undo()` on the shared one rewinds every patch that fixture holds, including the
    REQUIVO_WORKSPACE set by `workspace` — which pointed the assertions at the real store on the
    developer machine and failed with `no_session_json`, a plausible-looking verdict about a session
    that was never there."""
    real = store_module._atomic_write
    record: dict = {"attempted": []}

    def crashing(path, content):
        record["attempted"].append(path.name)
        if len(record["attempted"]) > after:
            raise _InjectedCrash(f"simulated death after write {after}")
        return real(path, content)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_module, "_atomic_write", crashing)
        yield record

    assert len(record["attempted"]) == after + 1, (
        f"the injection did not fire where the test aims it: writes attempted "
        f"{record['attempted']}, expected {after} to land and one to be refused")


def _tear_revision_two(*, after: int) -> dict:
    """A session at revision 1 holding 'first', interrupted `after` writes into revision 2."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model(**{"problem": _slot(10, "explicit", "low", "first")}))

    model = store.load_session_model("s")
    model.model["problem"].value = "second"

    with _crashing_after(after) as record:
        with pytest.raises(_InjectedCrash):
            store.save_revision("s", model)
    return record


def _tear_first_apply(slug: str, *, after: int) -> dict:
    """The other shape, and a different arm of `check_session`: a session at revision **0**,
    interrupted `after` writes into its very first revision. `migrate_legacy` takes this path too.

    Takes its slug, because `create_session` is an atomic claim (invariant 11) and a second tear in
    one test would otherwise lose the rename to the first."""
    svc = SessionService()
    svc.create_session("Something.", slug=slug)

    with _crashing_after(after) as record:
        with pytest.raises(_InjectedCrash):
            svc.update_model(slug, _full_model(**{"problem": _slot(10, "explicit", "low", "one")}))
    return record


def _current_model_is_the_recorded_revision(slug: str) -> bool:
    """Does model.json hold the content session.json says the current revision holds?"""
    meta = store.read_meta(slug)
    d = store.canonical_dir(slug)
    recorded = meta.revisions[meta.current_revision - 1].model_hash
    payload = (d / "model.json").read_text(encoding="utf-8")
    return store.content_hash(payload) == recorded


def test_a_crash_after_the_first_payload_write_still_reads_as_the_recorded_revision(workspace):
    """The window `save_revision` writes the frozen revision file first in order to make benign.

    With model.json written first, a death in this window left every read path serving content no
    revision records while session.json, the provenance log and the freshness machinery all believed
    the session was still at the previous revision. `snapshot()` then reported revision N with N+1's
    model — the plausible recorded number invariant 12 exists to prevent, produced by Requivo's own
    crash window rather than by a racing writer. Writing the revision file first inverts it: the
    single completed write is one the readers do not consult, so they keep serving the recorded
    revision and the tear survives as an orphan file `verify` names.

    This is the test the write order in `save_revision` cites. Swap the two `_atomic_write` calls back
    and it goes red on `model_is_not_the_last_revision`."""
    record = _tear_revision_two(after=1)

    # The claim: every read path still serves the content session.json accounts for. The helper has
    # already asserted that a second write was attempted and refused, so this is not passing because
    # the injection fired too early and nothing was torn at all.
    assert store.read_meta("s").current_revision == 1
    assert _current_model_is_the_recorded_revision("s"), (
        "model.json holds content no revision records, and the metadata does not say so")
    assert store.load_session_model("s").model["problem"].value == "first"

    codes = {p.code for p in check_session("s")}
    assert codes == {"orphan_revision_file"}, (
        f"a crash in this window must leave at most an orphan revision file, got {sorted(codes)}")

    # The mechanism behind it, asserted separately so a failure above reads as the defect rather
    # than as a rearranged implementation: the one write that landed went to `revisions/`, which no
    # read path consults, and it carries the content of the revision that never completed.
    assert record["attempted"][0] == "0002-model.json", (
        "the first payload write is model.json again — the reorder is gone")
    orphan = store.canonical_dir("s") / "revisions" / "0002-model.json"
    assert orphan.is_file() and "second" in orphan.read_text(encoding="utf-8")


def test_a_crash_after_both_payload_writes_is_still_reported_as_inconsistent(workspace):
    """The must-fire half, and the honest limit of the reorder.

    Reordering shrinks the inconsistent window; it does not close it. Once *both* payloads are on
    disk and session.json is not, model.json genuinely is content the metadata does not account for,
    and that has to keep being reported — the same verdict, in the same words, as before the reorder.
    Without this case beside the one above, `check_session` returning nothing at all for any torn
    session would satisfy that test perfectly."""
    _tear_revision_two(after=2)

    assert store.read_meta("s").current_revision == 1
    assert not _current_model_is_the_recorded_revision("s")
    codes = {p.code for p in check_session("s")}
    assert codes == {"orphan_revision_file", "model_is_not_the_last_revision"}, (
        f"the window that is still inconsistent stopped saying so: {sorted(codes)}")


def test_the_next_apply_reclaims_the_orphan_and_verifies_clean(workspace):
    """A tear is survivable because the revision number was never spent. session.json still says 1,
    so the next apply mints 2 again and `_atomic_write` replaces the orphan rather than colliding
    with it — no manual repair, and `verify` is clean afterwards."""
    _tear_revision_two(after=1)
    assert {p.code for p in check_session("s")} == {"orphan_revision_file"}

    svc = SessionService()
    svc.update_model("s", _full_model(**{"problem": _slot(20, "explicit", "low", "healed")}))

    meta = store.read_meta("s")
    assert meta.current_revision == 2
    assert store.load_session_model("s").model["problem"].value == "healed"
    assert store.load_revision_model("s", 2).model["problem"].value == "healed", (
        "the orphan was left holding the content of the revision that never landed")
    assert check_session("s") == []


def test_a_crash_in_the_very_first_apply_leaves_a_session_still_at_revision_zero(workspace):
    """The same two gaps on the revision 0 → 1 path, which is a *different* arm of `check_session`
    and the one `migrate_legacy` takes.

    Worth its own test because the codes differ: with no revision recorded yet there is no hash to
    compare a current model against, so the second gap reports `model_without_revision` rather than
    `model_is_not_the_last_revision`. Both halves of the reorder still hold here — the first gap
    leaves nothing a reader can mistake for a model, where writing model.json first left a model no
    revision accounted for and `load_session_model` handing it out."""
    _tear_first_apply("gap-one", after=1)

    assert store.read_meta("gap-one").current_revision == 0
    assert not (store.canonical_dir("gap-one") / "model.json").exists(), (
        "model.json was written before the frozen revision file — a model at revision 0")
    with pytest.raises(SessionNotFoundError):
        store.load_session_model("gap-one")     # "no model yet", which is the truth
    assert {p.code for p in check_session("gap-one")} == {"orphan_revision_file"}

    # And the second gap, which the reorder does not close: model.json is now on disk with the
    # metadata still at revision 0, and `check_session` has to keep saying so.
    _tear_first_apply("gap-two", after=2)
    assert store.read_meta("gap-two").current_revision == 0
    assert {p.code for p in check_session("gap-two")} == {"orphan_revision_file",
                                                          "model_without_revision"}
