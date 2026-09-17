"""The session metadata schema (`SessionMeta`/`RevisionRecord`/`ArtifactStatus`), `migrate_session`
(the version frontier) and `load_model`/`_read_model`, the one door every `model.json` read goes
through (#550, invariant 8). `Store` is the only writer.
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
from requivo.core.perimeters import DEFAULT_PERIMETER, resolve_perimeter

SESSION_FORMAT_VERSION = 1
# The slot schema version, recorded on every session.
SCHEMA_VERSION = 1


def load_model(path: Path, perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
    """Load a saved model through `PersistedEngineOutput` (a newer Requivo's key must not cost the
    reader the session). `perimeter` (#608) is the session's own, software for a bare `model.json`.
    Explicit UTF-8 (#11)."""
    return _read_model(path, perimeter=perimeter)


def _read_model(path: Path, *, slug: Optional[str] = None, revision: Optional[int] = None,
                perimeter: str = DEFAULT_PERIMETER) -> EngineOutput:
    """Read and validate a persisted model, turning every failure into one structured error (#204):
    a `ValidationError` is not a `RequivoError`. `OSError` is caught too; a missing file is the
    caller's `SessionNotFoundError`. `test_a_corrupt_model_is_a_structured_error_from_every_door`."""
    try:
        return PersistedEngineOutput.model_validate_json(
            path.read_text(encoding="utf-8"), context={"perimeter": perimeter})
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
# session.json, request.md, model.json, revisions/NNNN-model.json, artifacts/. Every write is atomic;
# a revision is preserved before the model is replaced. Legacy `out/` is read only by `session migrate`.


class ArtifactStatus(BaseModel):
    """Per-artifact provenance in session.json: the revision that produced it, its file, when, and `stale`."""
    revision: int
    filename: str
    updated_at: str
    stale: bool = False


class RevisionRecord(BaseModel):
    """Provenance for one applied revision; `extra="allow"` so a newer Requivo's field is carried through."""
    model_config = ConfigDict(extra="allow")

    revision: int
    created_at: str
    previous_revision: Optional[int] = None   # the revision this one succeeded (None for the first)
    provider: Optional[str] = None            # "anthropic", "claude-code", "cli", …
    model_name: Optional[str] = None          # the reasoning model, when one produced it
    surface: Optional[str] = None             # the reasoning surface, e.g. "cli-discover", "requivo-answer"
    prompt_version: Optional[str] = None      # "sha256:…" of the prompt, when known
    model_hash: str = ""                      # "sha256:…" of the model payload — content identity
    # Token/rate provenance for a provider-backed apply (#292); absent, never zero-filled, otherwise (invariant 6).
    usage_input_tokens: Optional[int] = None
    usage_output_tokens: Optional[int] = None
    usage_cache_read_tokens: Optional[int] = None
    usage_cache_write_tokens: Optional[int] = None
    # The rate these calls were billed at, stamped rather than looked up at render time; `None` when
    # the calls disagreed on one.
    usage_rate_per_mtok: Optional[tuple[float, float]] = None
    usage_priced_as_of: Optional[str] = None   # the rate table's own date, alongside the rate itself


class SessionMeta(BaseModel):
    """The versioned session metadata (`session.json`); `migrate_session()` is the version frontier.
    `extra="allow"`, so an older reader does not destroy a newer field on its first write."""
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
    # The session's perimeter (#608), half of identity (invariant 11); `None` reads as software, and
    # an unrecognised name is refused in `migrate_session`: a perimeter is interpreted, not carried.
    perimeter: Optional[str] = None
    # A session-level `prompt_versions` map lived here and is retired (`_RETIRED_KEYS`).
    current_revision: int = 0            # 0 == session created but no model applied yet
    revisions: list[RevisionRecord] = Field(default_factory=list)  # provenance log, one per applied revision
    artifact_status: dict[str, ArtifactStatus] = Field(default_factory=dict)


def _now() -> str:
    """UTC, second precision, Z-suffixed: the one timestamp format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def content_hash(text: str) -> str:
    """The persisted hash format, `sha256:<hex>`; public so `integrity.py` recomputes the same line."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_RETIRED_KEYS = ("prompt_versions",)


def migrate_session(data: dict) -> SessionMeta:
    """The version frontier: a raw session.json dict to a `SessionMeta`; a newer format is refused
    clearly, unknown keys are carried through, retired ones dropped."""
    fv = data.get("format_version", SESSION_FORMAT_VERSION)
    if fv > SESSION_FORMAT_VERSION:
        raise UnsupportedFormatVersionError(
            f"session format v{fv} is newer than this Requivo understands (v{SESSION_FORMAT_VERSION}) "
            "— upgrade requivo.",
            details={"format_version": fv, "supported_format_version": SESSION_FORMAT_VERSION},
        )
    # A newer slot schema is refused; an older one is ordinary backward compatibility.
    sv = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(sv, int) and sv > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"this session was authored against slot schema v{sv}, newer than this Requivo understands "
            f"(v{SCHEMA_VERSION}) — upgrade requivo.",
            details={"schema_version": sv, "supported_schema_version": SCHEMA_VERSION},
        )
    # The one field the permissive rule is inverted for (#608): a perimeter is interpreted, so an
    # unrecognised one is refused by name; `None` resolves to software.
    resolve_perimeter(data.get("perimeter"))
    return SessionMeta.model_validate({k: v for k, v in data.items() if k not in _RETIRED_KEYS})

