"""`requivo doctor`, `schema` and `context`: the verbs that answer for the install, not a session.
A lost context card is an environment finding, not an integrity code, so `_card_health` and its
remedy hints live in `remedies.py`, shared with `session verify` (#556). No LLM, no API key.
"""

from __future__ import annotations

import os
import platform

from requivo.core import persistence as store
from requivo.core.context import available_cards
from requivo.core.errors import InvalidModelError, SessionLockedError
from requivo.core.integrity import SEVERITY_NOTE, IntegrityProblem, blocking, inspect_session
from requivo.core.selectors import display_token
from requivo.deterministic._shared import _NO_DETAIL, _resolve_cards, print_json
from requivo.deterministic.remedies import _REPAIR_HINT, _RESTORABLE_CARD_CODES, _RESTORE_HINT, _card_health
from requivo.paths import ASSETS, CONTEXT, lock_root, session_root, user_context_dir, workspace_root
from requivo.providers.anthropic import credential_diagnosis, current_model_name
from requivo.services.sessions import SessionService
from requivo.streams import describe_streams


def doctor_report() -> dict:
    """A self-diagnosis of the install; a missing SDK or key is informational, not an error."""
    from requivo import __version__

    # Assets + schema.
    schema_ok, slot_count, schema_err = True, 0, None
    try:
        from requivo.core.contracts import schema_slot_ids
        allowed, _ = schema_slot_ids()
        slot_count = len(allowed)
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        schema_ok, schema_err = False, str(e)

    # Context cards have three states: `ok`, `empty` (a broken install) and `unreadable` (could not look).
    # `test_doctor_tells_a_loaded_context_dir_from_a_lost_one_and_from_an_unreadable_one`.
    cards, cards_err = [], None
    try:
        cards = available_cards()
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        cards_err = str(e)
    cards_status = "unreadable" if cards_err else ("ok" if cards else "empty")

    # Perimeters (#608), reported the same shape as context cards; `empty` is distinguished from `ok` anyway.
    perimeters, perimeters_err = [], None
    try:
        from requivo.core.perimeters import known_perimeter_ids
        perimeters = list(known_perimeter_ids())
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        perimeters_err = str(e)
    perimeters_status = "unreadable" if perimeters_err else ("ok" if perimeters else "empty")

    # Provider (optional).
    provider_installed, provider_version = False, None
    try:
        import anthropic
        provider_installed = True
        provider_version = getattr(anthropic, "__version__", "unknown")
    except ImportError:
        pass

    # `credential_diagnosis()` (#365): `credential_problem` is the SDK's own reason for a configured
    # profile that could not be loaded, so the rendering can name the real remedy.
    api_key_present, credential_problem = credential_diagnosis()

    # The console's codec, reported after `configure_streams()` ran, including a stream it could not fix (#29).
    output = describe_streams()

    # The model and where the choice came from (#247): `REQUIVO_MODEL` first, bare `MODEL` as fallback,
    # the order `current_model_name` reads them (#268). Presence, not truthiness:
    # `test_a_model_override_that_is_set_but_empty_is_reported_as_one`.
    override = os.getenv("REQUIVO_MODEL")
    if override is None:
        override = os.getenv("MODEL")
    model = {"name": current_model_name(), "source": "default" if override is None else "env"}

    return {
        "requivo_version": __version__,
        "python_version": platform.python_version(),
        # `platform.platform()`, not `sys.platform`: the bug template asks for an OS. Additive.
        "os": platform.platform(),
        "model": model,
        "assets": {"root": str(ASSETS), "present": ASSETS.exists()},
        "output": {"ok": all(s["state"] == "safe" for s in output), "streams": output},
        "schema": {"ok": schema_ok, "slots": slot_count, "error": schema_err},
        # `context_cards` stays the plain list: a published `--json` key. The verdict is the sibling.
        "context_cards": cards,
        "context": {"ok": cards_status == "ok", "status": cards_status, "count": len(cards),
                    "error": cards_err, "roots": [str(CONTEXT), str(user_context_dir())]},
        "perimeters": {"ok": perimeters_status == "ok", "status": perimeters_status,
                       "installed": perimeters, "error": perimeters_err},
        "provider_anthropic": {
            "installed": provider_installed,
            "version": provider_version,
            # The same credential probe `new_client()` reads (#332), so a bearer token counts:
            # test_doctor_reports_a_bearer_token_as_a_credential_present.
            "api_key_present": api_key_present,
            # `None` is the ordinary case; a string is the SDK's reason for an unloadable profile (#365). Additive.
            "credential_problem": credential_problem,
        },
        # `locks` names the one path every write touches and nothing else names; path only, not probed.
        # `test_doctor_reports_where_the_write_lock_lives`.
        "workspace": {"root": str(workspace_root()), "sessions": str(session_root()),
                      "locks": str(lock_root())},
        # Sessions that no longer add up: cheap, and this is where a user asks "is anything wrong?".
        "sessions": _session_health(cards_readable=cards_err is None),
        # Candidate lock residue (#180), in three states, never a conclusion the directory cannot support.
        "locks": _lock_health(),
    }


