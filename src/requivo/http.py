"""RequivoError code -> HTTP status classification (#422).

Framework-free, deliberately: no fastapi, no HTTP client, and no import of the stdlib `http`
package either -- verified directly rather than assumed, since Python 3's absolute-import default
means `import http` inside this module (or anywhere else in the tree) resolves to the standard
library regardless of this module's own name; the two coexist as distinct modules
(`requivo.http` vs. `http`) with nothing to shadow. `errors_http.py` was the recorded fallback name
if that had come out otherwise -- it did not, so this is the `paths.py`/`streams.py`/`usage.py`
single-word shape.

Moved out of `web/app.py`, which is where this table and `http_status_for` (as `_status_for`) lived
until #422: `_STATUS_BY_CODE`/`_status_for` were private names in a module that imports fastapi at
module scope, so a second HTTP surface -- the hosted product that already imports this package, or a
future local API facade -- could reach them only by importing a private name out of the optional
`[web]` extra, or by forking the table. One consumer had already forked it, mapping every
`RequivoError` to a blanket 502. This module is importable with **no extra installed**
(`pip install requivo` alone); `web/app.py` now imports from it and keeps thin private aliases so its
own call sites read the same as before.

The guard that keeps the table honest, `test_every_error_code_has_an_explicit_http_status`, moved
with it -- from `tests/web/test_web_routing.py` to `tests/test_http_status_table.py` -- since it no
longer needs the `[web]` extra to be collectible either; see that module's own docstring for the #34
lesson it guards (a code with no explicit row used to default to 400, so an entirely server-side
fault like `context_unreadable` reached the reader as "your request was bad").
"""

from __future__ import annotations

from requivo.core.errors import RequivoError
from requivo.providers.errors import EngineError

# `EngineError` is the one name this module reaches into `requivo.providers` for. It is a deliberate
# reach, not a leak: `providers/errors.py` is the provider protocol's own failure type, carries no
# SDK import, and is already used exactly this way -- an isinstance check, never a call -- from three
# scanned surfaces (`cli.py`, and until this move, `web/app.py` and `web/routes/sessions.py`).
# `tests/test_boundaries.py`'s surface-provider guard now scans this module the same way it scans
# `cli.py` (an individually named subject, not a tree) and carries an allowlist entry for exactly
# this import, with the same reasoning restated at the guard -- so a future import that is a real
# provider *call* rather than a type check still goes red there, while this one does not.

