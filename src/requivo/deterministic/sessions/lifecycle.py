"""`requivo session init/list/show/migrate/rescope/delete` -- the lifecycle verbs.

Split out of `deterministic/sessions.py` by #550 (the lean pass, #548): everything about a
session's existence and its bulk migration from the retired `out/` layout. `export`/`import`/
`restore` are `archives.py`'s; `verify` is `verify.py`'s. `register_sessions` (this package's
`__init__.py`) is the single seam `deterministic/__init__.py` binds, unchanged.
"""
from __future__ import annotations

from pathlib import Path

from requivo.core import persistence as store
from requivo.core.errors import InvalidModelError, RequivoError, SessionExistsError, SessionUnreadableError
from requivo.core.persistence import UnexaminableEntry
from requivo.core.selectors import display_token
from requivo.deterministic._shared import _NO_DETAIL, EXIT_DEGRADED, _read_source, _resolve_cards, print_json
from requivo.paths import session_root
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
    `core/persistence/store.py` byte for byte, because `_cmd_session_migrate` compares it against a
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
            # `repo.exists(slug)` belongs inside a per-slug guard too, not only the reads past it --
            # it can refuse a reserved-name legacy slug (#372, invariant 11, #221) and that refusal
            # must not escape this loop uncaught the way #371 closed for the reads further down --
            # `test_session_migrate_survives_a_reserved_name_legacy_directory_beside_a_healthy_one`.
            occupied = repo.exists(slug)
        except RequivoError as e:
            errors.append({"slug": slug, "error": str(e)})
            continue
        if occupied:
            try:
                meta = repo.read_meta(slug)
                # Both reads that decide `interrupted` vs. `skipped` belong inside the same try as
                # `read_meta` above (#371): neither `UnicodeDecodeError` nor an `EACCES`
                # `Path.exists()` re-raises is a `RequivoError`, so an undecodable legacy `request.md`
                # used to escape this per-slug guard entirely, invariant 15's class one read below
                # where #262 already closed it once --
                # `test_session_migrate_survives_one_undecodable_legacy_request_beside_a_healthy_session`.
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