def _session_health(*, cards_readable: bool = True) -> dict:
    """The workspace's sessions, with a third state on each question: `total` is `None`, never `0`,
    when the root could not be listed; `inconsistent` is the blocking half of `inspect_session`;
    `notes` are non-defect findings (an unknown artifact type, #260); `unresolved_cards` a selection
    that no longer loads (`cards_checked` False when nobody looked); `locked` a lock this call could
    not take, never folded into `inconsistent`; `non_sessions` and `unexaminable` from the scan.
    `test_doctor_tells_an_empty_workspace_from_an_unreadable_one`,
    `test_doctor_reports_a_locked_session_as_could_not_check_not_as_broken`,
    `test_doctor_reports_the_entry_instead_of_declaring_the_whole_root_unreadable`."""
    inconsistent: dict[str, list[str]] = {}
    noted: dict[str, list[str]] = {}
    unresolved: dict[str, dict] = {}
    locked: dict[str, str] = {}
    try:
        # One listing for all three parts, not two scans at two instants:
        # `test_the_parts_of_the_session_root_are_one_partition`.
        slugs, entries, blind = store.scan_session_root()
        non_sessions = [e.to_dict() for e in entries]
        unexaminable = [e.to_dict() for e in blind]
    except Exception as e:  # noqa: BLE001 - doctor reports, it does not fail — but it must say what it hit
        return {"total": None, "readable": False, "error": str(e),
                "inconsistent": {}, "notes": {}, "unresolved_cards": {}, "cards_checked": False,
                "non_sessions": None, "unexaminable": None, "locked": {}}
    for slug in slugs:
        try:
            findings = inspect_session(slug)
        except SessionLockedError as e:
            # No measurement, not a defect (#263, #265): a lock timeout gets its own bucket, and cards
            # are not checked either.
            locked[slug] = str(e)
            continue
        except Exception as e:  # noqa: BLE001
            findings = [IntegrityProblem("unreadable", str(e))]
        codes = [p.code for p in blocking(findings)]
        note_codes = [f.code for f in findings if f.severity == SEVERITY_NOTE]
        if cards_readable:
            health = _card_health(slug)
            if not health["checked"] and "unreadable" not in codes:
                codes.append("unreadable")
            if health["problem"]:
                unresolved[slug] = health["problem"]
        if codes:
            inconsistent[slug] = codes
        if note_codes:
            noted[slug] = note_codes
    return {"total": len(slugs), "readable": True, "error": None,
            "inconsistent": inconsistent, "notes": noted, "unresolved_cards": unresolved,
            "cards_checked": cards_readable, "non_sessions": non_sessions,
            "unexaminable": unexaminable, "locked": locked}


