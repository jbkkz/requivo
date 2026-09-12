"""The session metadata schema, and how a persisted model is read off disk.

Split out of `core/persistence.py` by #550 (the lean pass, #548), to keep `store.py` itself under
the 900-line ceiling: `SessionMeta`/`RevisionRecord`/`ArtifactStatus` (the shape of `session.json`),
`migrate_session` (its version frontier), and `load_model`/`_read_model` (the one door every
`model.json` read goes through, invariant 8). `Store` (in `store.py`) is the only writer of these;
nothing here touches `self` or the filesystem beyond `_read_model`'s own read.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from requivo import __version__
from requivo.core.contracts import EngineOutput, PersistedEngineOutput
from requivo.core.errors import ModelUnreadableError, UnsupportedFormatVersionError, UnsupportedSchemaVersionError

SESSION_FORMAT_VERSION = 1
# The framework's slot schema version. Bumped when the slot vocabulary changes shape; recorded on
# every session so a future reader knows which schema a model was authored against.
SCHEMA_VERSION = 1


def load_model(path: Path) -> EngineOutput:
    """Load a saved model so artifacts can be regenerated without redoing discovery.

    Read through `PersistedEngineOutput` — still an `EngineOutput`, so the annotation holds — because
    a model on disk may have been written by a newer Requivo, and refusing an unknown key there costs
    the reader a session they can otherwise understand completely. The block at the foot of
    `contracts.py` says why the disk side and the provider side answer that question oppositely.

    The explicit codec is #11's and is not optional here either: `_atomic_write` writes UTF-8, so a
    read that takes the platform default decodes a model holding an accented value into mojibake that
    is still valid JSON, on exactly the platforms this repo now has CI legs for."""
    return _read_model(path)


def _read_model(path: Path, *, slug: Optional[str] = None, revision: Optional[int] = None) -> EngineOutput:
    """Read and validate a persisted model, turning every way that can fail into one structured error.

    One helper rather than three call sites, and that is the point rather than tidiness: a guard added
    at two of the three doors is one the third quietly does without, and which door a given verb takes
    is not visible from the verb. The bare `model_validate_json` this replaced sent a truncated
    `model.json` to the operator as a raw pydantic traceback from three CLI verbs and a generic 500
    from the web session page -- `ValidationError` is not a `RequivoError` -- while the remedy sat on
    disk in `revisions/` with nothing saying so (#204).

    `OSError` is caught alongside the parse failures because "there but unreadable" is the same fact
    about the store as "there but unparseable"; a *missing* file is decided by the callers above,
    which raise `SessionNotFoundError` because that has a different remedy. Pinned by
    `test_a_corrupt_model_is_a_structured_error_from_every_door`.
    """
    try:
        return PersistedEngineOutput.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError) as e:
        details: dict = {"path": str(path)}
        if slug is not None:
            details["slug"] = slug
        if revision is not None:
            details["revision"] = revision
        what = f"revision {revision} of session '{slug}'" if revision is not None else (
            f"the model of session '{slug}'" if slug is not None else "the model file")
        remedy = (
            f" Run `requivo session verify {slug}` for the full picture; the session's `revisions/` "
            "directory holds every model that was applied, so an earlier one can be recovered from "
            "there." if slug is not None else ""
        )
        raise ModelUnreadableError(
            f"Could not read {what}: {path} is truncated, mis-encoded, or not a valid model "
            f"({type(e).__name__}).{remedy}",
            details=details,
        ) from e




# ── Canonical session store (.requivo/sessions/<slug>/) ────────────────────────
# The versioned, forward-compatible layout: a session is a directory holding session.json (the
# metadata + provenance), request.md, model.json (the current model), revisions/NNNN-model.json (the
# history, one file per applied revision), and artifacts/ (generated views, each tied to the revision
# it was produced from). Every write is atomic; a revision is preserved before the model is replaced.
# Legacy `out/<slug>/` sessions are read-only and are copied in here only by the explicit
# `requivo session migrate` (`migrate_legacy`). Nothing has read that layout implicitly since 0.9.8.


class ArtifactStatus(BaseModel):
    """Per-artifact provenance in session.json: which model revision produced it, its file, when it
    was written, and whether the model has since moved past that revision (stale)."""
    revision: int
    filename: str
    updated_at: str
    stale: bool = False


class RevisionRecord(BaseModel):
    """Provenance for one applied revision: who produced it and from what. A session's model can be
    moved by more than one surface over its life (the Anthropic provider, a Claude Code turn, the CLI,
    later the Web), so provenance belongs to each *revision*, not just the session's creation. `extra`
    is allowed so a newer Requivo can add a provenance field an older reader simply carries through."""
    model_config = ConfigDict(extra="allow")

    revision: int
    created_at: str
    previous_revision: Optional[int] = None   # the revision this one succeeded (None for the first)
    provider: Optional[str] = None            # "anthropic", "claude-code", "cli", …
    model_name: Optional[str] = None          # the reasoning model, when one produced it
    surface: Optional[str] = None             # the reasoning surface, e.g. "cli-discover", "requivo-answer"
    prompt_version: Optional[str] = None      # "sha256:…" of the prompt, when known
    model_hash: str = ""                      # "sha256:…" of the model payload — content identity
    # Token/rate provenance for a provider-backed apply (#292) — absent for a deterministic apply
    # (session import, a hand-authored `model apply`, a Claude Code turn, which spends no API tokens)
    # and for any revision written before this field existed. Never zero-filled: invariant 6 says
    # provenance is real or absent, and a revision that genuinely spent 0 tokens does not exist.
    usage_input_tokens: Optional[int] = None
    usage_output_tokens: Optional[int] = None
    usage_cache_read_tokens: Optional[int] = None
    usage_cache_write_tokens: Optional[int] = None
    # The rate this revision's calls were actually billed at, `(input, output)` USD per million
    # tokens — stamped rather than looked up again at render time, so a later price-table edit
    # cannot retroactively change what an old revision is reported to have cost (`usage.py`'s own
    # "cost is arithmetic here and nowhere else"). `None` when the calls behind this revision did not
    # all agree on one rate — a genuine disagreement is refused rather than guessed at.
    usage_rate_per_mtok: Optional[tuple[float, float]] = None
    usage_priced_as_of: Optional[str] = None   # the rate table's own date, alongside the rate itself


class SessionMeta(BaseModel):
    """The versioned session metadata (`session.json`). `migrate_session()` is the explicit version
    frontier.

    `extra="allow"` — matching `RevisionRecord` — so a field a *newer* Requivo added survives a
    round-trip through an older one. Under `extra="ignore"` the older reader loaded the session fine
    and then dropped the unknown field the moment it wrote the file back, which turns "an old reader
    tolerates a new field" into "an old reader silently destroys it on first use". Forward
    compatibility is a promise about the file, not just about the load."""
    model_config = ConfigDict(extra="allow")

    format_version: int = SESSION_FORMAT_VERSION
    requivo_version: str = __version__
    session_id: str
    slug: str
    created_at: str
    updated_at: str
    provider: Optional[str] = None          # "anthropic", "claude-code", or None (informational)
    model_name: Optional[str] = None        # the reasoning model, when a provider set one
    context_cards: Optional[list[str]] = None  # the card selection; None == all cards
    request_hash: str = ""               # "sha256:…" of the originating request
    schema_version: int = SCHEMA_VERSION
    # (A session-level `prompt_versions` map lived here and was never written. Prompt identity belongs
    # to the revision that was reasoned with it, not to the session — see RevisionRecord.prompt_version.
    # It is listed in _RETIRED_KEYS so `extra="allow"` doesn't carry the dead key forever.)
    current_revision: int = 0            # 0 == session created but no model applied yet
    revisions: list[RevisionRecord] = Field(default_factory=list)  # provenance log, one per applied revision
    artifact_status: dict[str, ArtifactStatus] = Field(default_factory=dict)


def _now() -> str:
    """UTC, second precision, Z-suffixed — one timestamp format across the whole session file."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def content_hash(text: str) -> str:
    """The persisted hash format — `sha256:<hex>` — as `model_hash` and `request_hash` carry it on disk.

    Public because `integrity.py` recomputes it to check a session against its own recorded hashes.
    A second implementation of this line would drift, and a drifted rehash reports
    `revision_hash_mismatch` against a file nobody touched.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_RETIRED_KEYS = ("prompt_versions",)


def migrate_session(data: dict) -> SessionMeta:
    """The version frontier: turn a raw session.json dict into a `SessionMeta`, upgrading old formats.
    Only v1 exists today, but the boundary is explicit — a session written by a *newer* Requivo is
    rejected clearly rather than silently mis-read. Unknown keys are carried through untouched (see
    `SessionMeta`); known-retired ones are dropped."""
    fv = data.get("format_version", SESSION_FORMAT_VERSION)
    if fv > SESSION_FORMAT_VERSION:
        raise UnsupportedFormatVersionError(
            f"session format v{fv} is newer than this Requivo understands (v{SESSION_FORMAT_VERSION}) "
            "— upgrade requivo.",
            details={"format_version": fv, "supported_format_version": SESSION_FORMAT_VERSION},
        )
    # The slot vocabulary is a second, independent contract, and it was recorded on every session and
    # then read by nothing. A model authored against a newer schema can hold slots this build has no
    # definition for; without this check the first symptom is an `unknown_slot` error naming a slot the
    # user never typed. An *older* schema is fine — that is ordinary backward compatibility.
    sv = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(sv, int) and sv > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"this session was authored against slot schema v{sv}, newer than this Requivo understands "
            f"(v{SCHEMA_VERSION}) — upgrade requivo.",
            details={"schema_version": sv, "supported_schema_version": SCHEMA_VERSION},
        )
    return SessionMeta.model_validate({k: v for k, v in data.items() if k not in _RETIRED_KEYS})

