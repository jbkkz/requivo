"""Structured, provider-agnostic errors -- the failure vocabulary of Requivo Core. Surfaced four ways from one
raise: printed in the CLI, read by Claude Code (`.to_dict()`), converted to an HTTP status by `requivo.http`, and
asserted in tests **by code**, never by message. The base carries a stable `code`, an optional dotted `path`, and
a `details` dict: {"code": "missing_required_slot", "message": "...", "path": "model.business_rules", "details":
{"slot": "business_rules"}}. Core raises these; it never imports a provider (a separate family, `providers`)."""

from __future__ import annotations


class RequivoError(Exception):
    """Base for every Requivo failure. `code` is the stable machine identifier; subclasses set it."""

    code = "requivo_error"

    def __init__(self, message: str, *, path: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.path = path
        self.details = details or {}

    def to_dict(self) -> dict:
        """The serializable envelope -- safe for `--json` output, Claude Code, and HTTP."""
        out: dict = {"code": self.code, "message": self.message}
        if self.path is not None:
            out["path"] = self.path
        if self.details:
            out["details"] = self.details
        return out


class InvalidModelError(RequivoError):
    """A proposed model is structurally or semantically invalid."""

    code = "invalid_model"


class UnknownSlotError(InvalidModelError):
    """A model (or a question) names a slot the schema does not define -- a typo or hallucination."""

    code = "unknown_slot"


class MissingRequiredSlotError(InvalidModelError):
    """A model omits a required slot -- it would become invisible to readiness and every view."""

    code = "missing_required_slot"


class UnknownContextCardError(InvalidModelError):
    """A caller named a context card that does not exist -- refused, not dropped (dropping would widen to *all* cards)."""

    code = "unknown_context_card"


class EmptySelectorTokenError(RequivoError):
    """A selector token was empty or whitespace-only. `details`: `{selector, position}` -- refused, not dropped: an empty token is no selection wearing the shape of one (invariant 3)."""

    code = "empty_selector_token"


class UnsafeSelectorTokenError(RequivoError):
    """A selector token carried a control character. `details`: `{selector, position}`. Refused rather
    than escaped-at-render (#40): the token can persist and forge a line of a later report."""

    code = "unsafe_selector_token"


class EmptySelectionError(RequivoError):
    """A selection was supplied and it selects nothing. `details`: `{selector, tokens: 0}` -- a sibling of `EmptySelectorTokenError`, not a subclass (shared code, different shape, until #35)."""

    code = "empty_selection"


class ContextUnreadableError(RequivoError):
    """A context-card directory exists but could not be enumerated -- permissions, usually (distinct from `UnknownContextCardError`, where the card is simply not there)."""

    code = "context_unreadable"


class NoContextCardsError(RequivoError):
    """No context cards are installed at all -- every root was readable and empty. `details`: `{roots}` -- raised, not tolerated: reasoning with an empty `{{CONTEXT}}` on a paid call must never be silent."""

    code = "no_context_cards"


class InputTooLargeError(RequivoError):
    """A supplied text exceeds the engine's ceiling. Raised, not truncated, so a cut copy is never reasoned over as the whole."""

    code = "input_too_large"


class InvalidSessionError(RequivoError):
    """A session on disk is malformed, unreadable, or of an unsupported format version -- a family; nothing raises it directly (#82). `docs/compatibility.md` carries the per-arm `details` table."""

    code = "invalid_session"


class UnsupportedFormatVersionError(InvalidSessionError):
    """The session was written by a newer Requivo than this one. `details`:
    `{format_version, supported_format_version}`."""

    code = "unsupported_format_version"


class UnsupportedSchemaVersionError(InvalidSessionError):
    """The model was authored against a newer slot schema than this build defines. `details`:
    `{schema_version, supported_schema_version}` -- independent of the session format."""

    code = "unsupported_schema_version"


class SessionUnreadableError(InvalidSessionError):
    """`session.json` will not parse, or its write lock could not be opened (#113). `details`: `{slug}`.
    500, not 400: a fact about the store. Deliberately not `session_not_found` (#114)."""

    code = "session_unreadable"


class ModelUnreadableError(InvalidSessionError):
    """`model.json` (or a `revisions/NNNN-model.json`) will not parse or validate. `details`: `{path}`,
    plus `slug`/`revision` when known. A sibling of `session_unreadable`: the session still opens."""

    code = "model_unreadable"


class ArtifactRevisionOutOfRangeError(InvalidSessionError):
    """An artifact was recorded against a revision this session does not have. `details`:
    `{slug, source_revision, current_revision}`."""

    code = "artifact_revision_out_of_range"


class InconsistentArchiveError(InvalidSessionError):
    """An imported archive holds a session that does not tell the truth about itself. `details`:
    `{slug, problems}`, the same codes `session verify` reports. 400: the caller handed us this archive."""

    code = "inconsistent_archive"


class UnreadableArchiveError(InvalidSessionError):
    """The file is not a readable `.zip`. `details`: `{archive}`, no `slug` -- nothing is identified yet."""

    code = "unreadable_archive"


class InvalidArchiveError(InvalidSessionError):
    """The archive opens but its shape is not an export (empty, too many entries or files, past the size
    ceiling, an entry outside one session directory, or more than one session). `details["problem"]`
    names the arm; each adds only the numbers its own sentence quotes (#82, #101, #219)."""

    code = "invalid_archive"


class ImportMoveFailedError(InvalidSessionError):
    """The validated session could not be moved into place. `details`: `{slug}`. 500: the archive was
    fine and the store refused it."""

    code = "import_move_failed"


class SessionNotFoundError(RequivoError):
    """No session matches the given reference (slug or path)."""

    code = "session_not_found"


class InvalidSlugError(RequivoError):
    """A slug is not a safe session identifier -- must be strict kebab-case, or it could escape the store (directory traversal)."""

    code = "invalid_slug"


class InvalidFilenameError(RequivoError):
    """A filename is not a safe name inside a session's `artifacts/` -- `InvalidSlugError`'s sibling, for reads as well as writes."""

    code = "invalid_filename"


class RevisionConflictError(RequivoError):
    """A write expected the session at one revision, but it moved on -- the caller must reload and re-apply."""

    code = "revision_conflict"


class SessionExistsError(RequivoError):
    """A session already occupies that slug. `details`: `{slug}`. Raised by the *creation* itself
    (invariant 11); `session import` is the one raiser where it is a check, ahead of `--force` (#101)."""

    code = "session_exists"


class ImportDestinationOccupiedError(RequivoError):
    """`session import` found something at the slug's directory that is **not** a session. `details`:
    `{slug, path}`. 409, not `session_exists` (no session, so `--force` cannot help) or 500 (#114)."""

    code = "import_destination_occupied"


class SessionLockedError(RequivoError):
    """The write never got to start, so retrying it unchanged is correct -- unlike `RevisionConflictError`,
    where something did race to a conclusion. Raised by `session_lock` and, for a racing discovery, `_discovery_guard` (#209)."""

    code = "session_locked"


class ProviderOutputError(RequivoError):
    """A provider could not be made to return output matching the contract, after every retry -- user-actionable, not an internal defect."""

    code = "provider_output_invalid"


class ArtifactWriteFailedError(RequivoError):
    """A generated artifact was produced (paid for) and only the write failed (#208). `details`:
    `{slug, type, path, cause}`. The remedy is "regenerate": the content is not recoverable from here."""

    code = "artifact_write_failed"


class SpendCeilingReachedError(RequivoError):
    """An injected `SpendPolicy` refused the next call: the ledger shows spend at or above the ceiling (#427).
    `details`: `{ceiling_usd, spent_usd, calls, reason}`; `reason` is `"ceiling_reached"` or `"unpriced_call"`
    (refuse rather than guess a call cost zero, invariant 6). 403, not 429: it does not reset with time."""

    code = "spend_ceiling_reached"