def _lock_health() -> dict:
    """Candidate residue under `lock_root()` (#180), never reported as *orphan*: this scan and the
    session scan run a moment apart. `total` is `None` when the root could not be listed;
    `unmatched` is `None` when the session list could not be read, and a session `list_slugs()`
    cannot confirm counts as existing (#80,
    `test_a_lock_for_a_session_that_exists_but_is_unexaminable_is_not_claimed_as_unmatched`);
    `unexpected` names what `scan_lock_root` does not recognise (#209, #391)."""
    try:
        lock_slugs, unexpected, unexaminable = store.scan_lock_root()
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        return {"readable": False, "error": str(e), "total": None, "sessions_checked": False,
                "unmatched": None, "unexpected": None, "unexaminable": None}
    try:
        # `repo.list_slugs()`: the backing-neutral primitive for *which slugs exist* (invariant 14).
        repo = SessionService().repo
        known = set(repo.list_slugs())
        # An unexaminable name is not confirmed absent, so it is excluded from `unmatched` (#80).
        known |= {e.name for e in repo.list_unexaminable()}
    except Exception:  # noqa: BLE001 - "could not check" must not read as "none matched"
        known = None
    unmatched = None if known is None else sorted(s for s in lock_slugs if s not in known)
    return {"readable": True, "error": None, "total": len(lock_slugs),
            "sessions_checked": known is not None, "unmatched": unmatched,
            "unexpected": sorted(unexpected), "unexaminable": [e.to_dict() for e in unexaminable]}


def _cmd_schema(a, client) -> None:
    """Print the slot schema (and optionally the framework spec) for a reasoning caller;
    `--perimeter` (#608) selects the installed perimeter, software by default."""
    from requivo.core.perimeters import get_perimeter
    perimeter = get_perimeter(getattr(a, "perimeter", None) or "software")
    print(perimeter.schema_path.read_text(encoding="utf-8"))
    if a.framework:
        print(f"\n\n<!-- {perimeter.id} perimeter's elicitation.md (human spec) -->\n")
        print(perimeter.elicitation_path.read_text(encoding="utf-8"))


def _cmd_context(a, client) -> None:
    """List or print the context cards; `--session` prints the cards that session was created with,
    so a caller cannot quietly widen the selection the model was built against."""
    from requivo.core.context import load_context
    if a.list:
        for c in available_cards():
            print(c)
        return
    if a.session:
        if a.cards:
            # Both spellings named, because #85 made them aliases.
            raise InvalidModelError(
                "--session and --cards/--context are alternatives; pass only one")
        svc = SessionService()
        cards = svc.cards(svc.resolve_slug(a.session))   # None == the session uses every card
    else:
        cards = _resolve_cards(a.cards) if a.cards else None
    print(load_context(cards))


