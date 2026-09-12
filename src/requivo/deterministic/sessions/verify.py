"""`requivo session verify` -- and the restore remedy it points readers at.

Split out of `deterministic/sessions.py` by #550 (the lean pass, #548): the one read-only
diagnostic verb, kept apart from `archives.py`'s `restore` even though `_restore_remedy_line`
names it, because this module never writes -- it only tells a reader what `session restore` would
do if they ran it.
"""
from __future__ import annotations

from typing import Optional

from requivo.core import persistence as store
from requivo.core.errors import RequivoError, SessionLockedError, SessionUnreadableError
from requivo.core.integrity import SEVERITY_NOTE, blocking, inspect_session, newest_readable_revision
from requivo.core.selectors import display_token
from requivo.deterministic._shared import EXIT_DEGRADED, print_json
from requivo.deterministic.doctor import _REPAIR_HINT, _RESTORABLE_CARD_CODES, _RESTORE_HINT, _card_health
from requivo.services.sessions import SessionService

# Problem codes `session restore` (#210) can actually repair -- model.json disagreeing with, or
# missing against, a revision history that is otherwise intact. Every other integrity code names
# something restore does not touch: a broken revision log, a corrupt session.json, a revision file
# gone -- copying a revision over model.json does nothing about any of those. `session verify`'s
# remedy line below is scoped to exactly this set on purpose: naming a fix that would not fix the
# problem is worse than naming none, the same reasoning `_RESTORABLE_CARD_CODES` in `doctor.py`
# already applies one finding-family over.
_RESTORABLE_MODEL_CODES = frozenset({"invalid_model", "model_is_not_the_last_revision", "missing_model"})