# Map a structured error code to an HTTP status.
#
# **Every code gets a row.** This used to default to 400 for anything unlisted, which meant a code
# added in one lane arrived at the browser wearing a plausible, wrong status rather than as an
# obvious gap: `context_unreadable` — the server unable to read its own card directory — was
# reported to the reader as "your request was bad" (#34). Six codes were sitting on that default;
# for two of them 400 happened to be right, four were wrong, and one of the four had been noticed.
# `test_every_error_code_has_an_explicit_http_status` walks the RequivoError subclasses and fails on
# the next omission, so the table cannot silently fall behind the vocabulary again.
#
# The 4xx/5xx split is the question the default got wrong: is this about what the caller sent, or
# about the state of the server they sent it to?
STATUS_BY_CODE = {
    # 4xx — the caller's request
    "session_not_found": 404,
    "invalid_slug": 400,
    "invalid_model": 400,
    "unknown_slot": 400,
    "unknown_context_card": 400,
    "missing_required_slot": 400,
    # The malformed-session family, one row per arm since #82. The arms disagree about the 4xx/5xx
    # question — that disagreement is *why* they are eight codes rather than one, and it is the third
    # of the three things that made this split worth doing before a 1.0 (the other two: `details`
    # shapes with no common key, and a documented promise the code could not carry).
    #
    # The family base answers **500**, not the 400 it inherited. Nothing raises it directly, so the
    # number is nominal — but a nominal number is still a number a reader sees, and "a session on disk
    # is malformed" is a fact about the store. The old 400 was the same misattribution #34 fixed for
    # `context_unreadable`: telling the reader their request was bad when the server could not read
    # its own state.
    "invalid_session": 500,           # the family base; nothing raises it directly
    # 409, not 426. Upgrade Required is defined for *connection protocol* negotiation and the spec
    # requires an `Upgrade` header naming what to move to; we have none to send, and the thing needing
    # an upgrade is the reader's Requivo rather than the transport. A status that is exactly right in
    # English and wrong in its RFC is worse than a general one, because the next client is written
    # against the RFC. Conflict is what this is: the resource's state against what this build can do.
    "unsupported_format_version": 409,
    "unsupported_schema_version": 409,
    "session_unreadable": 500,        # the store, not the request
    # 500 for the same reason, and a row of its own because the remedy differs: an unreadable
    # `session.json` cannot be recovered from anything on disk, while an unreadable `model.json`
    # leaves the session openable and every applied model sitting in `revisions/` (#204).
    "model_unreadable": 500,
    "artifact_revision_out_of_range": 500,
    "unreadable_source_revision": 500,  # a real revision was stated; the history is what is incomplete
    "inconsistent_archive": 400,      # the caller handed us this archive
    "unreadable_archive": 400,        # …and this one
    # …and the seven shape refusals between them, which kept `invalid_model` — a code about a
    # proposal — until #101. 400 for the same reason as its two siblings, and not 5xx: nothing is
    # written to the store until the archive has passed, so the store is not what is wrong. Not 409
    # either: nothing in the store conflicts with it, and re-sending the same archive unchanged can
    # never succeed. The status does not move — 400 before under `invalid_model`, 400 now — so what
    # a consumer sees change is the code, which is exactly what #101 set out to move.
    "invalid_archive": 400,           # …and the seven shapes in between (#101)
    "import_move_failed": 500,        # the archive was fine and the store refused it
    # 400 and not 409: an `artifact save` with no source revision is not a conflict with the store's
    # state, it is a request that never said the one thing only the caller knows. Nothing about the
    # session is wrong, and the remedy is entirely in the caller's hands — state the revision (#57).
    "unstated_source_revision": 400,
    "invalid_filename": 400,          # a path target the caller supplied
    "empty_selector_token": 400,      # a stray comma in what the caller typed
    "empty_selection": 400,           # a selection the caller supplied that selects nothing
    "unsafe_selector_token": 400,     # a control character in a name the caller supplied
    "unknown_artifact_type": 400,
    # The cross-site family, one row per arm since #52. They are all 403 and the guard renders that
    # status directly rather than reading this table — these rows classify the *codes*, which is what
    # `test_every_error_code_has_an_explicit_http_status` walks and what a consumer sees. The family
    # base keeps its row for the same reason: it is still in the vocabulary, and a code in the
    # vocabulary with no row is exactly the gap that test exists to make loud.
    "cross_site_request": 403,        # the family base; nothing raises it directly
    "undetermined_host": 403,
    "host_not_allowed": 403,
    "cross_site_fetch": 403,
    "opaque_origin": 403,
    "origin_mismatch": 403,
    "missing_request_token": 403,
    # 403 for the same reason as the cross-site family above -- the server understood the request
    # and refuses to authorize it -- but a different family and raised from a different guard
    # (`usage.SpendPolicy.check`, consulted by `DiscoveryService` before every provider call, not
    # the web's request-origin checks). Not 429: a budget does not reset with time, so "retry later"
    # is the wrong instruction to send (#427, `decision: the-http-api-facade`).
    "spend_ceiling_reached": 403,
    # 401, not 403: the request carried no acceptable credential, and the response says which one
    # to send (`WWW-Authenticate: Bearer`, RFC 6750). 403 is what the rows above answer when the
    # server has understood a request and declines it regardless of credentials; this row is the
    # one case where sending the right credential is the remedy (#425 slice 4, `api/auth.py`).
    "unauthorized": 401,
    "input_too_large": 413,
    # 409 — a conflict with the store's current state, not a malformed request
    "revision_conflict": 409,
    "session_exists": 409,
    # …and its neighbour, for the destination that holds no session at all. Not 500 beside
    # `import_move_failed`: the store is in a state that conflicts with the request, which is
    # exactly what 409 is for, and nothing failed that a retry could fix (#114).
    "import_destination_occupied": 409,
    # 5xx — the server, or what it depends on
    "context_unreadable": 500,        # we cannot read our own card directory: permissions, usually
    "no_context_cards": 500,          # this install shipped no cards; nothing the caller sent caused it
    "provider_output_invalid": 502,   # upstream would not hold the contract, after every retry
    "session_locked": 503,            # the write never started; retrying it unchanged is correct
    # The content was produced (paid for) and only the write failed — the store, not the request
    # (#208).
    "artifact_write_failed": 500,
    # Nominal, like the two family bases above: this is `create_api(bind_host=...)` refusing to
    # *start* bound beyond loopback with no `REQUIVO_API_TOKEN`, raised before any listener exists,
    # so no HTTP response ever carries it. It has a row because every code in the vocabulary has
    # one, and 500 is the honest nominal answer -- the operator's configuration, not a caller.
    "api_token_required": 500,
}

# What an *unknown* code gets. Deliberately a 5xx: with every known code mapped above, this only
# fires for a code this version has never heard of, and "we could not classify this" is not evidence
# that the caller erred. Blaming the reader for a gap in our own table is the generalised form of the
# bug #34 reports.
UNCLASSIFIED_STATUS = 500


def http_status_for(error: RequivoError) -> int:
    """The HTTP status a structured error is reported with.

    A function rather than an inline lookup so the classification can be asserted directly, for every
    code, without driving a request that reaches it — several codes (`session_locked`,
    `provider_output_invalid`) need a race or an upstream failure to reach honestly.

    `EngineError` is answered ahead of the table on purpose: provider transport is a family, not a
    code, so `provider_unavailable` deliberately has no row and the allowlist in the tests records why.
    """
    if isinstance(error, EngineError):
        return 502
    return STATUS_BY_CODE.get(error.code, UNCLASSIFIED_STATUS)