def _cmd_doctor(a, client) -> None:
    r = doctor_report()
    if a.json:
        print_json(r)
        return
    ok = "✅"
    warn = "🟡"
    print("Requivo doctor")
    print(f"  {ok} requivo         {r['requivo_version']}")
    print(f"  {ok} python          {r['python_version']}")
    # The rows the bug template asks for (#247), at the top, so a paste of the first lines is a bug report.
    print(f"  {ok} os              {r['os']}")
    if not r["model"]["name"]:
        # An exported-but-empty MODEL is a finding, not a blank under a tick.
        print("  ❌ model           MODEL is set but empty — a provider call would send no model id")
    else:
        origin = "MODEL env override" if r["model"]["source"] == "env" else "default"
        print(f"  {ok} model           {r['model']['name']}  ({origin})")
    # The console's codec, only when there is something to say.
    for stream in r["output"]["streams"]:
        if stream["state"] == "will_crash":
            print(f"  ❌ {stream['stream']:<15} {stream['detail']}")
            print("     └─ Requivo could not configure this stream; set PYTHONIOENCODING=utf-8.")
        elif stream["state"] == "lossy":
            print(f"  {warn} {stream['stream']:<15} {stream['detail']}")
            print("     └─ this handler came from your environment, not from Requivo. Prefer "
                  "errors=backslashreplace.")
        elif stream["state"] == "unknown":
            print(f"  {warn} {stream['stream']:<15} {stream['detail']}")
        elif (stream["encoding"] or "").lower() not in ("utf-8", "utf8"):
            print(f"  {warn} {stream['stream']:<15} {stream['encoding']} — characters it cannot "
                  f"encode are escaped, not dropped, and never crash")
    print(f"  {ok if r['assets']['present'] else '❌'} assets          {r['assets']['root']}")
    s = r["schema"]
    print(f"  {ok if s['ok'] else '❌'} schema          {s['slots']} slots"
          + (f"  (error: {display_token(s['error'])})" if not s["ok"] else ""))
    c = r["context"]
    if c["status"] == "unreadable":
        print(f"  ❌ context cards   unreadable — {display_token(c['error'])}")
    elif c["status"] == "empty":
        print("  ❌ context cards   0 available — none found under "
              f"{' or '.join(c['roots'])}")
        print("     └─ impact estimation has no product context to reason from; this install is "
              "incomplete.")
    else:
        print(f"  {ok} context cards   {c['count']} available")
    pr = r["perimeters"]
    if pr["status"] == "unreadable":
        print(f"  ❌ perimeters      unreadable — {display_token(pr['error'])}")
    elif pr["status"] == "empty":
        print("  ❌ perimeters      0 installed — this install is incomplete")
    else:
        print(f"  {ok} perimeters      {', '.join(pr['installed'])}")
    p = r["provider_anthropic"]
    prov = f"installed (v{p['version']})" if p["installed"] else "not installed"
    # A `credential_problem` means a profile IS configured and unloadable (#365): checked first, so
    # "no API key" does not shadow it.
    if p["credential_problem"]:
        key = "credential configured but could not be loaded"
    elif p["api_key_present"]:
        key = "API key set"
    else:
        key = "no API key"
    print(f"  {ok if p['installed'] else warn} anthropic       {prov} · {key}")
    if p["credential_problem"]:
        print(f"     └─ {display_token(p['credential_problem'])}")
    if not p["installed"]:
        print("     └─ optional: `pip install 'requivo[anthropic]'` for API-powered discovery.")
        print("        Not needed for Claude Code mode.")
    print(f"  {ok} workspace       {r['workspace']['root']}")
    print(f"     sessions        {r['workspace']['sessions']}")
    print(f"     locks           {r['workspace']['locks']}")
    _print_locks(r["locks"])
    h = r["sessions"]
    if not h["readable"]:
        # Not "0 sessions": we could not look.
        print(f"  ❌ sessions        unreadable — {display_token(h['error'])}")
        print(f"     └─ {r['workspace']['sessions']} could not be listed. This is not the same "
              "thing as having no sessions.")
        return
    bad, lost, noted = h["inconsistent"], h["unresolved_cards"], h["notes"]
    # Since #33 a session with no card selection is not exempt: `only=None` can fail like any selection.
    unchecked = not h["cards_checked"] and bool(h["total"])
    blind = h["unexaminable"] or []
    locked = h.get("locked") or {}
    # `tallies`, not `notes`: `notes` is a key of this report (#260).
    tallies = ([f"{len(bad)} inconsistent"] if bad else []) \
        + ([f"{len(lost)} with product context that no longer loads"] if lost else []) \
        + ([f"{len(noted)} with a note"] if noted else []) \
        + (["product context not checked"] if unchecked else []) \
        + ([f"{len(blind)} entr{'y' if len(blind) == 1 else 'ies'} that could not be examined"]
           if blind else []) \
        + ([f"{len(locked)} locked (could not check)"] if locked else [])
    # Three glyphs for three states: `bad`/`lost` earn ❌; a could-not-look shares the middle glyph
    # (`test_an_unexaminable_entry_alone_earns_the_warning_glyph_not_the_clean_tick`); a note moves
    # no glyph (`test_a_note_does_not_move_the_sessions_glyph`).
    glyph = "❌" if (bad or lost) else (warn if (unchecked or blind or locked) else ok)
    print(f"  {glyph} sessions        {h['total']} in this workspace"
          + (f" · {' · '.join(tallies)}" if tallies else ""))
    # `display_token` on every slug: a raw directory entry can carry a newline (#40).
    # `problem['message']` is left bare: its card names went through `normalize_tokens`.
    for slug, codes in bad.items():
        safe = display_token(slug)
        print(f"     └─ {safe}: {', '.join(codes)} — run `requivo session verify {safe}`")
    for slug, problem in lost.items():
        print(f"     └─ {display_token(slug)}: {problem['message']}")
    # A note names something the session is entitled to have (#260); the remedy, if any, is an upgrade.
    for slug, note_codes in noted.items():
        safe = display_token(slug)
        print(f"     └─ {safe}: {', '.join(note_codes)} — not a defect; "
              f"`requivo session verify {safe}` says what it is")
    # One hint per remedy actually present, rather than one hint for whichever remedy came first.
    codes = {p["code"] for p in lost.values()}
    if codes & _RESTORABLE_CARD_CODES:
        print(f"        {_RESTORE_HINT}")
    if codes - _RESTORABLE_CARD_CODES:
        print(f"        {_REPAIR_HINT}")
    if unchecked:
        print("     └─ the card directory could not be read (see above), so nothing is known about "
              "whether these sessions' product context still loads.")
    for slug, message in locked.items():
        print(f"     └─ {display_token(slug)}: could not check — {display_token(message)}")
    if locked:
        print("     └─ a lock this call could not take within its deadline. Writes normally hold "
              "it for milliseconds; retry, or investigate a stuck holder if it persists.")
    _print_unexaminable(blind, h["total"])
    _print_non_sessions(h["non_sessions"])