def _restore_remedy_line(slug: str, problems: list, svc: SessionService) -> Optional[str]:
    """The one line #210 was filed to add: which revision `session restore` would copy over
    model.json, if any of `problems` is a code that verb can fix (see `_RESTORABLE_MODEL_CODES`).

    Text-only, like the card-health hints beside it. `None` only when nothing here is restorable --
    a session that vanishes or locks up in the gap between `inspect_session`'s own, earlier read and
    this function's later one is not a state this may silently fold into "nothing to suggest" --
    `test_session_verify_says_it_could_not_check_whether_restore_would_help`.

    Two shapes of remedy, not one: the newest revision this build can trust might not be the *last*
    one, and restoring from a fallback does not clear `model_is_not_the_last_revision` -- recommending
    the identical command as the ordinary case, with no word of warning, would be the wrong kind of
    reassuring -- `test_session_restore_skips_a_broken_revision_when_searching_for_the_default`."""
    if not ({p.code for p in problems} & _RESTORABLE_MODEL_CODES):
        return None
    try:
        meta = svc.meta(slug)
        n = meta.current_revision
        hashes = {r.revision: r.model_hash for r in meta.revisions}
        found = newest_readable_revision(store.canonical_dir(slug), n,
                                         expected_hashes=hashes) if n > 0 else None
    except RequivoError as e:
        # The third state, not a silent None: a restorable code is present but the search itself
        # could not run (a race with a concurrent writer, most plausibly). Saying nothing here reads
        # as "there is no fix", which is a different and stronger claim than "this could not be
        # checked" -- the exact collapse this whole file's own `session.checked`/`EXIT_DEGRADED`
        # machinery exists to refuse everywhere else.
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
    """Check that a session tells the truth about itself, and that the product context it names is
    still there. Exits non-zero when either is wrong, so it can gate a script.

    The two are reported side by side and kept apart on purpose. `problems` are *internal*: the
    relationships between the session's own files, which validating each file on its own cannot see.
    `context_cards` is an *environment* finding — the cards a session was created against live
    outside its directory, so a lost one says nothing about the session and everything about this
    machine. Keeping it out of `check_session_dir` is what stops `session import` refusing a
    colleague's perfectly good archive over a card you do not have; see `_card_health`.

    It is nonetheless part of `ok`, because a session whose cards are gone is refused at its next
    reasoning turn, and a verb that answers "is this session usable" with a tick right up to that
    moment is the failure this whole change is about.

    **Three answers, three exit codes.** The rendering always distinguished them and the exit code
    distinguished two, in the verb whose whole job is to answer *is this session sound*. It takes two
    tests to pin, one per firm claim, because either alone is green while the other's arm is broken:
    `test_session_verify_exits_one_when_the_cards_were_checked_and_are_broken` for the 1, and
    `test_session_verify_exits_four_when_it_could_not_check_the_product_context` for the 4 that used
    to be collapsed into it -- the distinction this whole change is about, and the one a reference to
    the first alone leaves unguarded:

    - `problems` — checked, the session is inconsistent. A complete answer. **1**.
    - `cards["problem"]` — checked, its product context is broken. Also complete. **1**.
    - `not cards["checked"]` — the context could not be checked. Not an answer at all. **4**.

    4 rather than a code of this verb's own: it already means *the work was done and part of the
    answer was unreachable*, and an exit code describes a shape of answer, not a verb. A code per
    verb rebuilds the problem 4 was introduced to solve.

    **A firm negative outranks a partial one**, so a session that is both inconsistent *and* whose
    cards could not be read exits 1. A script gating on *is this usable* wants the definite answer,
    and there is one. Nothing is withheld at either code: `--json` carries the whole story either
    way, and `ok` keeps the meaning it always had — it is false in all three failing states.

    **A fourth thing is reported and is none of the three**, pinned by
    `test_session_verify_passes_and_still_names_the_unknown_type`: a `note` is a finding that is not
    a defect, and today the only one is an artifact type this build has no generator for. It prints,
    it rides in `--json` under `notes`, and it changes neither `ok` nor the exit code — because
    `docs/compatibility.md` lists a new artifact type among the changes that need no `format_version`
    bump, and a verb that answered "broken" there would be measuring a session written by a newer
    Requivo against a rule that version no longer follows. `problems` keeps its meaning exactly, so a
    consumer gating on it is unaffected.

    **A diagnosis is not the end of the story** (#210). Before this, the remedy for a torn model.json
    stopped at "run verify again" — the fact that `revisions/` holds every applied model, and that an
    earlier one can be copied over the broken one, lived nowhere a user reading this output would
    find it. When `problems` carries a code `session restore` can actually fix, the human render
    names the newest revision this build can still read and the exact command to run — see
    `_restore_remedy_line`. `--json` is unchanged: the codes were already enough for a script to act
    on, and this line is convenience for a human reading the terminal, the same split every other
    hint in this verb already makes.
    """
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    # The probe itself is a third source of `unchecked` (#97). `session_exists` no longer escapes as a
    # bare traceback when it cannot stat — it raises `SessionUnreadableError` — and letting that
    # propagate would exit 1, which says *I checked and it is broken* about a session nothing looked
    # at. That is the collapse #86 removed from this verb; it must not come back through a different
    # door. Nothing below this line can run either: `check_session` and `_card_health` both read the
    # directory this call could not stat.
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
            # A lock this call could not take within the deadline (#263, #265) is no measurement,
            # not a broken session -- reporting it as `problems` would be the exact accusation shape
            # invariant 17 exists to prevent, aimed at a session that is merely mid-write. It joins
            # the probe's own unreadable arm above rather than getting a fourth state of its own.
            session_probe = {"checked": False, "error": str(e)}
    problems = blocking(findings)
    notes = [f for f in findings if f.severity == SEVERITY_NOTE]
    cards = _card_health(slug) if session_probe["checked"] else {"checked": False, "problem": None,
                                                                 "error": session_probe["error"]}
    unsound = bool(problems) or cards["problem"] is not None
    unchecked = not cards["checked"] or not session_probe["checked"]
    ok = not unsound and not unchecked
    # `exit_code`, not `code`: the rendering below already binds `code` to a card-problem *code*
    # string, and the collision reached the raise as `SystemExit('unknown_context_card')`, which
    # CPython prints to stderr and turns into status 1 — the number this change is about replaced by
    # a stray line, on the branch where the shadowing happens and only there. Caught by an existing
    # test, not by this one, which is why the name rather than the number is the fix.
    exit_code = 1 if unsound else (EXIT_DEGRADED if unchecked else 0)
    if a.json:
        # `session` is additive and always present (#97). It is a sibling of `context_cards` and
        # carries the same two keys for the same reason: a consumer reading `problems: []` has to be
        # able to tell *checked, nothing wrong* from *nothing was checked*, and an empty list spells
        # both. Branch on `session.checked`, never on the emptiness of `problems` — or, since #260,
        # of `notes`, which is empty in that arm for exactly the same reason and says exactly as
        # little.
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
        # Printed under the tick rather than instead of it: the session *is* consistent, and this is
        # a fact about it worth naming (#260). Not a glyph of its own — ✅/❌/🟡 already spell the
        # three answers this verb gives, and a fourth would read as a fourth verdict.
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




