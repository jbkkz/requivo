"""The report-only session-root diagnostics tier, frozen per CLAUDE.md (#550): `NonSessionEntry`,
`UnexaminableEntry`, `_describe_non_session`, and `_ScanMixin`, the `Store` methods that run a scan.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING

from requivo.core.persistence.identifiers import _is_lock_stem, _shape_only, _stat_exists

_NON_SESSION_SAMPLE = 5


@dataclass(frozen=True)
class NonSessionEntry:
    """Something under the session root that is not a session, described and never interpreted: every
    field is an observation (`test_a_symlink_is_reported_as_one_and_its_target_is_not_read`).
    `slug_shaped` is `_shape_only` (pattern and length, #408:
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken`), never `is_slug`.
    `entries` is capped at `_NON_SESSION_SAMPLE`; None with an `error` is *could not look*, not empty
    (`test_an_entry_that_could_not_be_looked_inside_is_not_reported_as_empty`)."""
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
    """A name under the session root whose examination raised: the partition's third outcome, neither
    a session nor not one. `error` is the exception's text, since the path is what a user acts on."""
    name: str
    error: str

    def to_dict(self) -> dict:
        return {"name": self.name, "error": self.error}




def _describe_non_session(p: Path) -> NonSessionEntry:
    """Describe one entry, and never raise: it runs inside `_session_health`'s one `try`, so an escape
    would report the whole root unlistable (invariant 15). `Exception`, not `OSError`, since a
    non-UTF-8 filename off `iterdir` can fail any path operation. `slug_shaped` is `_shape_only`
    (#408): `p` already occupies the path the reserved-name probe would ask about."""
    slug_shaped = _shape_only(p.name)
    try:
        # `is_symlink` first: `is_dir()` follows, and `iterdir` would list the target's names (invariant 17).
        if p.is_symlink():
            return NonSessionEntry(p.name, "symlink", None, None, None, slug_shaped)
        kind = "directory" if p.is_dir() else ("file" if p.is_file() else "other")
    except Exception as e:  # noqa: BLE001 - a describe that raises blanks a report that succeeded
        # `Path.is_dir()` re-raises EACCES; what this is is then genuinely unknown.
        return NonSessionEntry(p.name, "unknown", None, None, str(e), slug_shaped)
    if kind != "directory":
        return NonSessionEntry(p.name, kind, None, None, None, slug_shaped)
    try:
        names = sorted(c.name for c in p.iterdir())
    except Exception as e:  # noqa: BLE001 - same reason; the kind is known, the contents are not
        return NonSessionEntry(p.name, kind, None, None, str(e), slug_shaped)
    return NonSessionEntry(p.name, kind, names[:_NON_SESSION_SAMPLE], len(names), None, slug_shaped)




class _ScanMixin:
    """The diagnostics third of `Store`, composed rather than duplicated."""

    if TYPE_CHECKING:  # what `Store` provides; declared so pyright can read the mixin alone
        def session_root(self) -> Path: ...
        def lock_root(self) -> Path: ...

    def _scan_session_root(self) -> tuple[list[str], list[Path], list[UnexaminableEntry]]:
        """One listing of the session root, partitioned three ways: sessions, everything else, and the
        entries whose examination raised (#80, #636; metadata errors must not look like absence).
        `test_the_partition_answers_in_three_states_and_the_third_is_neither_neighbour`. Dot-prefixed
        entries are staging areas and in none of the three. A missing root is an empty workspace; a
        root that cannot be listed still raises."""
        root = self.session_root()
        if not _stat_exists(root):
            return [], [], []
        slugs: list[str] = []
        others: list[Path] = []
        unexaminable: list[UnexaminableEntry] = []
        for p in sorted(root.iterdir(), key=lambda p: p.name):
            if p.name.startswith("."):
                continue
            try:
                is_session = _stat_exists(p / "session.json")
            except Exception as e:  # noqa: BLE001 - the third outcome, not a failure of the listing
                # `Exception`, not `OSError`: the ways a probe can fail are open-ended. `BaseException` is not caught.
                unexaminable.append(UnexaminableEntry(p.name, str(e)))
                continue
            if is_session:
                slugs.append(p.name)
            else:
                others.append(p)
        return slugs, others, unexaminable


    def list_session_slugs(self) -> list[str]:
        """Slugs of all canonical sessions, sorted: names known to be sessions, never widened (#80)."""
        return self._scan_session_root()[0]


    def scan_session_root(self) -> tuple[list[str], list[NonSessionEntry], list[UnexaminableEntry]]:
        """All three parts of the session root from one listing (#300,
        `test_the_parts_of_the_session_root_are_one_partition`). The second part costs at the next
        `create_session` on that name, whose rename loses to it (invariant 11,
        `test_the_silent_slug_substitution_the_report_names_is_the_one_that_happens`). A report, not
        a repair (#22). The describe step is here so `list_session_slugs` pays nothing for it."""
        slugs, others, unexaminable = self._scan_session_root()
        return slugs, [_describe_non_session(p) for p in others], unexaminable


    def list_unexaminable_entries(self) -> list[UnexaminableEntry]:
        """Names under the session root whose examination raised (#80), reaching a surface as a fact of
        its own (`test_the_repository_exposes_the_third_bucket`). A report, not a repair."""
        return self.scan_session_root()[2]


    def scan_lock_root(self) -> tuple[list[str], list[str], list[UnexaminableEntry]]:
        """Partition `lock_root()` three ways for `doctor`'s residue check (#180): the slugs a
        `<slug>.lock` names, the entries that are neither that nor a `<slug>.discovering` guard file
        (#209, #391: `test_an_ordinary_discover_leaves_no_lock_residue_doctor_flags`), and the entries
        whose examination raised. The stem question is `_is_lock_stem`'s, shape alone (#409). Answers
        only *is there a lock file*, never *is `slug` still a session*. A root that cannot be listed
        raises: `test_the_lock_root_being_unlistable_is_not_reported_as_no_residue`."""
        root = self.lock_root()
        if not _stat_exists(root):
            return [], [], []
        lock_slugs: list[str] = []
        unexpected: list[str] = []
        unexaminable: list[UnexaminableEntry] = []
        for p in sorted(root.iterdir(), key=lambda p: p.name):
            lock_slug = p.name[: -len(".lock")] if p.name.endswith(".lock") else None
            # The `.discovering` guard file (#209) is never unlinked: recognised, excluded from `unexpected`,
            # not folded into `lock_slugs` (#391).
            guard_slug = p.name[: -len(".discovering")] if p.name.endswith(".discovering") else None
            try:
                is_ordinary_file = S_ISREG(p.stat().st_mode) and not p.is_symlink()
                # `_is_lock_stem`, shape alone (#401, #409): a classification, not a creation.
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

