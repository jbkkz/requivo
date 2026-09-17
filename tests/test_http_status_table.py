"""The RequivoError code -> HTTP-status classification, pinned as a table (#34, #422)."""

from __future__ import annotations

import pytest

# ── the error-code → HTTP status contract (#34) ───────────────────────────────
#
# `STATUS_BY_CODE` used to fall back to 400 for anything unlisted.


def _all_error_codes() -> dict[str, str]:
    """Every `RequivoError` code the package can raise, mapped to the class carrying it."""
    import importlib
    import pkgutil

    import requivo
    from requivo.core.errors import RequivoError

    unimportable = {}
    for m in pkgutil.walk_packages(requivo.__path__, "requivo."):
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa: BLE001
            unimportable[m.name] = f"{type(e).__name__}: {e}"
    # A module that would not import is a hole in the scan set, and a hole here is invisible.
    assert not unimportable, f"scan set incomplete, cannot speak for these codes: {unimportable}"

    found = {RequivoError.code: "requivo.core.errors.RequivoError"}

    def walk(cls):
        for sub in cls.__subclasses__():
            found[sub.code] = f"{sub.__module__}.{sub.__qualname__}"
            walk(sub)

    walk(RequivoError)
    return found


# Codes deliberately absent from `STATUS_BY_CODE`.
_NOT_STATUS_MAPPED = {
    "provider_unavailable":
        "EngineError is classified by the isinstance branch in the handler, ahead of the table, so a "
        "row here would never be read. Pinned by test_a_provider_transport_failure_is_still_502.",
    "requivo_error":
        "the abstract base. Nothing raises it directly; a bare one reaching the handler is a defect, "
        "and the unclassified default is what surfaces it rather than dressing it as the caller's.",
}


def test_every_error_code_has_an_explicit_http_status():
    """The compounding half of #34: a new code is a red leg here, not a wrong answer to a user."""
    from requivo.http import STATUS_BY_CODE

    codes = _all_error_codes()
    # must fire: the walk really found the vocabulary, so the assertions below mean something
    assert len(codes) >= 15, f"scan set looks blind: {sorted(codes)}"
    assert {"session_not_found", "invalid_slug", "cross_site_request"} <= set(codes)

    unclassified = sorted(set(codes) - set(STATUS_BY_CODE) - set(_NOT_STATUS_MAPPED))
    assert not unclassified, (
        "these error codes have no explicit HTTP status and would fall through to the unclassified "
        f"default: { {c: codes[c] for c in unclassified} }")

    both = sorted(set(STATUS_BY_CODE) & set(_NOT_STATUS_MAPPED))
    assert not both, f"claimed unreachable by the table and also listed in it: {both}"

    dead = sorted(set(STATUS_BY_CODE) - set(codes))
    assert not dead, f"status rows for codes nothing raises any more: {dead}"

    stale = sorted(set(_NOT_STATUS_MAPPED) - set(codes))
    assert not stale, f"allowlisted codes that no longer exist: {stale}"


@pytest.mark.parametrize("code, status, why", [
    ("context_unreadable", 500,
     "the server cannot read its own card directory — the operator's environment, not the request"),
    ("no_context_cards", 500,
     "the install shipped no context cards; nothing the caller sent could have avoided it"),
    ("provider_output_invalid", 502,
     "the upstream model would not hold the contract after every retry — same family as a transport "
     "failure, which is already 502"),
    ("session_exists", 409,
     "a conflict with the current state of the store, like revision_conflict"),
    ("session_locked", 503,
     "nothing raced to a conclusion; the write never started and retrying it unchanged is correct"),
    ("empty_selector_token", 400, "a stray comma in what the caller typed"),
    ("empty_selection", 400, "a selection the caller supplied that selects nothing"),
    ("invalid_filename", 400, "a path target the caller supplied"),
    # #101.
    ("invalid_archive", 400, "the caller handed us this archive — the same answer its two siblings "
                             "on the import path already give"),
])
def test_a_server_side_fault_is_not_reported_as_the_users_bad_request(code, status, why):
    """The five decisions this change makes, each pinned with its reason, plus the three the issue confirms
    were already right at 400."""
    from requivo.http import STATUS_BY_CODE
    assert STATUS_BY_CODE[code] == status, why


def test_an_unclassified_code_is_a_server_fault_not_a_bad_request():
    """The default itself. With every known code explicitly mapped (#34)."""
    from requivo.core.errors import RequivoError, SessionNotFoundError
    from requivo.http import UNCLASSIFIED_STATUS, http_status_for

    class _FromTheFuture(RequivoError):
        code = "a_code_this_version_has_never_heard_of"

    assert UNCLASSIFIED_STATUS >= 500
    assert http_status_for(_FromTheFuture("...")) == UNCLASSIFIED_STATUS
    # must fire: a code it *has* heard of is still classified from the table, not defaulted
    assert http_status_for(SessionNotFoundError("...")) == 404


def test_a_provider_transport_failure_is_still_502():
    """The one allowlist entry that is about ordering."""
    from requivo.http import STATUS_BY_CODE, http_status_for
    from requivo.providers.errors import EngineError
    assert http_status_for(EngineError("upstream is down")) == 502
    assert "provider_unavailable" not in STATUS_BY_CODE
