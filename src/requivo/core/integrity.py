"""Session integrity — does this session directory tell the truth about itself?

A session is not one file. It is metadata claiming a revision count, a history of one file per
revision, a current model that should equal the last of them, and artifacts each pointing back at the
revision they came from. Every one of those claims can be false while each individual file is
perfectly valid JSON — an archive that lost its `revisions/`, a hand-edited `session.json`, an
interrupted copy, a model.json swapped out from under its own hash.

Validating the *shape* of each file (which is all `session import` used to do) cannot see any of that,
because nothing is malformed. Only the relationships are broken. This module checks the relationships,
and it is deliberately separate from `persistence`: the same function has to serve a session in the
store (`requivo session verify`, `doctor`) and a directory extracted from an archive that has not been
allowed into the store yet — so it takes a *path*, and it never writes.

It reports rather than raises. A caller decides what a problem means: `session verify` prints them all
and exits non-zero, `session import` refuses the archive, `doctor` names the sessions worth looking at.
Raising on the first one would answer a different, less useful question — "is it broken?" instead of
"what is broken?".

**The evidence is the directory, and only the directory.** One rule binding in both directions — an
integrity answer is derived from the directory's own bytes, importing no fact from the environment
and exporting no question to it:

- *Nothing outside becomes a verdict.* Whether a session's context cards still resolve is a fact
  about the machine, so the same directory would be coherent on one machine and broken on another —
  and `session import` refusing on these problems would make a colleague's good session unimportable
  for want of a card you do not have. That check lives in `core.context.check_selection`, reported
  beside these problems (`test_a_context_card_that_no_longer_resolves_is_not_an_integrity_problem`).
- *Nothing inside sends us outside.* A claim in `session.json` is untrusted input: the recorded
  artifact filename was joined into `artifacts/` and stat-ed unvalidated, and under `pathlib` an
  absolute component replaces the prefix outright, so the answer leaked whether a path existed
  (`test_a_crafted_artifact_filename_cannot_be_used_to_probe_for_files_outside_the_session`). See
  the artifact loop below.

**Not everything worth naming is a defect.** A finding carries a `severity`, and an artifact type
this build has no generator for is a *note* rather than a problem, because `docs/compatibility.md`
lists "a new artifact type" among the changes needing no `format_version` bump and this module used
to refuse one outright (#260) — a diagnostic must be at least as permissive as the loader.
`check_session_dir` returns the blocking half, so every existing caller's default is the safe one;
`inspect_session_dir` returns everything, for the surfaces that report rather than gate.
`test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from requivo.core.contracts import PersistedEngineOutput
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import InvalidFilenameError, RequivoError
from requivo.core.persistence import (
    canonical_dir,
    content_hash,
    is_contained,
    migrate_session,
    session_lock,
    validate_filename,
)

# The two severities a finding can carry.
#
# `problem` is a broken claim: the session does not tell the truth about itself, and every caller
# refuses on it. `note` is something worth *naming* that is not a defect, and the only member today
# is an artifact type this build has no generator for (#260).
SEVERITY_PROBLEM = "problem"
SEVERITY_NOTE = "note"

# What an artifact *type* must look like for this module to treat it as a plausible future one. The
# same shape as `_FILENAME_RE` in the store, and deliberately a separate statement of it: a type is a
# vocabulary token and a filename is a path component, `ARTIFACT_FILENAMES` maps one onto the other,
# and the two are free to diverge. Lowercase runs of [a-z0-9] joined by a single `.`, `-` or `_`,
# which is what every key in `ARTIFACT_FILENAMES` already is.
#
# It exists because tolerating an unknown type widens a door that used to be shut: before #260 an
# archive carrying arbitrary `artifact_status` keys was refused outright by `session import`, and a
# tolerated key is one this build will accept, store, and print on a *passing* `doctor` run for as
# long as the session lives. A key that is not token-shaped is not a newer Requivo's generator — it
# is junk or a forgery — so it keeps the refusal, under `unsafe_artifact_type` rather than under the
# note's code, because a consumer has to be able to tell the two apart.
# `test_an_artifact_type_that_is_not_a_plausible_token_is_still_a_problem` is the guard.
_ARTIFACT_TYPE_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")

# Room for a descriptive compound name and no more. An artifact type is printed once per row by
# `doctor` and by `session verify`, on what is now a code path that *passes*, so an unbounded one is
# a reader's whole screen. Generous against the longest key those tables could plausibly hold.
MAX_ARTIFACT_TYPE_LENGTH = 64


@dataclass(frozen=True)
class IntegrityProblem:
    """One finding. `code` is a stable machine token (assert on it, not on the message).

    `severity` is additive and defaults to `SEVERITY_PROBLEM`, so every construction and every
    `to_dict()` consumer that predates #260 keeps exactly the meaning it had. Filter with `blocking`
    rather than by hand: `severity != SEVERITY_PROBLEM` and `severity == SEVERITY_NOTE` differ the
    moment a third value exists, and only one of the two is the safe reading."""
    code: str
    message: str
    severity: str = SEVERITY_PROBLEM

    @property
    def is_problem(self) -> bool:
        return self.severity == SEVERITY_PROBLEM

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


def blocking(findings: list[IntegrityProblem]) -> list[IntegrityProblem]:
    """The findings that mean the session does not tell the truth about itself, in reading order."""
    return [f for f in findings if f.is_problem]


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as e:
        return None, str(e)


def _is_revision(filename: str, n: int) -> bool:
    """Whether `NNNN-model.json` names a revision this session claims to have."""
    head = filename.split("-", 1)[0]
    return head.isdigit() and 1 <= int(head) <= n


@dataclass(frozen=True)
class ReadableRevision:
    """One revision this build could parse, found while searching for a repair target (#210) — the
    pair `newest_readable_revision` returns. `payload` is the exact bytes read off disk, not a
    round-trip through a pydantic model: a caller that writes it back onto `model.json` unchanged
    reproduces the revision file's own `content_hash`, which is what lets `check_session_dir`'s
    `model_is_not_the_last_revision` check pass afterwards without this module recomputing anything.
    Re-serializing a parsed model instead would risk silently dropping a field this build cannot
    name — exactly the loss invariant 8 warns `resolve()` about, one file along."""
    revision: int
    payload: str


def _try_revision(d: Path, i: int, expected_hashes: dict[int, str] | None) -> ReadableRevision | None:
    """One candidate: does `revisions/NNNN-model.json` exist, parse under the permissive contract,
    and — when `expected_hashes` names a hash for it — match it? Shared by `newest_readable_revision`
    (searching) and `readable_revision` (checking one specific number), so the two never state the
    same three-part check twice. `expected` absent or empty is unconfirmed rather than refused, the
    same tolerance `inspect_session_dir` gives a legacy revision record with no recorded hash."""
    f = d / "revisions" / f"{i:04d}-model.json"
    if not f.is_file():
        return None
    try:
        payload = f.read_text(encoding="utf-8")
        PersistedEngineOutput.model_validate_json(payload)  # permissive, as inspect_session_dir below
    except (OSError, ValidationError, ValueError):
        return None
    expected = (expected_hashes or {}).get(i)
    if expected and content_hash(payload) != expected:
        return None
    return ReadableRevision(i, payload)


def readable_revision(d: Path, revision: int, *,
                      expected_hashes: dict[int, str] | None = None) -> ReadableRevision | None:
    """Is this one specific revision readable and trustworthy? `None` if the file is missing, does
    not parse, or does not match its recorded hash — the check `session restore`'s explicit
    `--revision N` runs before touching anything, so a caller-named target is refused on exactly the
    same grounds `newest_readable_revision`'s search would have skipped it on, never on a looser one.
    See `_try_revision` for what "readable and trustworthy" means."""
    return _try_revision(d, revision, expected_hashes)


def newest_readable_revision(d: Path, n: int, *,
                             expected_hashes: dict[int, str] | None = None) -> ReadableRevision | None:
    """The highest revision number in `1..n` whose `revisions/NNNN-model.json` exists, parses under
    the same permissive contract `inspect_session_dir` checks it against, and — when
    `expected_hashes` names one — matches the hash `session.json`'s own revision log recorded for
    it. Read newest first, because a repair wants the most recent state this build can trust, not
    the oldest one that happens to still parse. `None` when nothing in that range is readable, which
    is the answer a documented recovery path has to be able to give honestly rather than invent one
    for.

    **A file that parses is not automatically trustworthy, and `session restore` (#210) exists to be
    trustworthy.** A revision file tampered with after it was frozen still parses as a perfectly good
    model, and `inspect_session_dir` already refuses it as `revision_hash_mismatch` — so restoring
    without the identical check would let a repair tool trust what its paired diagnostic does not.
    `test_newest_readable_revision_skips_a_revision_whose_hash_no_longer_matches`, with
    `test_newest_readable_revision_with_no_expected_hashes_trusts_anything_that_parses` for the
    unconfirmed case: a revision absent from `expected_hashes`, or recorded with an empty hash, is
    unconfirmed rather than refused, because this answers *can this build open it*.

    Deliberately *not* a verdict about the session: it never raises and decides nothing about repair,
    only which history this build can open and trust. It re-validates each candidate independently
    rather than recovering that from `inspect_session_dir`'s findings, which do not carry "which
    revisions parsed" in a form a caller could use without re-deriving this same loop.
    """
    for i in range(n, 0, -1):
        found = _try_revision(d, i, expected_hashes)
        if found is not None:
            return found
    return None


def inspect_session_dir(d: Path, *, expected_slug: str | None = None) -> list[IntegrityProblem]:
    """Every finding about the session directory `d`, in reading order — notes included.

    `expected_slug` is the name the caller believes the session has — the directory name in the store,
    or the folder name inside an archive. A session that disagrees with its own container about its
    identity is the first thing to catch, because every later check keys on it.

    **`check_session_dir` is the one to call to decide something**; this one is for a surface that
    reports. The split is which way the *default* fails (#260): a caller asking "is this session
    sound" and getting the whole list back would gate on a note, silently, since the list is non-empty
    either way. `test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect`.
    """
    findings: list[IntegrityProblem] = []

    def bad(code: str, message: str) -> None:
        findings.append(IntegrityProblem(code, message))

    def note(code: str, message: str) -> None:
        findings.append(IntegrityProblem(code, message, severity=SEVERITY_NOTE))

    meta_path = d / "session.json"
    if not meta_path.is_file():
        bad("no_session_json", f"{d.name}/session.json is missing — this is not a session directory")
        return findings
    raw, err = _read_json(meta_path)
    if raw is None:
        bad("unreadable_session_json", f"session.json cannot be read: {err}")
        return findings
    try:
        meta = migrate_session(raw)
    except (RequivoError, ValidationError) as e:
        # Both are expected here and neither should escape as a traceback: a *future* format is a
        # RequivoError by design, and a structurally wrong session.json is a Pydantic ValidationError.
        bad("invalid_session_json", f"session.json is not valid session metadata: {e}")
        return findings

    if expected_slug is not None and meta.slug != expected_slug:
        bad("slug_mismatch",
            f"the directory is {expected_slug!r} but session.json says {meta.slug!r} — the session "
            "does not agree with itself about its own identity")

    # ── the revision log ────────────────────────────────────────────────────────
    n = meta.current_revision
    if n < 0:
        bad("negative_revision", f"current_revision is {n}")
        return findings
    if len(meta.revisions) != n:
        bad("revision_count_mismatch",
            f"session.json says revision {n} but its log holds {len(meta.revisions)} record(s) — the "
            "history does not account for the model that is there")

    seen_hashes: dict[int, str] = {}
    for i, rec in enumerate(meta.revisions, start=1):
        if rec.revision != i:
            bad("revision_out_of_order",
                f"revision record {i} is numbered {rec.revision} — the log must be 1..N in order")
            continue
        expected_prev = None if i == 1 else i - 1
        if rec.previous_revision != expected_prev:
            bad("revision_chain_broken",
                f"revision {i} records previous_revision={rec.previous_revision}, expected "
                f"{expected_prev}")
        seen_hashes[i] = rec.model_hash

        f = d / "revisions" / f"{i:04d}-model.json"
        if not f.is_file():
            bad("missing_revision_file", f"revisions/{i:04d}-model.json is missing")
            continue
        payload = f.read_text(encoding="utf-8")
        if rec.model_hash and content_hash(payload) != rec.model_hash:
            bad("revision_hash_mismatch",
                f"revisions/{i:04d}-model.json does not match the hash recorded for it — the file "
                "was changed after it was written")
        try:
            # The permissive contract, matching `load_revision_model`: a field a newer Requivo added
            # is legal on disk, so a checker that refused it would report a defect in a session that
            # opens perfectly well — the diagnostic disagreeing with the loader about the same file
            # is worse than either answer on its own (#14).
            PersistedEngineOutput.model_validate_json(payload)
        except (ValidationError, ValueError) as e:
            bad("invalid_revision_model", f"revisions/{i:04d}-model.json is not a valid model: {e}")

    rev_dir = d / "revisions"
    if rev_dir.is_dir():
        extra = sorted(p.name for p in rev_dir.glob("*-model.json") if not _is_revision(p.name, n))
        if extra:
            bad("orphan_revision_file",
                f"revisions/ holds file(s) beyond revision {n}: {', '.join(extra)}")

    # ── the current model ───────────────────────────────────────────────────────
    model_path = d / "model.json"
    if n == 0:
        if model_path.is_file():
            bad("model_without_revision",
                "model.json exists but session.json is at revision 0 — a model that no revision "
                "accounts for has no provenance at all")
    elif not model_path.is_file():
        bad("missing_model", f"session.json is at revision {n} but there is no model.json")
    else:
        payload = model_path.read_text(encoding="utf-8")
        try:
            PersistedEngineOutput.model_validate_json(payload)  # permissive, as above
        except (ValidationError, ValueError) as e:
            bad("invalid_model", f"model.json is not a valid model: {e}")
        last_hash = seen_hashes.get(n)
        if last_hash and content_hash(payload) != last_hash:
            bad("model_is_not_the_last_revision",
                f"model.json does not match revision {n}, the revision it is supposed to be — "
                "the current model and the history describe different states")

    # ── artifacts ───────────────────────────────────────────────────────────────
    artifacts = d / "artifacts"
    for atype, st in meta.artifact_status.items():
        if atype not in ARTIFACT_FILENAMES:
            # **A note, not a problem** (#260). `docs/compatibility.md` lists "a new artifact type"
            # among the changes that need no `format_version` bump, and refusing one here made the
            # first generator a later Requivo ships turn every session it had touched into a defect
            # on this build: `session verify` non-zero, `doctor` naming it, `session import` refusing
            # a colleague's archive — while `read_meta` opens the very same file without complaint.
            # The diagnostic disagreeing with the loader about one file is the worse of the two
            # answers (invariant 8), and it is the correction #14 already made one field along, for
            # a *model* key from a newer version.
            #
            # Tolerated is not trusted (invariant 14), in two ways. The type must be *shaped* like
            # one, or it keeps the refusal below. And nothing further down this loop is skipped: the
            # recorded filename still goes through `validate_filename` and `is_contained`, the file
            # still has to be there, and the revision still has to exist — so a type this build
            # cannot name buys an archive no relaxation of any other check.
            #
            # `atype` is untrusted and reaches the terminal, so it is rendered `!r` for the same
            # reason the filename beside it is: `session show` and `artifact list` already escape
            # this very dict key (#70), and a note prints on a run that *passes*, where a forged row
            # at column 0 is least likely to be doubted (#40).
            # `test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect` is the guard.
            if _ARTIFACT_TYPE_RE.match(atype) and len(atype) <= MAX_ARTIFACT_TYPE_LENGTH:
                note("unknown_artifact_type",
                     f"session.json records an artifact of unknown type {atype!r} — this build has "
                     "no generator by that name, which is what a session written by a newer Requivo "
                     "looks like. Nothing else about that entry is assumed")
            else:
                bad("unsafe_artifact_type",
                    f"session.json records an artifact under {atype[:MAX_ARTIFACT_TYPE_LENGTH]!r}, "
                    "which is not shaped like an artifact type — a plain lowercase name such as "
                    "'risk-register', no longer than "
                    f"{MAX_ARTIFACT_TYPE_LENGTH} characters")
        elif st.filename != ARTIFACT_FILENAMES[atype]:
            bad("artifact_filename_mismatch",
                f"the {atype!r} artifact is recorded as {st.filename!r}, but that type is stored as "
                f"{ARTIFACT_FILENAMES[atype]!r}")

        # `st.filename` is an unconstrained `str` out of session.json, and the two branches above only
        # *record* a problem -- execution carried on to the join with the untrusted value in hand, so
        # neither was a guard. `pathlib` makes the absolute case the sharp one: an absolute component
        # replaces everything before it, so the join never had to escape upwards at all, and the row
        # coming back `missing_artifact_file` disclosed whether an outside path existed.
        # `test_a_crafted_artifact_filename_cannot_be_used_to_probe_for_files_outside_the_session`.
        #
        # `artifact_path()` is deliberately not reused: it builds from `canonical_dir(slug)`, while
        # this function is also handed a directory extracted from an archive that is not in the store
        # -- it would answer confidently about the wrong directory, this module's own defect class.
        # The containment confirmation is `is_contained`, the store's, rather than a third statement
        # of the rule that had to be corrected twice for defects its siblings were each corrected for
        # separately (#3, invariant 17) --
        # `test_an_artifact_symlink_is_reported_unsafe_where_the_platform_cannot_resolve_it`.
        #
        # The classification runs before the existence check below, and that ordering is what keeps a
        # refused name reported as refused rather than probed and then reported as absent.
        try:
            f = artifacts / validate_filename(st.filename)
            safe = is_contained(f, artifacts)
        except (InvalidFilenameError, OSError, ValueError):
            safe = False
        if not safe:
            bad("unsafe_artifact_filename",
                f"the {atype!r} artifact is recorded under {st.filename!r}, which this session "
                "cannot confirm is a bare file inside artifacts/ — refused without checking whether "
                "it exists")
        elif not f.is_file():
            bad("missing_artifact_file",
                f"session.json records a {atype!r} artifact but artifacts/{st.filename} is missing")

        if not 1 <= st.revision <= n:
            bad("artifact_revision_out_of_range",
                f"the {atype!r} artifact claims to come from revision {st.revision}, which this "
                f"session does not have (it has 1..{n or 0})")

    return findings


def check_session_dir(d: Path, *, expected_slug: str | None = None) -> list[IntegrityProblem]:
    """Every internal inconsistency in the session directory `d`, in reading order. Empty == coherent.

    The gating answer, and the one whose meaning has not changed: a caller that refuses on a non-empty
    return refuses on exactly the same set it always did. `inspect_session_dir` is the same walk with
    the notes left in, for a surface that reports instead of deciding.
    """
    return blocking(inspect_session_dir(d, expected_slug=expected_slug))


def inspect_session(slug: str) -> list[IntegrityProblem]:
    """`inspect_session_dir` for a session in the store, taken under the session's write lock (#263).

    `check_session_dir` reads session.json, then the revision files, then model.json, and none of
    that used to be locked. `save_revision` writes the same three things in the same order but not
    atomically as one unit -- the frozen revision file and model.json land first, session.json last
    (see the comment on that ordering) -- so a checker racing a writer could read the *old* meta
    against the *new* model and report `model_is_not_the_last_revision`, "the file was changed after
    it was written", about a session that is perfectly healthy and merely mid-save. That is
    invariant 17's own class ("a check that can answer differently for the same argument depending
    on when it runs is not a check") landing in the one verb whose entire job is truth-telling.

    Taking the lock here, rather than in `check_session_dir`, keeps that function usable on an
    *extracted archive* directory during `session import` -- there is no session in the store yet to
    lock, and no writer that could be racing one. `session_lock` also reports a session that has
    vanished exactly the way every other store-side caller already reports it
    (`SessionNotFoundError`), so this needs no case of its own for that. Writes hold the lock for
    milliseconds, so waiting for one costs nothing measurable; a lock held past `_LOCK_TIMEOUT_SECONDS`
    raises `SessionLockedError`, which callers must treat as *no measurement*, never as *inconsistent*
    -- see `_cmd_session_verify` and `_session_health`, which both catch it for exactly that reason.
    Pinned by `test_check_session_waits_for_a_concurrent_writer_instead_of_reporting_a_tear`.
    """
    with session_lock(slug):
        return inspect_session_dir(canonical_dir(slug), expected_slug=slug)


def check_session(slug: str) -> list[IntegrityProblem]:
    """`check_session_dir` for a session in the store -- the blocking half of `inspect_session`,
    including its lock (#263)."""
    return blocking(inspect_session(slug))
