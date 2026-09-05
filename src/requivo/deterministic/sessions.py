"""`requivo session`: create, list, show, migrate, export, verify, restore, rescope and import
sessions.

The session directory is the interface between every surface and it is public at `format_version` 1
(invariant 8), so this is the largest of the deterministic modules and the one whose output shape is
hardest to change. Two rules run through all of it.

**A listing survives its own members** (invariant 15). `session list` renders every row it can and
degrades the ones it cannot, and *could not be read* is a different answer from *not analysed yet*.
`EXIT_DEGRADED` is the exit code that says so. It lives in `_shared` rather than here because it
names a shape of answer rather than a verb: `session verify` reaches the same state from the other
side when it cannot read a session's product context, and minting a code per verb would rebuild the
collapse the code exists to undo.

**A value read off disk is untrusted input** (invariant 14). Every slug, error string and filename
that comes back from the store goes through `display_token` before it reaches a printed line,
because a stored value carrying a newline would otherwise write what reads as a second,
authoritative line of Requivo's own output at column 0.

`session verify` also asks whether a session's context cards still load on this machine. That is an
environment finding rather than an integrity one, so the check and its two remedy hints are imported
from `doctor`, which owns them, instead of being restated here: the two surfaces printing different
advice for the same finding is how they drift.

Part of the deterministic surface, so no LLM and no API key. `register_sessions(sub)` is composed
into the package's single `register()` by `deterministic/__init__.py`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from requivo.core import persistence as store
from requivo.core.errors import (
    ImportDestinationOccupiedError,
    ImportMoveFailedError,
    InconsistentArchiveError,
    InvalidArchiveError,
    InvalidModelError,
    ModelUnreadableError,
    RequivoError,
    SessionExistsError,
    SessionLockedError,
    SessionNotFoundError,
    SessionUnreadableError,
    UnreadableArchiveError,
)
from requivo.core.integrity import (
    SEVERITY_NOTE,
    blocking,
    check_session_dir,
    inspect_session,
    newest_readable_revision,
    readable_revision,
)
from requivo.core.persistence import UnexaminableEntry, ensure_store_dir
from requivo.core.selectors import display_token
from requivo.deterministic._shared import _NO_DETAIL, EXIT_DEGRADED, _read_source, _resolve_cards, print_json
from requivo.deterministic.doctor import _REPAIR_HINT, _RESTORABLE_CARD_CODES, _RESTORE_HINT, _card_health
from requivo.paths import session_root
from requivo.services.repository import SessionRepository
from requivo.services.sessions import SessionService


def _cmd_session_init(a, client) -> None:
    request = _read_source(a.request)
    if not request.strip():
        raise InvalidModelError("session init needs a request (a sentence or a file path)")
    cards = _resolve_cards(a.context)
    meta = SessionService().create_session(
        request, context_cards=cards, slug=a.slug, provider=a.provider)
    # Both lines below reach `canonical_dir` directly, and that is the justified kind (#76): where
    # the session landed on this machine is the answer the caller asked for, in `--json` for a script
    # and in prose for a reader. `SessionRepository` deliberately exposes no path — a Postgres
    # backing has none to expose — so there is no seam to route this through, and a CLI that talks
    # about files is entitled to know about them.
    if a.json:
        # `revision` is 0 for a genuinely new session — but init is idempotent, so re-running it on the
        # same request returns an *existing* session that may already carry a model. A caller about to
        # apply needs to know which of the two it got, and this is where it finds out.
        print_json({"slug": meta.slug, "session_id": meta.session_id,
                     "path": str(store.canonical_dir(meta.slug)), "context_cards": meta.context_cards,
                     "revision": meta.current_revision})
        return
    print(f"Created session '{meta.slug}' → {store.canonical_dir(meta.slug)}")
    print("  No model yet. Produce a proposal and run:")
    print(f"    requivo model apply {meta.slug} proposal.json")


def _session_list_row(entry) -> dict:
    """One `--json` row, with the **same key set** whether the session could be read or not.

    That is the compatibility decision, and it is why a degraded row is not simply a shorter dict:
    `session list --json` is a public output (invariant 8), and a consumer looping over
    `payload["sessions"]` reading `row["revision"]` would get a `KeyError` from a row it was handed
    deliberately — trading a command that fails loudly for a caller that fails obscurely, one layer
    along.

    So the fields are always present and `null` where the fact is missing. `null`, never `0` or `""`:
    we did not read revision 0, we failed to read the revision, and a plausible value on a session
    nobody could open is the quiet-wrong-answer form of the bug this whole guard exists for.
    `readable` is what a consumer should branch on; `error` carries the reason, because *written by a
    newer Requivo, upgrade* is a remedy and a flattened code is not.
    """
    if not entry.readable:
        return {"slug": entry.slug, "revision": None, "provider": None, "updated_at": None,
                "readable": False, "error": entry.error or _NO_DETAIL}
    m = entry.meta
    return {"slug": m.slug, "revision": m.current_revision, "provider": m.provider,
            "updated_at": m.updated_at, "readable": True, "error": None}


def _session_list_line(entry) -> str:
    """One terminal row. A session that could not be read still gets one, and still names itself.

    **Every text field on both branches is untrusted, and all of them go through `display_token`**
    (#40). An earlier draft of this docstring wrapped only the degraded branch and argued the
    readable one was safe because "the slug comes back through `read_meta`, which validates it".
    That was wrong, and wrong in the way this codebase keeps finding: `read_meta` validates the slug
    it is *called with* — the directory name, via `canonical_dir` — and then returns
    `SessionMeta.slug`, which is the `"slug"` field inside `session.json`'s own body, declared a bare
    `str` with no pattern. The two are not the same value and nothing checks that they agree outside
    `session import`. Reproduced on this branch: a `session.json` whose `slug` carries a newline
    printed a second, entirely fabricated row — `rev 999 (trusted, …)` — into the listing, and the
    command exited 0.

    That is invariant 14's second door. A persisted `session.json` is untrusted input every time it
    is read back, exactly as a persisted `context_cards` is; creation resolving a value is a
    guarantee about creation, never about what is on disk. So:

    * **the degraded row's slug** is the raw directory name — `list_session_slugs` returns `p.name`
      filtered only on a leading dot, and the `read_meta` that would have refused a non-kebab name is
      precisely why this row is degraded, so it never ran;
    * **the readable row's `slug`, `provider` and `updated_at`** all come out of the file's body.
      `current_revision` does not need wrapping: it is an `int`, so `read_meta` refuses a string
      there already;
    * **the error text** is whatever the failure said. `read_meta` refusing a `session.json` whose
      `current_revision` is a string raises a pydantic `ValidationError` whose message is four lines
      long; printed raw that is four rows of listing for one session, with the reader unable to tell
      where the row ends. `display_token` collapses it to one escaped line — the same `!r` treatment
      `core/integrity.py` gives the recorded artifact filename, its sibling untrusted field.

    A value that is already one safe line comes back byte-for-byte, so every real session's row is
    unchanged and no reader learns a new shape for the normal case.

    **`session show` had the same defect and is fixed in #70** — this paragraph used to say it was
    deliberately left for its own change, which it was, and the pointer is kept rather than deleted
    because the count it gave was wrong: five, where the verb turned out to print **eight** untrusted
    strings. #62 counted the `SessionMeta` scalars and missed `slug` plus the two fields that live on
    `ArtifactStatus` and its dict key. Read `_cmd_session_show`'s docstring for the surface-specific
    half; the argument is this one.

    The `--json` path needs none of this, **for a narrower reason than this file used to give**.
    `json.dumps` defaults to `ensure_ascii=True`, and that default is load-bearing — but not for the
    newline both issues reproduced with. A control character below U+0020 is escaped by JSON's own
    grammar whatever the flag says; what the flag decides is the *non-ASCII* half of `_CONTROL_CHARS`,
    U+007F–U+009F, which carries NEL and CSI. Measured, and pinned by
    `test_session_show_json_escapes_a_control_character_before_it_reaches_a_line`, which probes both
    halves because a newline probe is green either way and pins nothing (#70).

    The reason rides the row rather than being replaced by a pointer, because for the commonest break
    mode the reason *is* the remedy. `requivo session verify <slug>` is the acting surface the footer
    points at for the cases where one line is not enough: measured against each way `read_meta` can
    refuse — a newer `format_version`, an unparseable `session.json`, a field of the wrong type — it
    reports an integrity code and exits 1 rather than raising.

    **Two** cases it does not report on, and they fail in opposite directions. A slug that is not a
    slug is refused by name, and there the row's own text is already the whole story because the name
    is the defect. An entry the partition could not *examine* is the other, and it is the one worth
    knowing about: `session_exists` probes `session.json` with the same unguarded `.exists()` this
    file's own listing had to stop using in #80, so `verify` raised a bare `PermissionError` on the
    very row this footer sent the reader to. **Fixed in #97**, one release after it was filed here:
    `session_exists` raises `SessionUnreadableError` rather than widening a bool that has two states
    for a question with three, and `verify` folds that into `unchecked` and exits **4** — the footer
    now reaches a verb that says it could not look, which is what this line always promised.
    """
    if not entry.readable:
        return (f"  {display_token(entry.slug):<40} could not be read — "
                f"{display_token(entry.error or _NO_DETAIL)}")
    m = entry.meta
    return (f"  {display_token(m.slug):<40} rev {m.current_revision}  "
            f"({display_token(m.provider or '—')}, {display_token(m.updated_at)})")


def _cmd_session_list(a, client) -> None:
    """Every session, degrading the ones that cannot be read rather than failing for the set.

    Invariant 15 — *a listing survives its own members* — and this is the surface that did not get
    the fix when the web half shipped (#7, #62). `list_sessions()` is the strict read: a single
    comprehension over `read_meta`, so one `session.json` written by a newer Requivo raised before
    any row existed, the command exited 1 with a single message, every other session was invisible,
    and nothing named which session was the problem. `list_entries()` is the same read degrading per
    member, and it is where the guard belongs — above the rows, not around them.

    **This row needs no second `except` and the web's does.** Everything rendered here comes off the
    metadata `list_entries` has already loaded and guarded; the web row additionally calls
    `request_text` and `status()`, which is why it wraps its row builder as well. That is a fact
    about the current row shape rather than a promise: adding a read to this row means adding that
    guard too, and `test_a_break_below_the_metadata_does_not_reach_this_listing` is what will say so.
    """
    entries = SessionService().list_entries()
    degraded = [e for e in entries if not e.readable]
    if a.json:
        # An **object**, not the bare array this was until #87. It was the only array among the
        # fourteen JSON payloads this CLI prints, and an array has no top level, so no field could
        # ever be added to it without the type change made here once, in the 1.0 release itself.
        #
        # `degraded` recovers no fact. Every row carries `readable` and `error` whether it could be
        # read or not, so the count has always been derivable from the rows. What the key buys is
        # that exit 4 is readable on stdout rather than only signalled, which is the same argument
        # that makes a degraded row name its session instead of disappearing.
        print_json({"sessions": [_session_list_row(e) for e in entries],
                     "degraded": len(degraded), "session_root": str(session_root())})
    elif not entries:
        print(f"No sessions under {session_root()}.")
    else:
        print(f"Sessions under {session_root()}:")
        for e in entries:
            print(_session_list_line(e))
        if degraded:
            n = len(degraded)
            print()
            # `entr{y,ies}` and not `session{,s}` since #80. A degraded row used to be a name that
            # certainly had a `session.json` behind it, because every row came from
            # `list_session_slugs`; one of them can now be an entry nobody could examine, and calling
            # that a session is the single claim this whole change exists to refuse. The word also
            # matches what `doctor` says about the same entry, so the two surfaces stop describing
            # one thing two ways. `session verify <slug>` stays the remedy: it is right for every
            # mode it was written for, and where it is not, the fix belongs in that verb.
            print(f"{n} entr{'y' if n == 1 else 'ies'} could not be read. "
                  f"`requivo session verify <slug>` reports what is wrong in full.")
    # Raised after the listing is printed, never instead of it: the rows are the answer, and the exit
    # code is the third state in the one channel a script that does not parse stdout can read.
    if degraded:
        raise SystemExit(EXIT_DEGRADED)


def _cmd_session_show(a, client) -> None:
    """One session's metadata. **Every string on this path comes out of `session.json`'s body and is
    untrusted**, so all eight of them go through `display_token` (#70).

    The argument is `_session_list_line`'s, in full, and is not repeated here — read that docstring.
    Only two things differ, and both make this verb the worse of the pair rather than the safer one:

    * **It is eight fields, not the five the issue counted.** #62 named the five that happen to be
      `SessionMeta` scalars. The other three are `meta.slug` — which #62's own fix caught on the
      listing and which is the same bare `str` here — plus two that are not `SessionMeta` fields at
      all: the **keys** of `artifact_status`, a `dict[str, …]` whose keys are whatever the file says,
      and `ArtifactStatus.filename`. `core/integrity.py` already treats that recorded filename as
      untrusted input; a render site that does not is the exception that makes the rule unreliable.
    * **Every line here is one Requivo writes itself**, in a fixed shape, at a fixed column. On the
      listing a forged row at least has to imitate a row; here a stored value can print
      `  revision 0` under a session that is at revision 12, and nothing in the render distinguishes
      the two. Reproduced on this branch: a `session.json` forged in all eight fields printed sixteen
      lines instead of eight, including its own `revision 999` and `provider trusted`, and the
      command exited 0.

    `meta.current_revision`, `st.revision` and `st.stale` are deliberately **not** wrapped, and that
    is stated rather than hedged: they are `int`/`int`/`bool`, so `read_meta` refuses a string there
    before this function runs. Wrapping them defensively would say the type gave us nothing, which is
    the reading that makes the next person wrap something that genuinely does not need it.

    `session_id` is **sliced before it is escaped**, and the order is load-bearing: escaping first
    would produce a quoted, backslash-escaped string, and truncating *that* to twelve characters can
    cut an escape sequence in half and leave the quote unclosed — a neutralised value rendered as
    garbage, which is a second defect bought with the fix for the first.

    The `--json` path needs none of this and is left alone — but **not for the reason #62 and #70
    both give**, which is worth stating here because that reason is what a later reader will act on.
    It is not that `json.dumps` defaults to `ensure_ascii=True`: a control character below U+0020 is
    escaped by JSON's own grammar whatever that flag says, so a *newline* — the character both issues
    reproduced with — is safe either way. The default is still load-bearing, for the non-ASCII half of
    `_CONTROL_CHARS` (U+007F–U+009F), which carries NEL, a line terminator `str.splitlines()` honours,
    and CSI. Measured rather than argued, and pinned by
    `test_session_show_json_escapes_a_control_character_before_it_reaches_a_line`, which probes both
    halves precisely because a newline probe is green under either setting and pins nothing.

    A value that is already one safe line comes back byte-for-byte, so no real session's output
    changes — `test_session_show_leaves_an_ordinary_session_byte_for_byte` pins every line of it.

    **What this does not cover, said here rather than left to be discovered.** `display_token`'s
    `_CONTROL_CHARS` is C0, DEL and C1 — the class that can move a terminal's cursor or end its line.
    `str.splitlines()` also breaks on U+2028 and U+2029, which are *not* in that class and come back
    from `display_token` byte-for-byte. On a terminal that is correct: xterm and the VT sequences it
    descends from answer to CR and LF, not to Unicode `Zl`/`Zp`. It is not correct for anything that
    parses this human-readable output line by line — which is what `--json` is for, and which is
    covered there, since `ensure_ascii=True` escapes those two as well. Widening `_CONTROL_CHARS`
    would also change what `normalize_tokens` *refuses*, i.e. the public `unsafe_selector_token`
    surface, and that module's own comment scopes it deliberately — so it is a decision for its
    owner, reported rather than taken here (#70).

    **One cosmetic cost, accepted rather than overlooked.** The first line wraps the slug in literal
    quotes of its own, so a slug that has to be escaped renders nested — an apostrophe in the stored
    value puts a `repr` in double quotes inside this line's single ones. Ugly, still one line, still
    incapable of forging anything. Both available fixes are worse: dropping the literal quotes changes
    the output of every clean session, which is the guarantee above and worth more than the nesting;
    and quoting conditionally on whether `display_token` escaped puts a branch on that function's
    *return shape* rather than on its contract, which is the coupling that survives until somebody
    changes the escaper.
    """
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    meta = svc.meta(slug)
    if a.json:
        print_json(meta.model_dump())
        return
    print(f"Session '{display_token(meta.slug)}'  (id {display_token(meta.session_id[:12])}…)")
    print(f"  created  {display_token(meta.created_at)}")
    print(f"  updated  {display_token(meta.updated_at)}")
    print(f"  revision {meta.current_revision}")
    print(f"  provider {display_token(meta.provider or '—')}   "
          f"model {display_token(meta.model_name or '—')}")
    # `display_token`, not a bare join (#40). This is the one card-name render site the selector
    # guard cannot reach: nothing here is *selecting*, so `normalize_tokens` never runs and a name
    # persisted by `session import` arrives unexamined. A clean name is returned byte-for-byte, so
    # this line is unchanged for every session that was not tampered with.
    print("  context  " + (", ".join(display_token(c) for c in meta.context_cards)
                           if meta.context_cards else "all cards"))
    if meta.artifact_status:
        print("  artifacts:")
        for t, st in meta.artifact_status.items():
            # The explicit stale flag is the whole rule — the source revision is provenance, not an
            # invalidation signal (see ArtifactService.list). An artifact produced two revisions ago
            # whose inputs never moved is still fresh, and saying otherwise here contradicted both
            # `artifact list` and the status JSON every other surface reads.
            #
            # Padded *after* escaping, which is the only order that works: the column widths exist so
            # a reader can scan the block, and padding a value that is about to grow quotes lines the
            # block up against a length the render does not have.
            print(f"    {display_token(t):<12} {display_token(st.filename):<26} "
                  f"rev {st.revision}  {'STALE' if st.stale else 'fresh'}")


def _legacy_request_text(legacy_dir: Path) -> str:
    """The exact request text `migrate_legacy` would read from this legacy directory -- `request.md`
    if it exists, else `request.txt`, else empty. Mirrors that function's own fallback in
    `core/persistence.py` byte for byte, because `_cmd_session_migrate` compares it against a
    canonical session's own recorded request text to tell an interrupted migrate apart from an
    unrelated session that happens to occupy the same slug (#262).

    **Takes the directory, not the slug.** The one caller already has it — `output_root() / slug`,
    the exact path the sweep's own `slugs` listing walked to find `model.json` in the first place —
    so resolving it a second time through `store.legacy_dir` would be a fresh, unjustified reach past
    `SessionRepository` into `core.persistence` (`tests/test_boundaries.py`'s surface-storage
    allowlist, a file this lane does not own this round)."""
    for name in ("request.md", "request.txt"):
        p = legacy_dir / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def _scan_legacy_root(root: Path) -> tuple[list[str], list[UnexaminableEntry]]:
    """Partition the legacy `out/` root into legacy sessions and entries that could not be
    examined -- the scan `_cmd_session_migrate` reads its per-slug rows from, one directory over
    from `core.persistence._scan_session_root`'s identical partition of the *canonical* root.

    **Three outcomes, not two.** The probe deciding whether a name is a legacy session can itself
    raise -- `Path.exists()` re-raises `EACCES` -- and one unreadable directory used to abort the
    whole `session migrate` pass with a raw `PermissionError`. An entry it could not examine goes in
    neither other bucket: excluded it is invisible, counted it claims the one thing the probe did not
    establish. Invariant 15's "a guard above the rows is only as good as the scan that produced
    them". Pinned by
    `test_the_bulk_migrate_command_degrades_an_unreadable_legacy_directory_rather_than_crashing`.

    A root that does not exist returns two empty lists; a root that exists and cannot be *listed*
    still raises, because that failure is the whole root and there is no entry to name it against.
    The caller is what has to say *we could not look*. Pinned by
    `test_a_totally_unlistable_legacy_root_refuses_cleanly_instead_of_crashing`."""
    if not root.exists():
        return [], []
    slugs: list[str] = []
    unreadable: list[UnexaminableEntry] = []
    for p in sorted(root.iterdir(), key=lambda p: p.name):
        try:
            is_legacy = (p / "model.json").exists()
        except Exception as e:  # noqa: BLE001 - the third outcome, not a failure of the listing.
            # `Exception` rather than `OSError`, mirroring `_scan_session_root`'s own reasoning:
            # the ways a probe of a name off a directory listing can fail are open-ended -- EACCES
            # here, and a name `iterdir` returned with surrogate escapes (a non-UTF-8 filename on
            # Linux) makes every path operation on `p` a candidate too. `BaseException` is not
            # caught: a `KeyboardInterrupt` is not an unexaminable directory.
            unexaminable_entry = UnexaminableEntry(p.name, str(e))
            unreadable.append(unexaminable_entry)
            continue
        if is_legacy:
            slugs.append(p.name)
    return slugs, unreadable


def _cmd_session_migrate(a, client) -> None:
    """The bulk migration of every legacy out/<slug>/ session into the canonical store. Since 0.9.8
    this is the *only* thing that reads that layout — there is no automatic migrate-on-first-write.

    The `session_exists` check below is **reporting, not the guard**: it is what fills the
    `skipped_already_present` (and `interrupted`, see below) rows, and it is kept because a sweep that
    names what it declined is worth a cheap stat call. The guard is `migrate_legacy`'s own atomic
    claim on the slug — which is why the `SessionExistsError` arm exists. A session that appears
    between the check and the migration is the TOCTOU window the check cannot close, and the correct
    outcome there is the same skip.

    **A canonical session already occupying the slug is two facts, not one, and folding them was a
    false receipt.** A crash between `migrate_legacy`'s slug claim and its model apply leaves a
    revision-0 shell; reporting that as `skipped_already_present` — which means *the work is done* —
    told the user their session had migrated when `out/` was still the only copy. It renders as
    `interrupted` and counts toward `EXIT_DEGRADED`. Pinned by
    `test_an_interrupted_migration_is_reported_distinctly_from_already_present`.

    **`current_revision == 0` is necessary and not sufficient.** An ordinary session can sit at
    revision 0 too, and calling one `interrupted` prints a remedy — delete it and re-run — that would
    destroy real, unrelated work. `_legacy_request_text` is the second check; pinned by
    `test_an_unrelated_revision_zero_session_at_a_legacy_slug_is_not_called_interrupted`.
    `create_session` writes `request.md` from the exact request text it is passed, so a
    genuine crash window (where `migrate_legacy` claimed the slug with the *legacy* request) leaves
    `repo.request_text(slug)` identical to the legacy directory's own request text, and an unrelated
    session, created with its own request, does not match. Both conditions have to hold.

    **A legacy session whose `model.json` does not parse used to abort the whole pass, per invariant
    15's "a listing survives its own members" applied to this loop.** `migrate_legacy` raises before
    it claims the slug, so nothing was written for that slug, and letting the exception propagate
    ended the run with no output at all — every slug sorted after the bad one silently unreported, and
    every one before it, however many had already migrated, never printed either. It is caught now,
    narrowly: `RequivoError` is the vocabulary every structured failure in this store already speaks
    (a bad `model.json`, an unreadable file), so catching it rather than `Exception` still lets a
    genuine bug in the migration code itself surface as a traceback instead of one more list row.

    **`repo.read_meta(slug)` on the occupied-slug branch needs the identical isolation — also found in
    review.** It reads the *canonical* session's own `session.json`, which can be just as corrupt as a
    legacy `model.json` (unrelated to this migration, or itself a symptom of an interrupted write of
    some other kind), and it sat outside any per-slug guard: an unreadable canonical session for an
    already-occupied legacy slug aborted the whole pass exactly the way the unparseable-legacy-model
    case did before this issue was filed. It is wrapped the same way `migrate_legacy` is below.

    **Wrapping `read_meta` alone was not the whole of that isolation, and shipped believing it was**
    (#371). `repo.request_text(slug)` and `_legacy_request_text(root / slug)` — the two reads that
    decide `interrupted` vs. `skipped` once `read_meta` has succeeded — each do their own
    `p.exists()` + `p.read_text(encoding="utf-8")`, outside the `try` above them, in the version that
    shipped in 2.0.0. Neither `UnicodeDecodeError` nor an `EACCES` `Path.exists()` re-raises is a
    `RequivoError`, so an undecodable legacy `request.md` escaped this guard exactly the way an
    unparseable `model.json` used to: a raw traceback, no receipt printed at all, and a healthy
    session sorted after the bad one in the same sweep neither migrated nor reported. Both reads are
    inside the same `try` now, widened to `(RequivoError, OSError, UnicodeDecodeError)`.

    **The scan that PRODUCES `slugs` needed the identical isolation, one level below every guard
    above** (#411). `(p / "model.json").exists()` sat outside every per-slug `try` this docstring
    already describes -- `Path.exists()` re-raises `EACCES`, so one legacy directory the process
    could not stat into aborted the whole pass before the loop below was ever reached, with no
    receipt printed at all. `_scan_legacy_root` is the fix: the identical three-outcome partition
    `core.persistence._scan_session_root` already applies to the *canonical* root, one directory
    over. An entry it could not examine is reported under its own `unreadable` key -- never
    silently dropped, and never counted as a slug to migrate -- and folds into `EXIT_DEGRADED`
    alongside `interrupted`/`errors`, the same "the answer is incomplete" bucket those two already
    use, rather than a code of its own: this command already reports several distinct degraded
    reasons through separate named fields under one exit code, and a fourth reason is not a
    reason to split the code."""
    from requivo.paths import output_root
    root = output_root()
    try:
        slugs, unreadable = _scan_legacy_root(root)
    except OSError as e:
        # `_scan_legacy_root` raises when the root itself could not be listed -- deliberately, on
        # the same terms `_scan_session_root` states for the canonical root -- and this is the
        # catch that turns that into a clean, expected failure rather than the bare traceback #411
        # was filed to close one level down. A `RequivoError`: `cli.py`'s `app()` already prints
        # it as a clean receipt (the `--json` envelope or a one-line message) and exits 1 -- "no
        # answer", not "the answer is incomplete", because nothing here was even examined, so
        # `EXIT_DEGRADED`'s partial-answer meaning would overstate what happened (found in review).
        raise SessionUnreadableError(
            f"could not list legacy sessions under {display_token(str(root))}: {e}",
            details={"source": str(root)},
        ) from e
    migrated, skipped, interrupted, errors = [], [], [], []
    repo = SessionService().repo
    for slug in slugs:
        try:
            # `repo.exists(slug)` itself belongs inside a per-slug guard, not only the reads past it
            # (found in review of this same change, #371). It resolves through `canonical_dir`, and
            # since #372 that can refuse a *legacy-only* slug that is a reserved Windows device name
            # (`con`, `nul`, ...) with no canonical counterpart yet -- correctly, migrating one would
            # be `create_session` materializing a brand-new reserved-name directory, which invariant
            # 11 and #221 both say must stay refused. What must not happen is that refusal escaping
            # this loop uncaught: with no canonical session to read `meta` from, the exception fired
            # here, before the `try` a few lines down was ever reached, aborting the whole pass with
            # no receipt -- the identical shape #371 closed for the two reads past this check.
            occupied = repo.exists(slug)
        except RequivoError as e:
            errors.append({"slug": slug, "error": str(e)})
            continue
        if occupied:
            try:
                meta = repo.read_meta(slug)
                # Both reads that decide `interrupted` vs. `skipped` belong inside the same try as
                # `read_meta` above -- #371. `repo.request_text` and `_legacy_request_text` each do
                # their own `p.exists()` + `p.read_text(encoding="utf-8")`, and neither
                # `UnicodeDecodeError` nor an `EACCES` `Path.exists()` re-raises is a `RequivoError`,
                # so a legacy `request.md` that is not valid UTF-8 (or unreadable) used to escape this
                # per-slug guard entirely: a raw traceback, no receipt printed at all, and every slug
                # sorted after the bad one -- however many had already migrated -- silently unreported.
                # This is invariant 15's own class, one read below where #262 already closed it once.
                is_interrupted = (meta.current_revision == 0
                                   and repo.request_text(slug) == _legacy_request_text(root / slug))
            except (RequivoError, OSError, UnicodeDecodeError) as e:
                errors.append({"slug": slug, "error": str(e)})
                continue
            if is_interrupted:
                interrupted.append(slug)
            else:
                skipped.append(slug)
            continue
        try:
            # No repository equivalent, and there cannot be one: this converts a directory in the
            # retired `out/` layout into one in `.requivo/sessions/`. It is a statement about two
            # filesystem layouts, which is what the verb *is* — a Postgres backing has neither.
            store.migrate_legacy(slug)
        except SessionExistsError:
            skipped.append(slug)
            continue
        except RequivoError as e:
            errors.append({"slug": slug, "error": str(e)})
            continue
        migrated.append(slug)
    degraded = bool(errors or interrupted or unreadable)
    if a.json:
        print_json({"migrated": migrated, "skipped_already_present": skipped,
                     "interrupted": interrupted, "errors": errors,
                     "unreadable": [e.to_dict() for e in unreadable], "source": str(root)})
    else:
        print(f"Legacy sessions under {root}:")
        print(f"  migrated: {', '.join(migrated) or '(none)'}")
        if skipped:
            print(f"  skipped (already in canonical store): {', '.join(skipped)}")
        if interrupted:
            print(f"  present but empty (interrupted migrate?) — delete .requivo/sessions/<slug> and "
                  f"re-run: {', '.join(interrupted)}")
        if errors:
            print("  could not migrate:")
            for e in errors:
                # Both fields are untrusted: `slug` is a directory name under `out/`, and `error` is a
                # structured error's message text, which can itself quote file content — a corrupt
                # legacy `model.json` is exactly the case that reaches this line (#40, #70, invariant 14).
                print(f"    {display_token(e['slug'])}: {display_token(e['error'])}")
        if unreadable:
            # Neither field is a slug: this entry was never established to be a legacy session at
            # all, only a name the scan could not stat into (#411) -- untrusted the same way, and
            # through the same escape, for the same reason (invariant 14).
            print("  could not examine (skipped, not counted as a session):")
            for e in unreadable:
                print(f"    {display_token(e.name)}: {display_token(e.error)}")
        print("  Legacy files were preserved (read-only).")
    # Raised after the receipt is printed, never instead of it, for the same reason `session list`
    # raises after its rows: nothing on stdout is withheld, and a script that reads the exit code
    # alone still learns that this run was not a clean success.
    if degraded:
        raise SystemExit(EXIT_DEGRADED)


def _cmd_session_export(a, client) -> None:
    """Archive a session as a .zip — under its lock, and complete or not at all.

    A session is a handful of files that must agree with each other: session.json's revision count,
    the revision files it names, the model that should equal the last of them. Reading them one by one
    while another surface applies a revision produces an archive that combines an old metadata with a
    new model — internally inconsistent, and only discovered on import. So the read happens under the
    session lock, the same one every writer takes.

    `.lock` and the scratch files of an interrupted write are excluded: they are local artefacts of
    *this* machine's coordination, meaningless in an archive, and the lock file in particular would
    import as a session component. The archive itself is written beside its destination and renamed
    into place, so an interrupted export leaves no half-written .zip looking like a real one.

    **The default `<slug>.requivo.zip` destination shares its reserved-stem shape with a slug
    already refused for creation, and that is not a live gap** (raised in review, #372): a reserved
    slug can only reach this verb by already occupying a session directory on disk, and Windows
    itself refuses to *materialize* one under that name in the first place -- so on the one platform
    where `con.requivo.zip` would also be a reserved-stem-shaped filename, there is no `con` session
    to reach this line from. A caller who genuinely needs a portable archive name still has
    `--output`."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    # Direct, and legitimately so: this verb archives the session's *directory*. A path is the
    # subject of the command, not an implementation detail leaking through it.
    d = store.canonical_dir(slug)
    dest = Path(a.output) if a.output else Path.cwd() / f"{slug}.requivo.zip"
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    try:
        with svc.repo.lock(slug):
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(d.rglob("*")):
                    if f.is_file() and not any(part.startswith(".") for part in f.relative_to(d).parts):
                        z.write(f, f.relative_to(d.parent))
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    if a.json:
        print_json({"slug": slug, "archive": str(dest)})
        return
    print(f"Exported session '{slug}' → {dest}")


# Problem codes `session restore` (#210) can actually repair -- model.json disagreeing with, or
# missing against, a revision history that is otherwise intact. Every other integrity code names
# something restore does not touch: a broken revision log, a corrupt session.json, a revision file
# gone -- copying a revision over model.json does nothing about any of those. `session verify`'s
# remedy line below is scoped to exactly this set on purpose: naming a fix that would not fix the
# problem is worse than naming none, the same reasoning `_RESTORABLE_CARD_CODES` in `doctor.py`
# already applies one finding-family over.
_RESTORABLE_MODEL_CODES = frozenset({"invalid_model", "model_is_not_the_last_revision", "missing_model"})


def _restore_remedy_line(slug: str, problems: list, svc: SessionService) -> Optional[str]:
    """The one line #210 was filed to add: which revision `session restore` would copy over
    model.json, if any of `problems` is a code that verb can fix (see `_RESTORABLE_MODEL_CODES`).

    Text-only, like the card-health hints beside it -- `--json` already carries the `code`s a
    consumer can act on programmatically, and every existing hint in this verb follows the same
    split (see the module's own docstring on `display_token` for the parallel). `None` only when
    nothing here is restorable -- a session whose own metadata cannot be read still gets a line
    (below), because `session.checked` is set by an earlier, separately-locked read
    (`inspect_session`, released before this function runs its own), and a session that vanishes or
    locks up in the gap between the two is not a state this function may silently fold into "nothing
    to suggest" (found in review).

    **Two shapes of remedy, not one** (found in review). The newest revision this build can trust
    might not be the *last* one: `newest_readable_revision` skips a broken or tampered latest
    revision and falls back to an older one, and restoring from that fallback does not clear
    `model_is_not_the_last_revision` -- the last revision's own content is genuinely gone, and
    `session verify` is right to keep saying so. Recommending the identical command as the ordinary
    case, with no word of warning, is the wrong kind of reassuring; the fallback branch below says so.
    """
    if not ({p.code for p in problems} & _RESTORABLE_MODEL_CODES):
        return None
    try:
        meta = svc.meta(slug)
        n = meta.current_revision
        hashes = {r.revision: r.model_hash for r in meta.revisions}
        found = newest_readable_revision(store.canonical_dir(slug), n,
                                         expected_hashes=hashes) if n > 0 else None
    except RequivoError as e:
        # The third state, not a silent None: a restorable code is present but the search itself
        # could not run (a race with a concurrent writer, most plausibly). Saying nothing here reads
        # as "there is no fix", which is a different and stronger claim than "this could not be
        # checked" -- the exact collapse this whole file's own `session.checked`/`EXIT_DEGRADED`
        # machinery exists to refuse everywhere else.
        return f"    Could not check whether `session restore` could help: {display_token(str(e))}"
    if found is None:
        return ("    No revision file in this session's history could be read or trusted -- there is "
                "nothing for `session restore` to copy from. Recovery here is manual JSON surgery, "
                "or restoring this session from a backup.")
    if found.revision == n:
        return (f"    revisions/{found.revision:04d}-model.json parses cleanly and can replace it: "
                f"`requivo session restore {display_token(slug)}` (defaults to revision "
                f"{found.revision}) -- `session verify` should read clean afterwards.")
    return (f"    revisions/{found.revision:04d}-model.json is the newest revision this build can "
            f"still trust, but it is not the last one -- revision {n}'s own content is unreadable "
            f"or tampered and cannot be recovered. `requivo session restore {display_token(slug)}` "
            f"restores to revision {found.revision} as a partial repair; `session verify` will keep "
            f"reporting the session as inconsistent afterwards, correctly.")


def _cmd_session_verify(a, client) -> None:
    """Check that a session tells the truth about itself, and that the product context it names is
    still there. Exits non-zero when either is wrong, so it can gate a script.

    The two are reported side by side and kept apart on purpose. `problems` are *internal*: the
    relationships between the session's own files, which validating each file on its own cannot see.
    `context_cards` is an *environment* finding — the cards a session was created against live
    outside its directory, so a lost one says nothing about the session and everything about this
    machine. Keeping it out of `check_session_dir` is what stops `session import` refusing a
    colleague's perfectly good archive over a card you do not have; see `_card_health`.

    It is nonetheless part of `ok`, because a session whose cards are gone is refused at its next
    reasoning turn, and a verb that answers "is this session usable" with a tick right up to that
    moment is the failure this whole change is about.

    **Three answers, three exit codes.** The rendering always distinguished them and the exit code
    distinguished two, in the verb whose whole job is to answer *is this session sound*. Pinned by
    `test_session_verify_exits_one_when_the_cards_were_checked_and_are_broken`:

    - `problems` — checked, the session is inconsistent. A complete answer. **1**.
    - `cards["problem"]` — checked, its product context is broken. Also complete. **1**.
    - `not cards["checked"]` — the context could not be checked. Not an answer at all. **4**.

    4 rather than a code of this verb's own: it already means *the work was done and part of the
    answer was unreachable*, and an exit code describes a shape of answer, not a verb. A code per
    verb rebuilds the problem 4 was introduced to solve.

    **A firm negative outranks a partial one**, so a session that is both inconsistent *and* whose
    cards could not be read exits 1. A script gating on *is this usable* wants the definite answer,
    and there is one. Nothing is withheld at either code: `--json` carries the whole story either
    way, and `ok` keeps the meaning it always had — it is false in all three failing states.

    **A fourth thing is reported and is none of the three**, pinned by
    `test_session_verify_passes_and_still_names_the_unknown_type`: a `note` is a finding that is not
    a defect, and today the only one is an artifact type this build has no generator for. It prints,
    it rides in `--json` under `notes`, and it changes neither `ok` nor the exit code — because
    `docs/compatibility.md` lists a new artifact type among the changes that need no `format_version`
    bump, and a verb that answered "broken" there would be measuring a session written by a newer
    Requivo against a rule that version no longer follows. `problems` keeps its meaning exactly, so a
    consumer gating on it is unaffected.

    **A diagnosis is not the end of the story** (#210). Before this, the remedy for a torn model.json
    stopped at "run verify again" — the fact that `revisions/` holds every applied model, and that an
    earlier one can be copied over the broken one, lived nowhere a user reading this output would
    find it. When `problems` carries a code `session restore` can actually fix, the human render
    names the newest revision this build can still read and the exact command to run — see
    `_restore_remedy_line`. `--json` is unchanged: the codes were already enough for a script to act
    on, and this line is convenience for a human reading the terminal, the same split every other
    hint in this verb already makes.
    """
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    # The probe itself is a third source of `unchecked` (#97). `session_exists` no longer escapes as a
    # bare traceback when it cannot stat — it raises `SessionUnreadableError` — and letting that
    # propagate would exit 1, which says *I checked and it is broken* about a session nothing looked
    # at. That is the collapse #86 removed from this verb; it must not come back through a different
    # door. Nothing below this line can run either: `check_session` and `_card_health` both read the
    # directory this call could not stat.
    session_probe: dict = {"checked": True, "error": None}
    try:
        found = svc.exists(slug)
    except SessionUnreadableError as e:
        session_probe = {"checked": False, "error": str(e)}
        found = True
    else:
        if not found:
            raise svc.no_session(slug)
    findings: list = []
    if session_probe["checked"]:
        try:
            findings = inspect_session(slug)
        except (SessionLockedError, SessionUnreadableError) as e:
            # A lock this call could not take within the deadline (#263, #265) is no measurement,
            # not a broken session -- reporting it as `problems` would be the exact accusation shape
            # invariant 17 exists to prevent, aimed at a session that is merely mid-write. It joins
            # the probe's own unreadable arm above rather than getting a fourth state of its own.
            session_probe = {"checked": False, "error": str(e)}
    problems = blocking(findings)
    notes = [f for f in findings if f.severity == SEVERITY_NOTE]
    cards = _card_health(slug) if session_probe["checked"] else {"checked": False, "problem": None,
                                                                 "error": session_probe["error"]}
    unsound = bool(problems) or cards["problem"] is not None
    unchecked = not cards["checked"] or not session_probe["checked"]
    ok = not unsound and not unchecked
    # `exit_code`, not `code`: the rendering below already binds `code` to a card-problem *code*
    # string, and the collision reached the raise as `SystemExit('unknown_context_card')`, which
    # CPython prints to stderr and turns into status 1 — the number this change is about replaced by
    # a stray line, on the branch where the shadowing happens and only there. Caught by an existing
    # test, not by this one, which is why the name rather than the number is the fix.
    exit_code = 1 if unsound else (EXIT_DEGRADED if unchecked else 0)
    if a.json:
        # `session` is additive and always present (#97). It is a sibling of `context_cards` and
        # carries the same two keys for the same reason: a consumer reading `problems: []` has to be
        # able to tell *checked, nothing wrong* from *nothing was checked*, and an empty list spells
        # both. Branch on `session.checked`, never on the emptiness of `problems` — or, since #260,
        # of `notes`, which is empty in that arm for exactly the same reason and says exactly as
        # little.
        print_json({"slug": slug, "ok": ok, "session": session_probe,
                     "problems": [p.to_dict() for p in problems],
                     "notes": [n.to_dict() for n in notes], "context_cards": cards})
        if exit_code:
            raise SystemExit(exit_code)
        return
    if ok:
        print(f"✅ Session '{slug}' is internally consistent and its product context still loads.")
    if not session_probe["checked"]:
        print(f"🟡 Could not examine '{slug}': {display_token(session_probe['error'])}")
        print("   Nothing about this session was checked — this is not a report that it is sound.")
        raise SystemExit(exit_code)
    if problems:
        print(f"❌ Session '{slug}' has {len(problems)} problem(s):")
        for p in problems:
            print(f"  · [{p.code}] {p.message}")
        remedy = _restore_remedy_line(slug, problems, svc)
        if remedy is not None:
            print(remedy)
    if notes:
        # Printed under the tick rather than instead of it: the session *is* consistent, and this is
        # a fact about it worth naming (#260). Not a glyph of its own — ✅/❌/🟡 already spell the
        # three answers this verb gives, and a fourth would read as a fourth verdict.
        print(f"  Also worth knowing about '{slug}':")
        for n in notes:
            print(f"  · [{n.code}] {n.message}")
    if cards["problem"]:
        code = cards["problem"]["code"]
        restorable = code in _RESTORABLE_CARD_CODES
        print(f"❌ Session '{slug}' " + ("names product context that no longer loads:" if restorable
                                         else "has a product-context selection that cannot be read:"))
        print(f"  · [{code}] {cards['problem']['message']}")
        print(f"    {_RESTORE_HINT if restorable else _REPAIR_HINT}")
    elif not cards["checked"]:
        print(f"🟡 Could not check '{slug}'s product context: {display_token(cards['error'])}")
    if exit_code:
        raise SystemExit(exit_code)


# Windows' `rename` (`MoveFileEx`) can fail with `PermissionError(13)` when an antivirus scanner or
# the Search Indexer briefly opens the destination microseconds after it is written -- the same
# transient, external, non-serialisable cause `core/persistence.py`'s `_atomic_write` retries for
# under invariant 18 (a genuinely unwritable destination still fails, and fast: the narrow
# `except PermissionError` never masks anything else). This is a second, small statement of the
# identical shape, deliberately not a call into `_atomic_write` itself: `core/persistence.py` is
# outside #210's own stated scope (see `_cmd_session_restore`'s docstring), and this write's shape
# -- a payload already read off disk, not one this module composes -- is closer to
# `_cmd_session_export`'s `tmp.replace(dest)` a few functions up, which has the identical gap and is
# unchanged by this issue (filed separately rather than folded into this diff's own blast radius).
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF_S = 0.01


def _replace_with_retry(tmp: Path, path: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))


def _cmd_session_restore(a, client) -> None:
    """Copy a readable `revisions/NNNN-model.json` over `model.json` -- the explicit, user-invoked
    repair for a torn or inconsistent session (#210).

    This is **consent, not automation**. `core/integrity.py`'s own module docstring states why
    `doctor` and `session verify` diagnose and never write ("it reports rather than raises... a
    caller decides what a problem means") -- that design does not bind this verb, whose name on the
    command line is the consent. It changes exactly one file.

    **The revision history is untouched.** This is not `model apply`/`save_revision` under a new
    name: no entry is appended to `session.json`'s revision log, `current_revision` does not move,
    and no new `revisions/NNNN-model.json` is written. Restoring is model.json catching up with a
    history that was already the truth, not a new fact about the session.

    **"Clean afterwards" is a claim about the ordinary case, not a guarantee** (found in review).
    When the restored revision *is* the last one, model.json's bytes now equal it byte for byte and
    `session verify`'s `model_is_not_the_last_revision` check passes. When it is not -- because the
    last revision's own file could not be read or trusted, and the search fell back to an older one
    -- restoring is still the right thing to do (a real, historical state beats a torn file), but
    `session verify` will keep reporting the session as inconsistent afterwards, correctly: the last
    revision's content is genuinely gone, and no copy of an older one changes that. The printed
    receipt below says which case this run was.

    **Trusted, not merely parseable** (found in review, closing the same gap `revision_hash_mismatch`
    exists to catch on the read side). A revision file edited by hand after being frozen still parses
    as a valid model; restoring from one without checking its hash would make this the one place in
    the store that trusts what `session verify` itself would refuse. Both paths below compare the
    candidate's `content_hash` against the hash `session.json`'s own revision log recorded for it
    (`SessionMeta.revisions[i].model_hash`) and refuse a mismatch exactly as they refuse a parse
    failure -- an empty recorded hash is unconfirmed rather than refused, the same tolerance
    `inspect_session_dir` gives a legacy record with none.

    **A named target is refused, never silently substituted.** `--revision N` that does not exist,
    whose file cannot be read, does not parse, or does not match its recorded hash, raises rather
    than quietly falling back to another revision -- silently repairing from something the caller did
    not ask for is the wrong kind of helpful in a tool whose whole job is a deliberate, auditable
    repair. Only the *default* (no `--revision`) searches for the newest revision this build can read
    and trust (`newest_readable_revision`, the same search `session verify`'s remedy line names), and
    says which one it picked.

    **No `--json`, and that is a scoping decision, not an oversight.** Every sibling verb in this
    module has one; this one does not, because `docs/compatibility.md`'s `--json` promise and its
    two guards (`test_every_json_verb_is_inside_the_promise`,
    `test_every_public_json_payload_keeps_its_recorded_top_level_shape`) are a heavier commitment
    than #210's own stated scope (`deterministic/sessions.py`, `docs/cli.md`, tests) asked for. The
    human output already states everything this verb does.

    **No repository seam for this, and that is scoped the same way.** `SessionRepository.save_revision`
    always advances the revision log; there is no protocol method for "replace model.json and touch
    nothing else", and `core/persistence.py`/`services/` are outside #210's own stated scope, so
    inventing one was not this change to make. This reaches `store.canonical_dir` for the directory
    -- already a justified direct call in this file -- and reads/writes the two files with plain
    `Path` operations, the same shape `_cmd_session_export` already uses for directory-level work the
    repository has no method for either. The lock is still taken through the repository
    (`svc.repo.lock`), never `store.session_lock` directly, so the one direct reach past the seam
    here is the file operations themselves, not the serialisation around them.
    """
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    with svc.repo.lock(slug):
        meta = svc.meta(slug)
        n = meta.current_revision
        if n <= 0:
            raise InvalidModelError(
                f"session '{slug}' has no applied revision to restore from (current_revision is {n})",
                details={"slug": slug, "current_revision": n})
        hashes = {r.revision: r.model_hash for r in meta.revisions}
        d = store.canonical_dir(slug)
        if a.revision is not None:
            target_rev = a.revision
            if not 1 <= target_rev <= n:
                raise ModelUnreadableError(
                    f"session '{slug}' has revisions 1..{n}; {target_rev} is out of range",
                    details={"slug": slug, "revision": target_rev, "current_revision": n})
            found = readable_revision(d, target_rev, expected_hashes=hashes)
            if found is None:
                raise ModelUnreadableError(
                    f"revisions/{target_rev:04d}-model.json is missing, does not parse, or does not "
                    "match the hash session.json recorded for it -- refusing to restore from it",
                    details={"slug": slug, "revision": target_rev})
            payload = found.payload
        else:
            found = newest_readable_revision(d, n, expected_hashes=hashes)
            if found is None:
                raise ModelUnreadableError(
                    f"session '{slug}' has no readable, trusted revision file (1..{n}) to restore "
                    "from",
                    details={"slug": slug, "current_revision": n})
            target_rev, payload = found.revision, found.payload
        model_path = d / "model.json"
        # Temp file + rename, the same shape `_cmd_session_export` already uses for a write this
        # module's own repository seam has no method for: a crash mid-write can never leave
        # model.json half-written, because the rename is atomic on the same filesystem and nothing
        # else on disk ever points at the temp file's name. `_replace_with_retry` above is the one
        # difference from that sibling: a transient Windows lock retries instead of raising raw.
        tmp = model_path.with_name(f".{model_path.name}.{os.getpid()}.restore.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            _replace_with_retry(tmp, model_path)
        finally:
            tmp.unlink(missing_ok=True)
    print(f"✅ Restored model.json for '{display_token(slug)}' from revision {target_rev}.")
    print("  The revision history is untouched — this is not a new revision.")
    if target_rev != n:
        print(f"  This is a partial repair: revision {n}'s own content could not be read or "
              f"trusted and cannot be recovered. `session verify` will keep reporting this session "
              f"as inconsistent, correctly.")
    print(f"  requivo session verify {display_token(slug)}")


def _cmd_session_rescope(a, client) -> None:
    """Re-scope an existing session's context-card selection. What this does and does not do (a new
    revision only once a model exists, existing artifacts left alone, nothing re-run) is decided on
    `SessionService.rescope`, the single place it lives.

    `--context` is **required**, unlike `session init`'s optional flag: silently resetting to every
    card because the flag was left off is the accident a re-scope must not be able to cause. Its
    empty-string spelling still means "every card", so widening back is `--context ""`, spelled out
    rather than implicit. Pinned by `test_session_rescope_requires_context` and
    `test_session_rescope_to_all_cards_reports_none`."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    cards = _resolve_cards(a.context)
    result = svc.rescope(slug, cards)
    if a.json:
        print_json(result.to_dict())
        return
    previous = (", ".join(display_token(c) for c in result.previous_context_cards)
               if result.previous_context_cards else "all cards")
    now = (", ".join(display_token(c) for c in result.context_cards)
          if result.context_cards else "all cards")
    if not result.changed:
        print(f"Session '{display_token(slug)}' is already scoped to these cards — nothing changed.")
        print(f"  context  {now}")
        return
    print(f"✅ Re-scoped '{display_token(slug)}' → revision {result.revision}")
    print(f"  previous  {previous}")
    print(f"  now       {now}")
    if result.revision > 0:
        print("  Turns already reasoned were reasoned under the previous selection and are "
             "untouched; the next turn reasons against the new one.")


# Ceilings for an imported archive. A session is a handful of small JSON and Markdown files; anything
# near these is not one. They exist so a hostile or corrupt archive fails on a bound rather than on the
# filesystem filling up, and so decompression cannot be used as an amplifier.
MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
# Both caps above are computed over files alone, so an archive built entirely of *directory* entries
# declared zero files and ~zero bytes and passed both while the extraction loop still created every
# one of them -- inode exhaustion through a door neither file-only cap covers. This bounds the raw
# entry count, files and directories together, before either runs. A real `session export` writes no
# directory entry at all, so a small multiple is loose for the legitimate case and tight for the
# hostile one. Pinned by
# `test_import_refuses_an_archive_bounded_by_files_and_bytes_but_not_by_directory_entries`, with
# `test_an_archive_with_directory_entries_just_under_the_cap_still_imports` as the control.
MAX_ARCHIVE_ENTRIES = MAX_ARCHIVE_FILES * 4


def _inspect_archive(z: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
    """Validate an export archive *before* anything is written, and return the single session slug it
    contains together with the exact entry list that was validated. Raises `InvalidArchiveError` on
    anything unexpected, with `details["problem"]` naming which shape check refused it — see that
    class for the vocabulary and for why the eight conditions are one code (#101, #219). It answered
    `invalid_model` until then, on a path where nobody had proposed a model and where the arm on
    either side of it already named the archive.

    **The returned entry list is what the caller must extract, and only that.** The caller used to
    re-read `z.infolist()` for the extraction loop — a second call that happened to agree every time,
    which is not what "the extracted set is exactly the validated set" means. Handing back the one
    list this function counted and bounded makes it structural rather than a coincidence.

    Names are decomposed into path components, never prefix-matched: `str(target).startswith(root)`
    is not a containment test, since `/…/sessions-evil` starts with `/…/sessions`. A separator, a
    drive letter, a root or a `..` segment is unrepresentable rather than merely unlikely. Pinned by
    `test_import_refuses_unsafe_entries` and `test_every_refusal_on_the_import_path_names_what_it_is_about`."""
    all_infos = z.infolist()
    if len(all_infos) > MAX_ARCHIVE_ENTRIES:
        raise InvalidArchiveError(
            f"the archive holds {len(all_infos)} entries (files and directories); the maximum is "
            f"{MAX_ARCHIVE_ENTRIES}",
            details={"problem": "too_many_entries",
                     "entries": len(all_infos), "max_entries": MAX_ARCHIVE_ENTRIES})
    infos = [i for i in all_infos if not i.is_dir()]
    if not infos:
        raise InvalidArchiveError("the archive contains no files", details={"problem": "empty"})
    if len(infos) > MAX_ARCHIVE_FILES:
        raise InvalidArchiveError(
            f"the archive holds {len(infos)} files; the maximum is {MAX_ARCHIVE_FILES}",
            details={"problem": "too_many_files",
                     "files": len(infos), "max_files": MAX_ARCHIVE_FILES})
    total = sum(i.file_size for i in infos)
    if total > MAX_ARCHIVE_BYTES:
        raise InvalidArchiveError(
            f"the archive expands to {total} bytes; the maximum is {MAX_ARCHIVE_BYTES}",
            details={"problem": "too_large",
                     "bytes": total, "max_bytes": MAX_ARCHIVE_BYTES})

    slugs = set()
    for i in infos:
        name = i.filename
        if "\\" in name:  # a Windows-style separator is not a component boundary to zipfile
            raise InvalidArchiveError(f"unsafe path in archive: {name!r}",
                                      details={"problem": "unsafe_entry", "entry": name})
        parts = PurePosixPath(name).parts
        if len(parts) < 2:
            raise InvalidArchiveError(
                f"archive entry {name!r} is not inside a session directory; an export contains "
                "<slug>/session.json and friends",
                details={"problem": "entry_outside_session_directory", "entry": name})
        if any(p in ("", ".", "..") for p in parts) or PurePosixPath(name).is_absolute():
            raise InvalidArchiveError(f"unsafe path in archive: {name!r}",
                                      details={"problem": "unsafe_entry", "entry": name})
        slugs.add(parts[0])

    if len(slugs) != 1:
        # `display_token` per name, and this is the one arm on this path that needs it: the two
        # entry-name refusals above render with `!r`, and every message *after* this point names a
        # slug that `validate_slug` has already made kebab-safe. Here the names are raw archive text
        # — a directory called "ok\nAll clear." ends the line and writes the next at column 0, in the
        # refusal that exists to report it. Same class as #40 and #98, one function along. A name
        # with nothing to escape comes back byte-for-byte, so an ordinary archive reads unchanged,
        # and `details["slugs"]` stays raw because `json.dumps` escapes it on the way out.
        shown = ", ".join(display_token(s) for s in sorted(slugs))
        raise InvalidArchiveError(
            f"the archive holds {len(slugs)} session directories ({shown}); "
            "import takes exactly one",
            details={"problem": "multiple_sessions", "slugs": sorted(slugs)})
    slug = slugs.pop()
    # The directory name becomes a session slug, so it faces the same validation as any other — this is
    # what stopped an archive whose folder was called `bad slug` from being unpacked into the store and
    # breaking every later `session list`. Direct on purpose: this is the *name* rule the file
    # backing enforces, asked before any session exists to ask a repository about.
    return store.validate_slug(slug), all_infos


def _swap_in(extracted: Path, target: Path, slug: str, repo: SessionRepository) -> None:
    """Replace an existing session directory with a freshly extracted one, reversibly.

    A swap, not a delete-then-move. `rmtree` followed by a rename leaves nothing at all if the
    rename fails — the archive is refused *and* the session the user already had is gone. The old
    one steps aside first and only dies once the new one is in place; anything going wrong in
    between puts it back.

    **Only ever called for a session the caller passed `--force` for**, and never for a slug that was
    free at the guard — that arm claims by rename instead, because a session that appeared during the
    extraction window has an owner who never asked for it to be replaced (#111). That is why taking
    `session_lock` here needs no relaxation of the rule that a session must exist to be locked: this
    function is unreachable unless one does.

    **It runs under `session_lock`, which it could not do for a release** (#113). The lock used to be
    an open handle on `.lock` *inside* the directory being renamed, which Windows refuses — #112's
    four Windows legs died on `WinError 5`. It now lives in `lock_root()`, outside every session, so
    the swap is serialised against the writers of the session it replaces like any other compound
    write (invariant 9), and `os.replace` sees no open handle.

    Three things the lock closes, and the third is the one the issue did not name:

    1. a writer inside `save_revision` no longer keeps writing by pathname into the *imported*
       directory, stamping the replaced session's identity and revision log onto it;
    2. a third process no longer opens a fresh lock file in the imported directory and acquires a
       lock the first writer still holds on the old, since-unlinked inode;
    3. between the two renames below `<root>/<slug>` does not exist, and a concurrent
       `save_revision` recreated it with `(d / "revisions").mkdir(parents=True, exist_ok=True)`.
       Both the move and the rollback then failed on a non-empty destination — the rollback raising a
       bare `OSError` rather than `ImportMoveFailedError` — leaving the user's session stranded at a
       dot-prefixed name `_scan_session_root` skips and the slug held by a stub containing only
       `revisions/`. Reproduced before the fix: `session list` reported no sessions at all. So the
       step-aside, whose whole justification is that it is reversible, was defeated by the same race.

    A window remains between the caller's `repo.exists(slug)` and this lock, and it now ends in a
    structured `SessionNotFoundError` rather than the bare `FileNotFoundError` `target.replace` used
    to raise there.

    The lock is taken through `repo.lock`, not `store.session_lock`: *hold this session exclusively*
    has a backing-neutral form, so the direct call is the one
    `test_the_surfaces_reach_the_store_only_through_the_named_filesystem_concerns` is right to
    refuse. The two `Path.replace` calls below stay direct, because moving a directory onto another
    is genuinely about paths — the justification the sibling `canonical_dir` call already carries.

    Pinned by `test_a_forced_import_serialises_against_a_concurrent_writer`."""
    with repo.lock(slug):
        backup = target.with_name(f".{target.name}.replaced-{os.getpid()}")
        target.replace(backup)
        try:
            extracted.replace(target)
        except OSError as e:
            backup.replace(target)
            raise ImportMoveFailedError(
                f"could not move the imported session into place: {e}"
                " — the session that was already here has been restored",
                details={"slug": slug}) from e
        shutil.rmtree(backup, ignore_errors=True)


def _refuse_a_non_session_destination(target: Path, slug: str, repo: SessionRepository,
                                      cause: Optional[BaseException] = None) -> None:
    """Refuse when something that is **not a session** already occupies the slug's directory.

    `os.replace` answers this differently per platform, so without this guard the same stray `mkdir`
    imported on POSIX and failed on Windows, and the Windows refusal read `import_move_failed` — a
    sentence about a move, naming a cause that is not the cause. Enforced by
    `test_a_stray_directory_at_the_slug_is_refused_by_name_on_every_platform`.

    **It only ever refuses**, so invariant 11 is intact: the rename is still the claim, and nothing
    here authorises an import the rename would have lost. It is called on *both* sides of that rename
    because the two sides catch different windows — before it for a stray already on disk, and from
    the `except OSError` arm for one that landed while the archive was being read
    (`test_a_stray_appearing_in_the_rename_window_is_named_rather_than_called_a_move_failure`).

    The session half of the question goes through `repo.exists` rather than `store.session_exists`:
    *is this slug a session* is not a question about a path, so it has a backing-neutral form and
    `test_the_surfaces_reach_the_store_only_through_the_named_filesystem_concerns` is right to refuse
    the direct call. `target` is a path because the import moves a directory onto it, which is the
    justification the sibling `canonical_dir` call above already carries.

    **Three answers, not two.** Both probes re-raise rather than answering `False` — `Path.exists` on
    EACCES, `repo.exists` as `SessionUnreadableError` — and a probe that could not look has not
    established anything about the destination, so it says nothing and lets the rename decide, which
    is the only decision that was ever authoritative. `is_symlink` rides with `exists` for the reason
    invariant 17 gives: `exists()` follows the link, so a dangling symlink at the slug is a stray this
    would otherwise call an empty space.

    **A destination that really is a session is not this function's answer**, and reading the name
    without the body is how that gets lost. It belongs to `SessionExistsError` — *created while this
    archive was being read; pass `--force`* — which is a different remedy from this one and is raised
    by the caller. Answering here instead would replace a code a consumer already branches on with a
    new one, in the window `test_that_window_refusal_names_the_conflict_rather_than_a_move_failure`
    exists to pin.
    """
    try:
        if not (target.exists() or target.is_symlink()):
            return
        if repo.exists(slug):
            return
    except (OSError, SessionUnreadableError):
        return
    raise ImportDestinationOccupiedError(
        f"cannot import session '{slug}': {display_token(str(target))} already exists and is not a "
        "session — nothing was imported and nothing was removed. Move or delete it and import again; "
        "--force replaces a session and does not apply here.",
        details={"slug": slug, "path": str(target)}) from cause


def _validate_extracted(d: Path, slug: str) -> None:
    """Confirm an extracted directory really is a *coherent* session before it is allowed in.

    This used to check that session.json parsed, that its slug agreed, and that a claimed revision had
    a model.json — which is shape, not truth. An archive announcing revision 2 with no `revisions/` at
    all passed, and so did one whose model.json had been swapped for a different model: nothing is
    malformed in either, only the relationships are broken. `check_session_dir` is the same check
    `requivo session verify` runs, so an archive is held to exactly the standard a live session is.

    **Exactly the same standard, and deliberately not the same output.** What `session verify` has
    and this does not is the `notes` half — an artifact type this build has no generator for is a
    fact about the session, not a reason to refuse it, so it has nothing to say on a path whose only
    question is accept or reject. Pinned by
    `test_a_future_artifact_type_survives_an_export_import_round_trip`."""
    problems = check_session_dir(d, expected_slug=slug)
    if problems:
        raise InconsistentArchiveError(
            f"the archive's session '{slug}' is not internally consistent: "
            + "; ".join(p.message for p in problems),
            details={"slug": slug, "problems": [p.to_dict() for p in problems]})


def _cmd_session_import(a, client) -> None:
    """Import a session archive: inspect → extract to a scratch directory → validate → move into place.

    Nothing lands in the session store until the whole archive has been checked and what came out of it
    has been confirmed to be a session. The old flow did the reverse — `extractall` straight into the
    store, then report success — so a bad archive was already unpacked by the time anyone could object.
    (If a second surface ever needs this, it moves to core; today the CLI is the only importer.)"""
    archive = Path(a.archive)
    if not archive.is_file():
        raise SessionNotFoundError(f"archive not found: {display_token(str(archive))}",
                                   details={"archive": str(archive)})
    root = session_root()
    # Not a bare `mkdir`: on a fresh workspace `session import` is one of the calls that can bring
    # `.requivo/` into existence -- the second door of invariant 14, receiving a colleague's session
    # before this user has run one of their own -- and the privacy `.gitignore` is written by
    # whichever call creates the root (#211). Pinned by
    # `test_import_into_a_fresh_workspace_writes_the_privacy_gitignore`.
    ensure_store_dir(root)
    repo = SessionService().repo

    try:
        z = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as e:
        raise UnreadableArchiveError(f"{display_token(str(archive))} is not a readable .zip archive: {e}",
                                     details={"archive": str(archive)}) from e
    with z:
        slug, entries = _inspect_archive(z)
        # A conflict with the store's current state, not a malformed proposal: `session_exists`
        # exists for exactly this fact and answers 409 where `invalid_model` answered 400 (#101).
        #
        # **This answer is remembered, never asked twice.** Re-deciding it as `target.exists()`
        # *after* the unzip let a session created in that window be moved aside and `rmtree`d —
        # destroyed without `--force`, because when the user would have been asked to force there was
        # nothing to force past. Pinned by
        # `test_a_session_created_during_the_extraction_window_is_refused_not_destroyed`.
        # That is invariant 9 ("a precondition is
        # held across the writes it authorises") in the one verb that writes a whole session, and it
        # is why the two arms below are two arms rather than one flag.
        occupied = repo.exists(slug)
        if occupied and not a.force:
            raise SessionExistsError(
                f"session '{slug}' already exists in this workspace — pass --force to replace it",
                details={"slug": slug})
        # Scratch space beside the store, not inside it: same filesystem, so the final move is a
        # rename, but never visible to `session list` while it is still half-written.
        scratch = Path(tempfile.mkdtemp(prefix=".import-", dir=root.parent))
        try:
            # Exactly the entry list `_inspect_archive` validated and bounded — never a fresh
            # `z.infolist()` call — so the extracted set is structurally the validated set (#219).
            for info in entries:
                z.extract(info, scratch)
            extracted = scratch / slug
            _validate_extracted(extracted, slug)
            # Direct: the import moves a directory into place, so the destination *is* a path.
            target = store.canonical_dir(slug)
            if not occupied:
                # The slug was free when it was checked, so **the rename is the claim** and nothing
                # steps aside — invariant 11's rule, and what makes the window above safe rather than
                # merely narrow. `os.replace` refuses a non-empty destination (POSIX `ENOTEMPTY`;
                # Windows any existing directory, stricter still), so a session that appeared during
                # the unzip stops this import rather than being destroyed by it. Read without that
                # platform qualifier the sentence is how #114 happened: the safety claim holds on
                # both platforms and the *answer* did not. Pinned by
                # `test_that_window_refusal_names_the_conflict_rather_than_a_move_failure`.
                #
                # **What `os.replace` does *not* answer the same way on every platform is a
                # destination that holds no session at all** (#114), which is what
                # `_refuse_a_non_session_destination` is for. It is called on both sides of the
                # rename: neither side alone converges the platforms, because a stray already on
                # disk never reaches the `except` on POSIX and one that lands mid-window never
                # reaches the pre-check.
                _refuse_a_non_session_destination(target, slug, repo)
                try:
                    extracted.replace(target)
                except OSError as e:
                    if repo.exists(slug):
                        raise SessionExistsError(
                            f"session '{slug}' was created while this archive was being read — "
                            "nothing was imported and nothing was replaced; pass --force to replace it",
                            details={"slug": slug}) from e
                    _refuse_a_non_session_destination(target, slug, repo, e)
                    raise ImportMoveFailedError(
                        f"could not move the imported session into place: {e}",
                        details={"slug": slug}) from e
            else:
                # `--force` was given against a session that is really there.
                #
                # **The swap holds `session_lock`**, which for one release it could not: the lock
                # was an open handle inside the very directory being renamed, and Windows refuses
                # that on all four legs. Moving the lock out of the session is what made this
                # available; locking anywhere else *in addition* would serialise nothing, since this
                # is the one lock every writer already takes. Pinned by
                # `test_a_forced_import_serialises_against_a_concurrent_writer`.
                #
                # What closes #111 is still the arm above and the single decision it rests on, not
                # this lock: losing a session the caller was never asked about is a question about
                # which arm runs, and no amount of mutual exclusion answers it.
                #
                # What `--force` still means, deliberately: a concurrent writer's in-flight work is
                # lost. It is now lost cleanly — the writer completes, and then the whole session is
                # replaced — rather than half-landing in the imported directory.
                _swap_in(extracted, target, slug, repo)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    if a.json:
        # `slug`/`path`, the spelling every sibling session verb uses; it was `imported`/`into`, so
        # a consumer looping over the verbs and reading `row["slug"]` got a `KeyError` from the one
        # verb that had just put the session there. Pinned by
        # `test_import_json_names_the_session_and_its_directory_the_way_its_siblings_do`.
        #
        # `path` is the session's own directory, which is what `session init --json` means by the
        # word and what the line below already prints. `into` carried the session *root*; renaming
        # the key over that value would give `path` two meanings across two verbs of one noun, which
        # is this defect back under the harmonised name and harder to see for it.
        # `replaced` keeps the meaning it always had — *did this import replace an existing session*
        # — and is now the guard's own answer rather than a second observation taken after the
        # extraction. Those two used to be able to disagree, and the disagreement was #111.
        print_json({"slug": slug, "path": str(target), "replaced": occupied})
        return
    # Same as `session init`: the line's subject is where the session landed on this machine.
    print(f"Imported session '{slug}' → {store.canonical_dir(slug)}"
          + (" (replaced an existing session)" if occupied else ""))


def _cmd_session_delete(a, client) -> None:
    """Irreversibly remove a session -- the directory and its lock file, under the same lock every
    other compound mutation takes. No soft-delete, trash or undo, deliberately: `session export`
    first is the undo story, which is why the Web's confirmation copy points there rather than at a
    recovery this verb does not offer. Pinned by
    `test_a_session_removed_through_session_delete_leaves_no_lock_residue`.

    `a.session` may be a slug or a path, like every verb taking `session`; the existence check runs
    before the delete so a nonexistent slug is refused as `session_not_found` rather than reaching
    the store's own, less specific refusal. Pinned by
    `test_session_delete_refuses_a_nonexistent_slug_with_session_not_found`."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    svc.delete_session(slug)
    if a.json:
        print_json({"slug": slug, "deleted": True})
        return
    print(f"Deleted session '{slug}'.")


def register_sessions(sub) -> None:
    """Attach the `session` verb group to the main `requivo` subparser."""
    # session
    sp = sub.add_parser("session",
                        help="create, list, show, verify, restore, rescope, delete, migrate, export/import "
                             "sessions")
    ss = sp.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    si = ss.add_parser("init", help="create a session from a request (no LLM)")
    si.add_argument("request", help="the request, a path to a file containing it, or '-' for stdin")
    si.add_argument("--slug", help="explicit session slug (default: derived from the request)")
    si.add_argument("--context", "--cards", metavar="CARDS", dest="context",
                    help="comma-separated context cards to record. Alias: --cards.")
    si.add_argument("--provider", default=None, help="informational provider tag (e.g. claude-code)")
    si.add_argument("--json", action="store_true")
    si.set_defaults(func=_cmd_session_init)

    sl = ss.add_parser("list", help="list canonical sessions")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=_cmd_session_list)

    sh = ss.add_parser("show", help="show a session's metadata + artifacts")
    sh.add_argument("session", help="session slug or path")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=_cmd_session_show)

    sm = ss.add_parser("migrate", help="migrate ALL legacy out/ sessions into .requivo/sessions/")
    sm.add_argument("--json", action="store_true")
    sm.set_defaults(func=_cmd_session_migrate)

    se = ss.add_parser("export", help="export a session as a .zip archive")
    se.add_argument("session", help="session slug or path")
    se.add_argument("-o", "--output", help="destination archive path")
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=_cmd_session_export)

    sv = ss.add_parser("verify", help="check that a session's files agree with each other")
    sv.add_argument("session", help="session slug or path")
    sv.add_argument("--json", action="store_true")
    sv.set_defaults(func=_cmd_session_verify)

    srt = ss.add_parser("restore", help="copy a readable revision over model.json — the recovery "
                        "path for a torn or inconsistent session (#210)")
    srt.add_argument("session", help="session slug or path")
    srt.add_argument("--revision", type=int, default=None,
                     help="restore from this revision instead of the newest one this build can read")
    srt.set_defaults(func=_cmd_session_restore)

    sr = ss.add_parser("rescope", help="re-scope an existing session's context cards")
    sr.add_argument("session", help="session slug or path")
    sr.add_argument("--context", "--cards", metavar="CARDS", dest="context", required=True,
                    help="comma-separated context cards to switch to, or '' for every card. "
                         "Alias: --cards. Required — unlike `init`, omitting it is not a default.")
    sr.add_argument("--json", action="store_true")
    sr.set_defaults(func=_cmd_session_rescope)

    sig = ss.add_parser("import", help="import a session archive into the workspace")
    sig.add_argument("archive", help="path to a .zip produced by `session export`")
    sig.add_argument("--force", action="store_true",
                     help="replace a session of the same slug that already exists here")
    sig.add_argument("--json", action="store_true")
    sig.set_defaults(func=_cmd_session_import)

    sd = ss.add_parser("delete", help="irreversibly remove a session -- `session export` first is "
                       "the undo story; there is no trash")
    sd.add_argument("session", help="session slug or path")
    sd.add_argument("--json", action="store_true")
    sd.set_defaults(func=_cmd_session_delete)
