"""`requivo session`: create, list, show, migrate, export, verify, restore, rescope and import
sessions.

The session directory is the interface between every surface and it is public at `format_version` 1
(invariant 8), so this is the largest of the deterministic modules and the one whose output shape is
hardest to change. Two rules run through all of it.

**A listing survives its own members** (invariant 15). `session list` renders every row it can and
degrades the ones it cannot, and *could not be read* is a different answer from *not analysed yet*.
`EXIT_DEGRADED` is the exit code that says so. It lives in `_shared` rather than here because it
names a shape of answer rather than a verb: `session verify` reaches the same state from the other
side when it cannot read a session's product context, and minting a code per verb would rebuild the
collapse the code exists to undo.

**A value read off disk is untrusted input** (invariant 14). Every slug, error string and filename
that comes back from the store goes through `display_token` before it reaches a printed line,
because a stored value carrying a newline would otherwise write what reads as a second,
authoritative line of Requivo's own output at column 0.

`session verify` also asks whether a session's context cards still load on this machine. That is an
environment finding rather than an integrity one, so the check and its two remedy hints are imported
from `doctor`, which owns them, instead of being restated here: the two surfaces printing different
advice for the same finding is how they drift.

Part of the deterministic surface, so no LLM and no API key. `register_sessions(sub)` is composed
into the package's single `register()` by `deterministic/__init__.py`.

Split into a package by #550 (the lean pass, #548): `lifecycle.py` (init/list/show/migrate/rescope/
delete), `archives.py` (export/import/restore) and `verify.py` (verify) -- this file is the single
seam that stays, wiring every subcommand's `argparse` parser to the verb function that now lives in
one of the three.
"""
from __future__ import annotations

from requivo.deterministic.sessions.archives import _cmd_session_export, _cmd_session_import, _cmd_session_restore
from requivo.deterministic.sessions.lifecycle import (
    _cmd_session_delete,
    _cmd_session_init,
    _cmd_session_list,
    _cmd_session_migrate,
    _cmd_session_rescope,
    _cmd_session_show,
)
from requivo.deterministic.sessions.verify import _cmd_session_verify


def register_sessions(sub) -> None:
    """Attach the `session` verb group to the main `requivo` subparser."""
    # session
    sp = sub.add_parser("session",
                        help="create, list, show, verify, restore, rescope, delete, migrate, export/import "
                             "sessions")
    ss = sp.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    si = ss.add_parser("init", help="create a session from a request (no LLM)")
    si.add_argument("request", help="the request, a path to a file containing it, or '-' for stdin")
    si.add_argument("--slug", help="explicit session slug (default: derived from the request)")
    si.add_argument("--context", "--cards", metavar="CARDS", dest="context",
                    help="comma-separated context cards to record. Alias: --cards.")
    si.add_argument("--provider", default=None, help="informational provider tag (e.g. claude-code)")
    si.add_argument("--json", action="store_true")
    si.set_defaults(func=_cmd_session_init)

    sl = ss.add_parser("list", help="list canonical sessions")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=_cmd_session_list)

    sh = ss.add_parser("show", help="show a session's metadata + artifacts")
    sh.add_argument("session", help="session slug or path")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=_cmd_session_show)

    sm = ss.add_parser("migrate", help="migrate ALL legacy out/ sessions into .requivo/sessions/")
    sm.add_argument("--json", action="store_true")
    sm.set_defaults(func=_cmd_session_migrate)

    se = ss.add_parser("export", help="export a session as a .zip archive")
    se.add_argument("session", help="session slug or path")
    se.add_argument("-o", "--output", help="destination archive path")
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=_cmd_session_export)

    sv = ss.add_parser("verify", help="check that a session's files agree with each other")
    sv.add_argument("session", help="session slug or path")
    sv.add_argument("--json", action="store_true")
    sv.set_defaults(func=_cmd_session_verify)

    srt = ss.add_parser("restore", help="copy a readable revision over model.json — the recovery "
                        "path for a torn or inconsistent session (#210)")
    srt.add_argument("session", help="session slug or path")
    srt.add_argument("--revision", type=int, default=None,
                     help="restore from this revision instead of the newest one this build can read")
    srt.set_defaults(func=_cmd_session_restore)

    sr = ss.add_parser("rescope", help="re-scope an existing session's context cards")
    sr.add_argument("session", help="session slug or path")
    sr.add_argument("--context", "--cards", metavar="CARDS", dest="context", required=True,
                    help="comma-separated context cards to switch to, or '' for every card. "
                         "Alias: --cards. Required — unlike `init`, omitting it is not a default.")
    sr.add_argument("--json", action="store_true")
    sr.set_defaults(func=_cmd_session_rescope)

    sig = ss.add_parser("import", help="import a session archive into the workspace")
    sig.add_argument("archive", help="path to a .zip produced by `session export`")
    sig.add_argument("--force", action="store_true",
                     help="replace a session of the same slug that already exists here")
    sig.add_argument("--json", action="store_true")
    sig.set_defaults(func=_cmd_session_import)

    sd = ss.add_parser("delete", help="irreversibly remove a session -- `session export` first is "
                       "the undo story; there is no trash")
    sd.add_argument("session", help="session slug or path")
    sd.add_argument("--json", action="store_true")
    sd.set_defaults(func=_cmd_session_delete)
