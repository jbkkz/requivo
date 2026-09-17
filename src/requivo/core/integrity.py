"""Session integrity: does this session directory tell the truth about itself?

It checks the relationships between the files (revision count, one file per revision, model equal
to the last, artifacts pointing at real revisions), takes a *path* so it serves the store and an
extracted archive alike, never writes, and reports rather than raises. The evidence is the directory
and only the directory: nothing outside becomes a verdict
(`test_a_context_card_that_no_longer_resolves_is_not_an_integrity_problem`) and nothing inside sends a
filesystem call outside (`test_a_crafted_artifact_filename_cannot_be_used_to_probe_for_files_outside_the_session`).
A finding carries a `severity`; an artifact type this build cannot name is a note, not a problem
(#260, `test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from requivo.core.contracts import PersistedEngineOutput
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import InvalidFilenameError, RequivoError, UnknownPerimeterError
from requivo.core.perimeters import DEFAULT_PERIMETER, resolve_perimeter
from requivo.core.persistence import (
    canonical_dir,
    content_hash,
    is_contained,
    migrate_session,
    session_lock,
    validate_filename,
)

# `problem` is a broken claim every caller refuses on; `note` is worth naming and not a defect (#260).
SEVERITY_PROBLEM = "problem"
SEVERITY_NOTE = "note"

# What an artifact type must look like to be treated as a plausible future one: a vocabulary token,
# stated separately from `_FILENAME_RE`. A key that is not token-shaped keeps the refusal, under
# `unsafe_artifact_type` (#260): `test_an_artifact_type_that_is_not_a_plausible_token_is_still_a_problem`.
_ARTIFACT_TYPE_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")

# Room for a descriptive compound name and no more: a type is printed once per row on a passing run.
MAX_ARTIFACT_TYPE_LENGTH = 64


@dataclass(frozen=True)
class IntegrityProblem:
    """One finding. `code` is a stable machine token (assert on it, not on the message); `severity`
    defaults to `SEVERITY_PROBLEM`. Filter with `blocking`, not by hand."""
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
    """One revision this build could parse, found while searching for a repair target (#210).
    `payload` is the exact bytes read off disk, so writing it back reproduces the file's own
    `content_hash` and drops no field this build cannot name (invariant 8)."""
    revision: int
    payload: str


def _try_revision(d: Path, i: int, expected_hashes: dict[int, str] | None,
                  perimeter: str = DEFAULT_PERIMETER) -> ReadableRevision | None:
    """One candidate: does `revisions/NNNN-model.json` exist, parse permissively and match the hash
    `expected_hashes` names for it (absent or empty is unconfirmed, not refused)? `perimeter` (#608)
    defaults to software."""
    f = d / "revisions" / f"{i:04d}-model.json"
    if not f.is_file():
        return None
    try:
        payload = f.read_text(encoding="utf-8")
        # permissive, as inspect_session_dir below
        PersistedEngineOutput.model_validate_json(payload, context={"perimeter": perimeter})
    except (OSError, ValidationError, ValueError):
        return None
    expected = (expected_hashes or {}).get(i)
    if expected and content_hash(payload) != expected:
        return None
    return ReadableRevision(i, payload)


def readable_revision(d: Path, revision: int, *, expected_hashes: dict[int, str] | None = None,
                      perimeter: str = DEFAULT_PERIMETER) -> ReadableRevision | None:
    """Is this one revision readable and trustworthy? `None` if missing, unparseable or hash-mismatched,
    the check `session restore --revision N` runs before touching anything."""
    return _try_revision(d, revision, expected_hashes, perimeter)


def newest_readable_revision(d: Path, n: int, *, expected_hashes: dict[int, str] | None = None,
                             perimeter: str = DEFAULT_PERIMETER) -> ReadableRevision | None:
    """The highest revision in `1..n` that exists, parses under the permissive contract and, where
    `expected_hashes` names one, matches its recorded hash; `None` when none does. Never raises and
    decides nothing about repair. `test_newest_readable_revision_skips_a_revision_whose_hash_no_longer_matches`,
    `test_newest_readable_revision_with_no_expected_hashes_trusts_anything_that_parses`."""
    for i in range(n, 0, -1):
        found = _try_revision(d, i, expected_hashes, perimeter)
        if found is not None:
            return found
    return None


def inspect_session_dir(d: Path, *, expected_slug: str | None = None) -> list[IntegrityProblem]:
    """Every finding about the session directory `d`, notes included, in reading order.
    `expected_slug` is the name the caller believes the session has. `check_session_dir` is the one
    to call to decide something (#260)."""
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
    except UnknownPerimeterError as e:
        # Named separately from the generic arm (#608), so a reader can tell an unknown perimeter apart.
        bad("unknown_perimeter", str(e))
        return findings
    except (RequivoError, ValidationError) as e:
        # Both expected: a future format is a RequivoError by design, a wrong shape a ValidationError.
        bad("invalid_session_json", f"session.json is not valid session metadata: {e}")
        return findings
    perimeter = resolve_perimeter(meta.perimeter)

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
            # The permissive contract, matching the loader (#14), against the session's own perimeter (#608).
            PersistedEngineOutput.model_validate_json(payload, context={"perimeter": perimeter})
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
            PersistedEngineOutput.model_validate_json(
                payload, context={"perimeter": perimeter})  # permissive, as above
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
            # A note, not a problem (#260): a new artifact type needs no `format_version` bump, and the
            # diagnostic must not disagree with the loader (invariant 8). Tolerated is not trusted
            # (invariant 14): the type must be token-shaped, and every check below still runs. `atype`
            # is rendered `!r` because it reaches the terminal on a passing run (#40, #70).
            # `test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect`.
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

        # `st.filename` is untrusted: under `pathlib` an absolute component replaces the prefix, so an
        # unvalidated join disclosed whether an outside path existed.
        # `test_a_crafted_artifact_filename_cannot_be_used_to_probe_for_files_outside_the_session`.
        # Not `artifact_path()`, which builds from `canonical_dir(slug)` and this may be an extracted
        # archive; containment is `is_contained`, the store's (invariant 17):
        # `test_an_artifact_symlink_is_reported_unsafe_where_the_platform_cannot_resolve_it`.
        # Classification runs before the existence check, so a refused name is never probed.
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
    """Every internal inconsistency in `d`, in reading order; empty means coherent. The gating answer;
    `inspect_session_dir` is the same walk with the notes left in."""
    return blocking(inspect_session_dir(d, expected_slug=expected_slug))


def inspect_session(slug: str) -> list[IntegrityProblem]:
    """`inspect_session_dir` for a session in the store, under the session's write lock (#263): a
    checker racing `save_revision` could read the old meta against the new model and report a tear
    on a healthy session (invariant 17). A lock held past the timeout raises `SessionLockedError`,
    which callers treat as *no measurement*. `test_check_session_waits_for_a_concurrent_writer_instead_of_reporting_a_tear`."""
    with session_lock(slug):
        return inspect_session_dir(canonical_dir(slug), expected_slug=slug)


def check_session(slug: str) -> list[IntegrityProblem]:
    """`check_session_dir` for a session in the store: the blocking half of `inspect_session`."""
    return blocking(inspect_session(slug))
