"""`requivo session verify` (#550): the one read-only diagnostic verb, and the restore remedy it
points readers at without ever writing.
"""
from __future__ import annotations

from typing import Optional

from requivo.core import persistence as store
from requivo.core.errors import RequivoError, SessionLockedError, SessionUnreadableError
from requivo.core.integrity import SEVERITY_NOTE, blocking, inspect_session, newest_readable_revision
from requivo.core.selectors import display_token
from requivo.deterministic._shared import EXIT_DEGRADED, print_json
from requivo.deterministic.remedies import _REPAIR_HINT, _RESTORABLE_CARD_CODES, _RESTORE_HINT, _card_health
from requivo.services.sessions import SessionService

# The problem codes `session restore` (#210) can actually repair; naming a fix that would not fix
# the problem is worse than naming none.
_RESTORABLE_MODEL_CODES = frozenset({"invalid_model", "model_is_not_the_last_revision", "missing_model"})


def _restore_remedy_line(slug: str, problems: list, svc: SessionService) -> Optional[str]:
    """Which revision `session restore` would copy over model.json, if a `problems` code is restorable
    (#210). `None` only when nothing is restorable; a search that could not run says so
    (`test_session_verify_says_it_could_not_check_whether_restore_would_help`), and a fallback that
    is not the last revision is warned about (`test_session_restore_skips_a_broken_revision_when_searching_for_the_default`)."""
    if not ({p.code for p in problems} & _RESTORABLE_MODEL_CODES):
        return None
    try:
        meta = svc.meta(slug)
        n = meta.current_revision
        hashes = {r.revision: r.model_hash for r in meta.revisions}
        found = newest_readable_revision(store.canonical_dir(slug), n,
                                         expected_hashes=hashes) if n > 0 else None
    except RequivoError as e:
        # The third state, not a silent None: "no fix" is a stronger claim than "could not check".
        return f"    Could not check whether `session restore` could help: {display_token(str(e))}"
    if found is None:
        return ("    No revision file in this session's history could be read or trusted -- there is "
                "nothing for `session restore` to copy from. Recovery here is manual JSON surgery, "
                "or restoring this session from a backup.")
    if found.revision == n:
        return (f"    revisions/{found.revision:04d}-model.json parses cleanly and can replace it: "
                f"`requivo session restore {display_token(slug)}` (defaults to revision "
                f"{found.revision}) -- `session verify` should read clean afterwards.")
    return (f"    revisions/{found.revision:04d}-model.json is the newest revision this build can "
            f"still trust, but it is not the last one -- revision {n}'s own content is unreadable "
            f"or tampered and cannot be recovered. `requivo session restore {display_token(slug)}` "
            f"restores to revision {found.revision} as a partial repair; `session verify` will keep "
            f"reporting the session as inconsistent afterwards, correctly.")




def _cmd_session_verify(a, client) -> None:
    """Check that a session tells the truth about itself and that its product context is still
    there; exits non-zero when either is wrong. `problems` are internal, `context_cards` an
    environment finding kept out of `check_session_dir`. Three answers, three exit codes: inconsistent
    or broken cards is 1, context that could not be checked is 4 (`EXIT_DEGRADED`), and a firm
    negative outranks a partial one (`test_session_verify_exits_one_when_the_cards_were_checked_and_are_broken`,
    `test_session_verify_exits_four_when_it_could_not_check_the_product_context`). A `note` moves
    neither `ok` nor the code (#260, `test_session_verify_passes_and_still_names_the_unknown_type`).
    A restorable problem names the remedy (#210, `_restore_remedy_line`)."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    # The probe itself is a third source of `unchecked` (#97): `SessionUnreadableError` must not exit 1.
    session_probe: dict = {"checked": True, "error": None}
    try:
        found = svc.exists(slug)
    except SessionUnreadableError as e:
        session_probe = {"checked": False, "error": str(e)}
        found = True
    else:
        if not found:
            raise svc.no_session(slug)
    findings: list = []
    if session_probe["checked"]:
        try:
            findings = inspect_session(slug)
        except (SessionLockedError, SessionUnreadableError) as e:
            # A lock this call could not take is no measurement (#263, #265, invariant 17).
            session_probe = {"checked": False, "error": str(e)}
    problems = blocking(findings)
    notes = [f for f in findings if f.severity == SEVERITY_NOTE]
    cards = _card_health(slug) if session_probe["checked"] else {"checked": False, "problem": None,
                                                                 "error": session_probe["error"]}
    unsound = bool(problems) or cards["problem"] is not None
    unchecked = not cards["checked"] or not session_probe["checked"]
    ok = not unsound and not unchecked
    # `exit_code`, not `code`: the rendering below binds `code` to a card-problem string.
    exit_code = 1 if unsound else (EXIT_DEGRADED if unchecked else 0)
    if a.json:
        # `session` is additive (#97): branch on `session.checked`, never on the emptiness of `problems` or `notes`.
        print_json({"slug": slug, "ok": ok, "session": session_probe,
                     "problems": [p.to_dict() for p in problems],
                     "notes": [n.to_dict() for n in notes], "context_cards": cards})
        if exit_code:
            raise SystemExit(exit_code)
        return
    if ok:
        print(f"✅ Session '{slug}' is internally consistent and its product context still loads.")
    if not session_probe["checked"]:
        print(f"🟡 Could not examine '{slug}': {display_token(session_probe['error'])}")
        print("   Nothing about this session was checked — this is not a report that it is sound.")
        raise SystemExit(exit_code)
    if problems:
        print(f"❌ Session '{slug}' has {len(problems)} problem(s):")
        for p in problems:
            print(f"  · [{p.code}] {p.message}")
        remedy = _restore_remedy_line(slug, problems, svc)
        if remedy is not None:
            print(remedy)
    if notes:
        # Printed under the tick, not instead of it (#260); no fourth glyph.
        print(f"  Also worth knowing about '{slug}':")
        for n in notes:
            print(f"  · [{n.code}] {n.message}")
    if cards["problem"]:
        code = cards["problem"]["code"]
        restorable = code in _RESTORABLE_CARD_CODES
        print(f"❌ Session '{slug}' " + ("names product context that no longer loads:" if restorable
                                         else "has a product-context selection that cannot be read:"))
        print(f"  · [{code}] {cards['problem']['message']}")
        print(f"    {_RESTORE_HINT if restorable else _REPAIR_HINT}")
    elif not cards["checked"]:
        print(f"🟡 Could not check '{slug}'s product context: {display_token(cards['error'])}")
    if exit_code:
        raise SystemExit(exit_code)