def _print_locks(entries: dict) -> None:
    """The `locks` check, in the three-state discipline of `sessions`, and never printing "orphan".
    `test_the_lock_root_being_unlistable_is_not_reported_as_no_residue`,
    `test_a_lock_whose_session_was_deleted_by_hand_is_named_but_not_concluded`."""
    if not entries["readable"]:
        print(f"  ❌ locks           unreadable — {display_token(entries['error'])}")
        print("     └─ this could not be listed. This is not the same thing as having no residue.")
        return
    total = entries["total"]
    unmatched = entries["unmatched"] or []
    unexpected = entries["unexpected"] or []
    unexaminable = entries["unexaminable"] or []
    unchecked = not entries["sessions_checked"] and bool(total)
    notes = ([f"{len(unmatched)} with no matching session"] if unmatched else []) \
        + (["not checked against current sessions"] if unchecked else []) \
        + ([f"{len(unexpected)} unexpected entr{'y' if len(unexpected) == 1 else 'ies'}"]
           if unexpected else []) \
        + ([f"{len(unexaminable)} that could not be examined"] if unexaminable else [])
    glyph = "🟡" if (unmatched or unchecked or unexpected or unexaminable) else "✅"
    print(f"  {glyph} locks           {total} lock file{'s' if total != 1 else ''}"
          + (f" · {' · '.join(notes)}" if notes else ""))
    for slug in unmatched:
        print(f"     └─ {display_token(slug)} — no session currently named that")
    if unmatched:
        print("     No session claims these slugs right now. A session removed by hand ('rm -rf', "
              "bypassing `session delete`) or by an older Requivo with no delete verb is the "
              "ordinary way that happens, and a session created or removed between this scan and "
              "the one above reads the same way for a moment without being residue. Requivo has "
              "not opened or removed any of these; a lock file costs nothing to leave and nothing "
              "to delete once nothing is running.")
    if unchecked:
        print("     └─ the current session list could not be read (see above), so nothing is known "
              "about which of these locks still match a session.")
    for name in unexpected:
        print(f"     └─ {display_token(name)} — not a lock file Requivo recognises")
    if unexpected:
        print("     Requivo does not read these; a name here did not come from `session_lock`.")
    for entry in unexaminable:
        print(f"     └─ {display_token(entry['name'])} — could not be examined: "
              f"{display_token(entry['error'] or _NO_DETAIL)}")


def _print_unexaminable(entries: list[dict], total: int | None) -> None:
    """Names under the session root that could not be examined, under the sessions check (they may
    be sessions), with the name and error through `display_token`.
    `test_an_unexaminable_name_carrying_a_control_character_cannot_forge_a_line`."""
    if not entries:
        # `[]` is a clean workspace; the unreadable-root arm already returned above.
        return
    n = len(entries)
    # Detail lines under the sessions check, not a second check row.
    for entry in entries:
        print(f"     └─ {display_token(entry['name'])} — could not be examined: "
              f"{display_token(entry['error'] or _NO_DETAIL)}")
    thing = "this is a session" if n == 1 else "these are sessions"
    print(f"     Requivo cannot tell whether {thing}, so the count above ({total}) is what it could "
          "confirm, not what is there. Nothing has been read, moved or changed; Requivo does not "
          "alter permissions in your workspace.")


