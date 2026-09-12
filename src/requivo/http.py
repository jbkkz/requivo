"""RequivoError code -> HTTP status classification (#422). Framework-free: no fastapi, no HTTP
client, no stdlib `http` import (`requivo.http` and stdlib `http` coexist, nothing shadowed).
Moved out of `web/app.py` (#422) so a second HTTP surface can reach it with no extra installed.
Guarded by `test_every_error_code_has_an_explicit_http_status`; per-code rationale lives in
`tests/test_http_status_table.py` (#34, `decision: the-tree-records-the-rule`)."""

from __future__ import annotations

from requivo.core.errors import RequivoError
from requivo.providers.errors import EngineError

# Allowlisted isinstance-only reach into providers/errors.py — see tests/test_boundaries.py.

# Every code needs an explicit row, or it silently defaults below and misreports a server fault as
# the caller's bad request (#34) -- rationale for the rows a test pins: test_http_status_table.py;
# for the rest, docs/compatibility.md's exit-code and status tables.
STATUS_BY_CODE = {
    "session_not_found": 404,
    "invalid_slug": 400,
    "invalid_model": 400,
    "unknown_slot": 400,
    "unknown_context_card": 400,
    "missing_required_slot": 400,
    "invalid_session": 500,  # a store fault is not the caller's bad request (#34)
    "unsupported_format_version": 409,  # not 426: no Upgrade header to send
    "unsupported_schema_version": 409,
    "session_unreadable": 500,
    "model_unreadable": 500,
    "artifact_revision_out_of_range": 500,
    "unreadable_source_revision": 500,
    "inconsistent_archive": 400,
    "unreadable_archive": 400,
    "invalid_archive": 400,
    "import_move_failed": 500,
    "unstated_source_revision": 400,  # not 409: the remedy is entirely the caller's (#57)
    "invalid_filename": 400,
    "empty_selector_token": 400,
    "empty_selection": 400,
    "unsafe_selector_token": 400,
    "unknown_artifact_type": 400,
    "cross_site_request": 403,
    "undetermined_host": 403,
    "host_not_allowed": 403,
    "cross_site_fetch": 403,
    "opaque_origin": 403,
    "origin_mismatch": 403,
    "missing_request_token": 403,
    "spend_ceiling_reached": 403,  # not 429: a budget does not reset with time (#427, `decision: the-http-api-facade`)
    "unauthorized": 401,  # names which credential to send, via WWW-Authenticate (#425 slice 4)
    "input_too_large": 413,
    "revision_conflict": 409,
    "session_exists": 409,
    "import_destination_occupied": 409,
    "context_unreadable": 500,
    "no_context_cards": 500,
    "provider_output_invalid": 502,
    "session_locked": 503,
    "artifact_write_failed": 500,
    "api_token_required": 500,
}

# Deliberately 5xx -- fires only for a code this version has never heard of, the generalised form
# of the bug #34 reports.
UNCLASSIFIED_STATUS = 500


def http_status_for(error: RequivoError) -> int:
    """The HTTP status for a structured error. A function, not an inline lookup, so every code can
    be asserted directly with no real request driven to it. `EngineError` is checked ahead of the
    table: `provider_unavailable` deliberately has no row -- see
    `test_a_provider_transport_failure_is_still_502`."""
    if isinstance(error, EngineError):
        return 502
    return STATUS_BY_CODE.get(error.code, UNCLASSIFIED_STATUS)
