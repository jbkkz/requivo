"""The report-only session-root diagnostics tier -- frozen per CLAUDE.md's "The persistence
diagnostics tier is frozen".

Split out of `core/persistence.py` by #550 (the lean pass, #548): moving is the only thing that
happens to it. `NonSessionEntry`, `UnexaminableEntry` and `_describe_non_session` describe what a
scan finds; `_ScanMixin` is the `Store` methods that run one (`Store` in `store.py` inherits it,
unchanged method bodies, moved class only).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from requivo.core.persistence.identifiers import _is_lock_stem, _shape_only

_NON_SESSION_SAMPLE = 5


@dataclass(frozen=True)
class NonSessionEntry:
    """Something under the session root that is **not** a session, described and not interpreted.

    A directory holding only `.lock` is almost certainly what `session_lock` left behind before #22,
    and *almost certainly* is not a licence to say so: a half-extracted archive, an interrupted copy
    and a hand-made directory are the same shape from here, and `integrity.py`'s rule is that the
    evidence is the directory and only the directory. So every field is an observation and there is
    deliberately no field spelling a conclusion — a reader acts on the name of the field, not on the
    paragraph beside it. `test_a_symlink_is_reported_as_one_and_its_target_is_not_read`.

    `slug_shaped` is the one derived value, and it is about the *name*: whether `create_session`'s
    rename would reach this directory and collide with it, which is what decides whether the entry
    costs anybody anything. It is `_shape_only` — pattern *and* length, since the pattern alone once
    marked an 81-character name as one a session would silently lose (`test_a_name_too_long_to_be_a_slug_is_not_marked_as_taken`)
    — and deliberately not `is_slug`, whose unconditional creation-time refusal read a *taken*
    reserved name as unreachable and left `doctor`'s `[name taken]` hint silent about the one
    directory it exists to name (#408,
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken`).

    `entries` is capped at `_NON_SESSION_SAMPLE` and `entry_count` is the true total. Three states,
    as everywhere: populated with `error` None (we looked inside); None with an `error` (we could
    not, which must not render like an empty directory —
    `test_an_entry_that_could_not_be_looked_inside_is_not_reported_as_empty`); and None with no
    `error` on a `file` or `other`, where there was nothing to look inside. Telling *empty* from
    *could not look* matters because an empty directory costs nothing on POSIX, where `rename(2)`
    replaces it, and everything on Windows, where `MoveFileEx` refuses any existing destination —
    which is also why `slug_shaped` does not exempt an empty one.
    """
    name: str
    kind: str
    entries: list[str] | None
    entry_count: int | None
    error: str | None
    slug_shaped: bool

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "entries": self.entries,
                "entry_count": self.entry_count, "error": self.error,
                "slug_shaped": self.slug_shaped}


@dataclass(frozen=True)
class UnexaminableEntry:
    """A name under the session root whose examination **raised** — the partition's third outcome.

    Not a session, and not *not* a session: unknown. The probe that decides which one it is failed,
    so both of the other answers would be claims nobody established.

    `error` is the exception's own text rather than a code, for the reason every other third state
    in this codebase keeps it: *permission denied on this path* is a remedy and `unexaminable` is
    not. It carries the path, which is the part a user acts on."""
    name: str
    error: str

    def to_dict(self) -> dict:
        return {"name": self.name, "error": self.error}




def _describe_non_session(p: Path) -> NonSessionEntry:
    """Describe one entry, and **never raise**.

    Totality is the point, not politeness. This runs inside the one `try` in `_session_health` that
    also holds the session listing, so an exception escaping here discards a session report that had
    already succeeded and tells the reader the whole root was unlistable — a claim broader than what
    failed, which is invariant 15's shape one layer down. The two arms below are therefore `Exception`
    rather than `OSError`, and each still lands in a state this entry already has: *we could not stat
    it* and *we could not list it*. That is not the guard-that-provably-cannot-fire invariant 15 warns
    against — it is the same third state reached from a wider set of causes, and the cause I could not
    rule out is real: on Linux a filename that is not valid UTF-8 comes back from `iterdir` carrying
    surrogates, and every consumer of `p.name` downstream is a candidate. APFS refuses such a name, so
    it could not be constructed here to be ruled out either way.

    **`slug_shaped` is `_shape_only(p.name)`, not the full read-time rule, and not `is_slug`** (#408).
    `p` came out of `iterdir()` under `session_root()`, so it already occupies the one path
    `_refuse_new_reserved_slug` would be asked to probe -- that probe could only ever answer "does
    not refuse", so calling it would be a filesystem read this "never raise" function would then have
    to guard, for an answer `p`'s existence already implies. `NonSessionEntry`'s own docstring carries
    what `is_slug` broke here; pinned by
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken`."""
    slug_shaped = _shape_only(p.name)
    try:
        # `is_symlink` first, and it does not follow. `is_dir()` does: a symlink at a slug name
        # pointing anywhere else reported as a plain `directory`, and then `iterdir` listed the
        # **target's** filenames into a report about this workspace. A symlink is a third shape, not
        # a directory, and this file already treats one as the single case a containment guard has to
        # answer for (invariant 17). Found by review.
        if p.is_symlink():
            return NonSessionEntry(p.name, "symlink", None, None, None, slug_shaped)
        kind = "directory" if p.is_dir() else ("file" if p.is_file() else "other")
    except Exception as e:  # noqa: BLE001 - a describe that raises blanks a report that succeeded
        # `Path.is_dir()` swallows only what `_ignore_error` covers — ENOENT, ENOTDIR, ELOOP — and
        # re-raises the rest, EACCES among them. A stat we are not allowed to make lands here, and
        # what this is is then genuinely unknown: answering `other` would be a claim we cannot make.
        return NonSessionEntry(p.name, "unknown", None, None, str(e), slug_shaped)
    if kind != "directory":
        return NonSessionEntry(p.name, kind, None, None, None, slug_shaped)
    try:
        names = sorted(c.name for c in p.iterdir())
    except Exception as e:  # noqa: BLE001 - same reason; the kind is known, the contents are not
        return NonSessionEntry(p.name, kind, None, None, str(e), slug_shaped)
    return NonSessionEntry(p.name, kind, names[:_NON_SESSION_SAMPLE], len(names), None, slug_shaped)




class _ScanMixin:
    """The diagnostics third of `Store` -- see `core/persistence/scan.py`'s module docstring.
    Composed into `Store` (`store.py`) rather than duplicated."""

    def _scan_session_root(self) -> tuple[list[str], list[Path], list[UnexaminableEntry]]:
        """One listing of the session root, partitioned three ways: the canonical sessions, everything
        else, and the entries whose examination raised.

        **Three outcomes, because the predicate can fail** (#80). `Path.exists()` does not swallow
        `EACCES`, so one directory the process cannot stat into aborted the partition for *every*
        entry and `session list` exited 1 with an empty stdout. The third answer belongs in neither
        neighbour: in `others` it never comes back from `list_session_slugs`, which is the invisible
        entry #67 exists to close; in `slugs` it is claimed to *be* a session, the one thing the failed
        probe did not establish. `test_the_partition_answers_in_three_states_and_the_third_is_neither_neighbour`
        and `test_list_session_slugs_still_answers_only_what_is_known_to_be_a_session`, with
        `test_the_probe_the_partition_makes_really_raises_here` as the control.

        Dot-prefixed entries are in none of the three, on purpose: a slug cannot start with a dot, so
        they are `create_session`'s staging areas — a session in flight, and reporting one is a race
        the reader cannot act on.

        A root that does not exist is an empty workspace. A root that cannot be *listed* still raises:
        that failure is genuinely the whole root, there is no entry to name it against, and per-entry
        and whole-root are two claims this function must not merge in either direction."""
        root = self.session_root()
        if not root.exists():
            return [], [], []
        slugs: list[str] = []
        others: list[Path] = []
        unexaminable: list[UnexaminableEntry] = []
        for p in sorted(root.iterdir(), key=lambda p: p.name):
            if p.name.startswith("."):
                continue
            try:
                is_session = (p / "session.json").exists()
            except Exception as e:  # noqa: BLE001 - the third outcome, not a failure of the listing
                # `Exception` rather than `OSError`, for `_describe_non_session`'s reason one function
                # down: the set of ways a probe of a name off a directory listing can fail is open —
                # EACCES here, and on Linux a filename that is not valid UTF-8 comes back from
                # `iterdir` carrying surrogates, which every path operation on `p` is a candidate for.
                # Whatever it was, it lands in a state this partition now has. `BaseException` is not
                # caught: a `KeyboardInterrupt` is not an unexaminable directory.
                unexaminable.append(UnexaminableEntry(p.name, str(e)))
                continue
            if is_session:
                slugs.append(p.name)
            else:
                others.append(p)
        return slugs, others, unexaminable


    def list_session_slugs(self) -> list[str]:
        """Slugs of all canonical sessions, sorted — the backbone of `session list`.

        **Names known to be sessions, and this contract does not widen.** `doctor`, `session verify` and
        every read path reason over what comes back here, so an entry the partition could not examine is
        deliberately not in it — see `list_unexaminable_entries`, which is where it goes instead."""
        return self._scan_session_root()[0]


    def scan_session_root(self) -> tuple[list[str], list[NonSessionEntry], list[UnexaminableEntry]]:
        """All three parts of the session root from **one** listing — and the only way to reach the
        second one, since #300 (see below).

        **One listing, because two scans are two instants** (#300): `doctor` asks all three questions,
        and a `session.json` landing between two scans puts a name in *neither* answer — the invisible
        state #67 is about, reintroduced by the report meant to close it.
        `test_the_parts_of_the_session_root_are_one_partition`.

        **The second part is what nothing could see before #67**, and its cost is not in this module's
        output but at the next `create_session` on that name: the rename that *is* the claim on a slug
        (invariant 11) loses to a directory already there, and `SessionService` falls through to its
        `<slug>-<identity hash>` candidate — a session under a name the user did not ask for, with
        nothing explaining why. `test_the_silent_slug_substitution_the_report_names_is_the_one_that_happens`
        and `test_doctor_names_what_is_under_the_session_root_and_is_not_a_session`.

        **A report, not a repair.** This reads; it never deletes, moves or rewrites. Clearing residue
        on sight is the mistake #22 rejected pointing the other way, and nothing in the directory tells
        a ghost from a half-extracted archive.

        Second of three since #80, not the other half of two — an entry whose examination raised is the
        third part, and folding it into this one would hide it from `session list` for want of a
        `session.json` nobody could look for. The describe step is here rather than in
        `_scan_session_root` so `list_session_slugs` keeps paying nothing for it; the third part carries
        no describe step at all, since whatever we would ask it we have just failed to ask once."""
        slugs, others, unexaminable = self._scan_session_root()
        return slugs, [_describe_non_session(p) for p in others], unexaminable


    def list_unexaminable_entries(self) -> list[UnexaminableEntry]:
        """Names under the session root whose examination raised — the partition's third answer (#80).

        Neither `list_session_slugs` nor `scan_session_root`'s second part returns one, and that is the
        point — `_scan_session_root`'s docstring carries why. It reaches a surface as a fact of its own:
        a degraded row on `session list`, its own line under `doctor`'s sessions check
        (`test_the_repository_exposes_the_third_bucket`,
        `test_doctor_reports_the_entry_instead_of_declaring_the_whole_root_unreadable`).

        **A report, not a repair**: a name and the reason the probe failed, nothing chmod-ed. A caller
        that wants the other parts too takes `scan_session_root()` — this one scans on its own."""
        return self.scan_session_root()[2]


    def scan_lock_root(self) -> tuple[list[str], list[str], list[UnexaminableEntry]]:
        """Partition `lock_root()` three ways, for `doctor`'s lock-residue check (#180): the slugs a
        `<slug>.lock` file names, the entries that are neither that nor a recognised
        `<slug>.discovering` guard file (#209, #391), and the entries whose examination raised. The
        session-root sibling of `_scan_session_root`, one root over.

        **Two regular-file shapes are what this store writes here, and both are recognised** -- a
        `<slug>.lock` from `lock_path`, and `services.discovery`'s `<slug>.discovering` guard file,
        which is deliberately never unlinked (#209) and so outlives every discovery it served. This
        function predates the second shape and #209 never came back to teach it (#391), so every guard
        file read as "not a lock file Requivo recognises" -- about a file this store had just written,
        on the first ordinary discovery a workspace ever ran.
        `test_an_ordinary_discover_leaves_no_lock_residue_doctor_flags`, with
        `test_an_entry_under_lock_root_that_is_not_a_lock_file_is_named_as_unexpected` and
        `test_a_symlink_at_a_lock_name_is_reported_and_not_followed` for what is still reported.

        **The stem question is `_is_lock_stem`'s, and it is shape alone** (#401, corrected by #409):
        asked as `validate_slug`'s creation-time refusal it reported a reserved-name session's own
        files as residue, and asked as the read-time rule it made a fixed file's classification flip
        when an unrelated session was later deleted. `_is_lock_stem` carries the argument;
        `test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue` and
        `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted`.

        **What a matching slug means is left to the caller, deliberately.** This answers only *is there
        a `<slug>.lock` file*, never *is `slug` still a session* — conflating them would make this
        function's answer depend on a root it does not take. `doctor._lock_health` is where the two
        lists meet.

        Three outcomes per entry, on the same reasoning `_scan_session_root` gives for its own third
        bucket (#80): `p.is_symlink()`/`p.is_file()` raise on the errnos `Path.exists()` does not
        swallow. That bucket had a second source from #401 to #409 -- `_is_lock_stem` statting the
        session root -- and #409 removed it, so the file-type probe is now its only one:
        `test_a_reserved_lock_stem_no_longer_probes_the_session_root`. A root that cannot be *listed*
        is left to raise for the caller to report as `readable: False`, never as a clean scan of
        nothing: `test_the_lock_root_being_unlistable_is_not_reported_as_no_residue`."""
        root = self.lock_root()
        if not root.exists():
            return [], [], []
        lock_slugs: list[str] = []
        unexpected: list[str] = []
        unexaminable: list[UnexaminableEntry] = []
        for p in sorted(root.iterdir(), key=lambda p: p.name):
            lock_slug = p.name[: -len(".lock")] if p.name.endswith(".lock") else None
            # `_discovery_guard_path` (services/discovery.py, #209) writes this second shape and never
            # unlinks it -- recognised and excluded from `unexpected`, not folded into `lock_slugs`:
            # it is not a `<slug>.lock` file and answers a different question (#391).
            guard_slug = p.name[: -len(".discovering")] if p.name.endswith(".discovering") else None
            try:
                is_ordinary_file = p.is_file() and not p.is_symlink()
                # `_is_lock_stem`, not `is_slug` (#401), and shape alone -- not a read of the session
                # root (#409). This is a classification, not a creation: whether a writer *here* could
                # have produced this file is a fact about its own name and nothing else. See this
                # method's docstring for what each of the other two questions broke.
                if is_ordinary_file and lock_slug and _is_lock_stem(lock_slug):
                    lock_slugs.append(lock_slug)
                    continue
                if is_ordinary_file and guard_slug and _is_lock_stem(guard_slug):
                    continue
            except Exception as e:  # noqa: BLE001 - the third outcome, not a failure of the listing
                unexaminable.append(UnexaminableEntry(p.name, str(e)))
                continue
            unexpected.append(p.name)
        return lock_slugs, unexpected, unexaminable