def _non_session_detail(entry: dict) -> str:
    """One entry of `sessions.non_sessions`, as an observation and never a conclusion (invariant 14).
    Every value here comes off disk and goes through `display_token`:
    `test_the_error_text_on_a_non_session_line_cannot_forge_a_line_either`."""
    kind, error = entry["kind"], entry["error"]
    if kind == "unknown":
        return f"could not be examined — {display_token(error)}"
    if kind == "file":
        return "a file, not a directory"
    if kind == "symlink":
        # Not followed: a symlink's target contents belong to another workspace.
        return "a symbolic link, not followed; nothing here is read from its target"
    if kind == "other":
        return "neither a file nor a directory"
    if error:
        # Not "an empty directory": we could not look inside.
        return f"a directory whose contents could not be listed — {display_token(error)}"
    total, shown = entry["entry_count"], entry["entries"] or []
    if not total:
        return "an empty directory"
    names = ", ".join(display_token(n) for n in shown)
    more = f", … ({total} in total)" if total > len(shown) else ""
    return f"a directory holding {total} entr{'y' if total == 1 else 'ies'}: {names}{more}"


def _print_non_sessions(entries: list[dict] | None) -> None:
    """Things under the session root that are not sessions, with what they cost (#67): the rename
    that claims a slug loses to anything occupying the name, and `session import` refuses it (#114).
    Its own row, so the sessions count stays true. `test_the_name_taken_hint_names_what_import_does_about_it`."""
    if not entries:
        # `None` is the unreadable-root arm, already reported; `[]` earns no row.
        return
    n = len(entries)
    print(f"  🟡 other entries   {n} entr{'y' if n == 1 else 'ies'} under this directory that "
          "Requivo does not read")
    for entry in entries:
        taken = "  [name taken]" if entry["slug_shaped"] else ""
        print(f"     └─ {display_token(entry['name'])} — {_non_session_detail(entry)}{taken}")
    # Marked per row and explained once.
    if any(e["slug_shaped"] for e in entries):
        print("     [name taken]: a new session asked for that name will not get it. The rename "
              "that claims a slug loses to anything already occupying it, so the session is "
              "created under that name plus a hash, and `session import` refuses that name outright "
              "(import_destination_occupied).")
    print("     Requivo has not read, moved or deleted any of these, and does not say what they "
          "are: an interrupted copy and a directory an older version left behind look the same "
          "from here. Check before removing anything.")


def register_doctor(sub) -> None:
    """Attach `doctor`, `schema` and `context` to the main `requivo` subparser."""
    # doctor
    dr = sub.add_parser("doctor", help="diagnose the install (no API key needed)")
    dr.add_argument("--json", action="store_true", help="emit the report as JSON")
    dr.set_defaults(func=_cmd_doctor)

    # schema / context — read-only knowledge for a reasoning caller (Claude Code)
    sc = sub.add_parser("schema", help="print the slot schema (the model vocabulary + driver rule)")
    sc.add_argument("--framework", action="store_true", help="also print the human framework spec")
    sc.add_argument("--perimeter", default="software",
                    help="which installed perimeter's schema to print (default: software)")
    sc.set_defaults(func=_cmd_schema)

    cx = sub.add_parser("context", help="list or print the product context cards")
    cx.add_argument("--list", action="store_true", help="list available card stems instead of content")
    # `--context` is the primary spelling and `--cards` a permanent alias (#85); the dest stays `cards`.
    cx.add_argument("--context", "--cards", metavar="CARDS", dest="cards",
                    help="comma-separated subset to print (default: all). Alias: --cards.")
    cx.add_argument("--session", metavar="SESSION",
                    help="print exactly the cards this session was created with")
    cx.set_defaults(func=_cmd_context)
