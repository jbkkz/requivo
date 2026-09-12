"""`requivo doctor`, `schema` and `context`: the verbs that answer for the install, not for a session.

Three verbs, one subject. `doctor` reports whether this install can work at all, `schema` prints the
slot vocabulary and the driver rule a reasoning caller needs, and `context` prints the product
context cards that impact estimation is read against. None of them needs a session to do its own
job, and all three read the same bundled assets, which is why a change to one is usually a change to
its neighbours.

Two things here are deliberately *not* integrity checks, and the distinction is load-bearing.
`core/integrity.py` answers whether a session directory tells the truth about itself, and its
evidence is the directory and only the directory. A context card lives in the installed package or
in `user_context_dir()`, so a lost card is an environment finding rather than a broken session. That
is why `_card_health` and the two remedy hints live here and are imported from this module by
`session verify`, which asks the same environment question from the other side. They are stated once
so that the two surfaces cannot print different advice for the same finding.

Part of the deterministic surface, so no LLM and no API key. `register_doctor(sub)` is composed into
the package's single `register()` by `deterministic/__init__.py`.
"""

from __future__ import annotations

import os
import platform

from requivo.core import persistence as store
from requivo.core.context import available_cards, check_selection
from requivo.core.errors import InvalidModelError, SessionLockedError
from requivo.core.integrity import SEVERITY_NOTE, IntegrityProblem, blocking, inspect_session
from requivo.core.selectors import display_token
from requivo.deterministic._shared import _NO_DETAIL, _resolve_cards, print_json
from requivo.paths import ASSETS, CONTEXT, lock_root, session_root, user_context_dir, workspace_root
from requivo.providers.anthropic import credential_diagnosis, current_model_name
from requivo.services.sessions import SessionService
from requivo.streams import describe_streams


def doctor_report() -> dict:
    """A self-diagnosis of the install: Python, Requivo, assets, schema, provider availability, and
    the workspace. Absence of the Anthropic SDK / API key is reported as informational, NOT an error —
    Claude Code mode needs neither."""
    from requivo import __version__

    # Assets + schema.
    schema_ok, slot_count, schema_err = True, 0, None
    try:
        from requivo.core.contracts import schema_slot_ids
        allowed, _ = schema_slot_ids()
        slot_count = len(allowed)
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        schema_ok, schema_err = False, str(e)

    # Context cards get their own check, with their own three states: `ok` (cards loaded), `empty`
    # (we looked and there are none — a broken install, since impact estimation runs on these
    # cards), `unreadable` (we could not look, which must not render like the clean one). Without
    # it, a failure here was written into a *different* check's field and every wheel missing
    # `assets/context/` showed three green ticks. Pinned by
    # `test_doctor_tells_a_loaded_context_dir_from_a_lost_one_and_from_an_unreadable_one`.
    cards, cards_err = [], None
    try:
        cards = available_cards()
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        cards_err = str(e)
    cards_status = "unreadable" if cards_err else ("ok" if cards else "empty")

    # Provider (optional).
    provider_installed, provider_version = False, None
    try:
        import anthropic
        provider_installed = True
        provider_version = getattr(anthropic, "__version__", "unknown")
    except ImportError:
        pass

    # #365: `credential_diagnosis()` is `credential_present()`'s own answer for a reader that wants
    # more than the bool -- `credential_problem` is `None` for the ordinary "no credential visible"
    # case (and for "SDK not installed", reported separately above) and the SDK's own quoted reason
    # for a profile that is configured and could not be loaded, so the human rendering below can name
    # the actual remedy instead of "no API key" for a fault no environment variable fixes.
    api_key_present, credential_problem = credential_diagnosis()

    # The console's own codec, which `doctor` is the right verb to answer about: it is the thing
    # that decides whether any *other* line of this report can be printed at all (#29). `cli.app()`
    # has already run `configure_streams()` by the time this is reached, so what is reported is the
    # state after that — including the case where a stream refused to be configured, which is the
    # one state in which a glyph can still kill the process mid-report.
    output = describe_streams()

    # The model this install will reason with, and where the choice came from (#247). Two states
    # rather than one string: a reporter with MODEL set in an environment they have forgotten about
    # is exactly the case this row exists for, and a row printing only the resolved name reads
    # identically in both. `current_model_name` is the provider own answer rather than a second copy
    # of the same lookup, which would be right until the day the default moved. Importing it is a
    # read that orchestrates nothing, and the argument for that is written into the boundary guard
    # surface-provider allowlist, which is where such a claim is kept honest.
    #
    # **The day the default moved was #268**, one release after this comment predicted it: the
    # provider started reading `REQUIVO_MODEL` first and bare `MODEL` only as a fallback, and this
    # line still asked only about `MODEL`. A reporter who had already moved to `REQUIVO_MODEL` -- the
    # name every doc now teaches -- was told their override was the default, which is the same drift
    # this comment names, arriving on schedule. Reading both names, in the same presence-based order
    # `current_model_name` reads them, is the fix rather than a third copy of the precedence.
    #
    # Presence (`is not None`), not truthiness -- already established for bare MODEL by
    # `test_a_model_override_that_is_set_but_empty_is_reported_as_one`: an exported-but-empty
    # variable is an override in effect, and `source` must say "env" for it or it names neither the
    # default it didn't use nor the override that was really in force. `REQUIVO_MODEL=""` gets the
    # same answer.
    override = os.getenv("REQUIVO_MODEL")
    if override is None:
        override = os.getenv("MODEL")
    model = {"name": current_model_name(), "source": "default" if override is None else "env"}

    return {
        "requivo_version": __version__,
        "python_version": platform.python_version(),
        # `platform.platform()` rather than `sys.platform`: the bug template asks for an OS, and
        # "darwin" is not one -- the release and the architecture are what separate a Windows path
        # bug from a macOS one. Additive, so a consumer reading any existing key is unaffected.
        "os": platform.platform(),
        "model": model,
        "assets": {"root": str(ASSETS), "present": ASSETS.exists()},
        "output": {"ok": all(s["state"] == "safe" for s in output), "streams": output},
        "schema": {"ok": schema_ok, "slots": slot_count, "error": schema_err},
        # `context_cards` stays the plain list it has always been — it is a published `--json` key
        # and a consumer reading `len(...)` off it must keep working. The verdict is the new sibling.
        "context_cards": cards,
        "context": {"ok": cards_status == "ok", "status": cards_status, "count": len(cards),
                    "error": cards_err, "roots": [str(CONTEXT), str(user_context_dir())]},
        "provider_anthropic": {
            "installed": provider_installed,
            "version": provider_version,
            # The same credential probe `new_client()` and the web surface read (#332) -- this used
            # to be its own `os.getenv("ANTHROPIC_API_KEY")`, current when it was written and stale
            # the day #201 widened the runner to accept `ANTHROPIC_AUTH_TOKEN` too, so a working
            # bearer-token install reported no key here. Pinned by
            # test_doctor_reports_a_bearer_token_as_a_credential_present.
            "api_key_present": api_key_present,
            # #365: `credential_present()` alone flattens "no credential" and "a credential that is
            # configured and unloadable" onto the same False, which is correct for a caller that only
            # wants a yes/no and wrong for the verb whose whole job is naming the remedy -- it told a
            # user with an unloadable profile to set a variable that was never the fault. `None` here
            # is the ordinary case (nothing to add to the boolean above); a string is quoted verbatim
            # from the SDK by `_resolve_client()` and named in the human rendering below instead of
            # "no API key". Additive: a consumer reading `api_key_present` alone is unaffected.
            "credential_problem": credential_problem,
        },
        # `locks` is here because a convention this verb does not report is a convention this verb
        # answers about the wrong shape. `.requivo/locks/` is the one path in the workspace every
        # write touches and nothing else names: a permission fault there surfaces as `could not
        # open the write lock for session '<slug>'` on every verb at once, with nothing telling the
        # user which directory to look at. Additive (invariant 8), and path only — whether it is
        # writable is not probed, because probing means creating it and this verb reports rather
        # than makes. Pinned by `test_doctor_reports_where_the_write_lock_lives`.
        "workspace": {"root": str(workspace_root()), "sessions": str(session_root()),
                      "locks": str(lock_root())},
        # Sessions that no longer add up. Cheap (a session is a handful of small files) and this is
        # where a user asks "is anything wrong?" — a broken history is exactly that, and it otherwise
        # only surfaces later, as a refused artifact save with no obvious cause.
        "sessions": _session_health(cards_readable=cards_err is None),
        # Candidate residue under the write-lock root (#180). #113 moved the lock outside the
        # session it guards, and `session delete` (#238) removes it as the last step of a normal
        # delete — but a session removed by hand (`rm -rf`, bypassing that verb) or by an older
        # Requivo with no delete verb at all still leaves its lock file behind, and that is the
        # ordinary way one gets here now. Reported the way #67 reports a non-session entry: what is
        # there, in three states, never a conclusion the directory alone cannot support.
        "locks": _lock_health(),
    }


# Which card findings are repaired by *restoring a file*, and which by *fixing the stored selection*.
# Two different remedies, and printing the first under the second is the quiet-wrong-answer form of
# the bug #40 is about: the verb names a real problem and then tells you to do something that cannot
# fix it. Stated once and read by both surfaces, because `doctor` and `session verify` printing
# different advice for the same finding is how they drift.
#
# `context_unreadable` is deliberately NOT a member, for the same reason `_SELECTION_REFUSALS` in
# `core/context.py` deliberately excludes it: `check_selection` lets it propagate rather than
# returning it, so `_card_health` reports it as `{"checked": False, "problem": None}` and it can
# never arrive here as a `problem["code"]` at all. Listing it would be a branch that cannot run,
# which reads to the next person as coverage this does not have. The pair is pinned by
# `test_the_two_card_code_tables_agree`, so adding it to the refusals tuple later fails loudly here
# instead of silently routing a permissions fault to the wrong remedy.
_RESTORABLE_CARD_CODES = frozenset({"unknown_context_card", "no_context_cards"})

_RESTORE_HINT = ("Put the card back, or point REQUIVO_CONTEXT_DIR at where it now lives — until "
                 "then these sessions refuse their next reasoning turn.")
_REPAIR_HINT = ("Repair the `context_cards` list in the session's session.json — the selection "
                "itself is malformed, so no card you install will resolve it.")


def _card_health(slug: str) -> dict:
    """Does this session's persisted context-card selection still load *here*? Three states, because
    a checker that could not look must not answer like one that looked and found nothing:

    - `{"checked": True,  "problem": None}`  — it loads;
    - `{"checked": True,  "problem": {…}}`   — it does not, and the envelope names the cards;
    - `{"checked": False, "error": "…"}`     — neither the session's metadata nor the card directory
      could be read, so this session's context is simply unknown.

    **Why this lives here and not in `core/integrity.py`.** That module answers one question — does
    a session directory tell the truth *about itself* — and a context card is not in the directory;
    it is in the installed package or in `user_context_dir()`. Reporting a lost card as an integrity
    problem would make the same directory coherent on one machine and broken on another, which is
    not a property an integrity check can have. It would also break `session import`, which refuses
    an archive on exactly those problems: a colleague's perfectly good session would become
    unimportable because you happen not to have one of their cards. So it is an *environment*
    finding, reported by the two verbs that ask about the environment — `doctor` and
    `session verify` — over `core.context.check_selection`, which is the guard `load_context`
    itself applies rather than a second implementation of it.
    """
    try:
        # `SessionService.meta`, not `repo.context_cards`: the two differ on the case that matters
        # here. `context_cards` answers None for a session it cannot find, and None means *all
        # cards* — so an unreadable session would be reported as healthy. `meta` raises, the
        # `except` below turns that into `checked: False`, and "could not look" stays distinct from
        # "looked and found nothing" (#80, #86).
        problem = check_selection(SessionService().meta(slug).context_cards)
    except Exception as e:  # noqa: BLE001 - a health check reports that it could not look; it never raises
        return {"checked": False, "problem": None, "error": str(e)}
    return {"checked": True, "problem": problem.to_dict() if problem else None, "error": None}


def _session_health(*, cards_readable: bool = True) -> dict:
    """The workspace's sessions, with a third state on each question it asks.

    - `readable` / `total` / `error` — could the session root be listed at all? `total` is `None`,
      never `0`, when we could not look, because `0` is a claim about the workspace we do not have.
      Pinned by `test_doctor_tells_an_empty_workspace_from_an_unreadable_one`.
    - `inconsistent` — {slug: [integrity codes]}, the blocking half of `inspect_session` (which is
      what `check_session` returns, so this key means exactly what it meant).
    - `notes` — {slug: [integrity codes]} for findings that are *not* defects. Today the only member
      is an artifact type this build has no generator for, which `docs/compatibility.md` says may
      appear without a `format_version` bump. Kept out of `inconsistent` because that key drives the
      ❌ glyph and a session written by a newer Requivo is not broken. Pinned by
      `test_doctor_names_the_unknown_type_without_calling_the_session_inconsistent`.
    - `unresolved_cards` — {slug: error envelope} for a session whose persisted card selection no
      longer loads (see `_card_health` for why that is not an integrity code). `cards_checked` is
      false when the card layer itself was unreadable — then nobody looked. Pinned by
      `test_doctor_and_verify_flag_a_session_whose_context_card_is_gone`.
    - `locked` — {slug: error message} for a session `inspect_session` could not even take the lock
      on within the deadline. Deliberately **not** folded into `inconsistent`: a lock timeout says
      nothing about whether the session is sound, the identical accusation shape invariant 17 exists
      to prevent. Cards are not checked for a locked slug either, for the same reason `session
      verify` skips them when its own probe could not run. Pinned by
      `test_doctor_reports_a_locked_session_as_could_not_check_not_as_broken`.
    - `non_sessions` — what is under the session root and is *not* a session, from
      `scan_session_root`'s second part. `None`, never `[]`, in the arm where the root could not be
      listed: an empty list there would read as *we looked and there is nothing else*. Pinned by
      `test_doctor_names_what_is_under_the_session_root_and_is_not_a_session`.
    - `unexaminable` — names under the root that could not be examined at all, so nothing above knows
      whether they are sessions. Kept out of `non_sessions` because that key states a fact — *this is
      not a session* — and here nobody established one; kept out of `total` for the same reason, so
      the count stays what could be confirmed. Pinned by
      `test_doctor_reports_the_entry_instead_of_declaring_the_whole_root_unreadable`.
    """
    inconsistent: dict[str, list[str]] = {}
    noted: dict[str, list[str]] = {}
    unresolved: dict[str, dict] = {}
    locked: dict[str, str] = {}
    try:
        # One listing for all three parts, not two calls at two instants — a `session.json` landing
        # between them would put a name in *no* answer at all, the invisible state this key exists
        # to end. This one call stays direct rather than going through the repository, whose
        # `list_slugs`/`list_unexaminable` are deliberately two scans -- the very thing this key
        # exists to avoid. Neither `_describe_non_session` nor the partition's third bucket raises,
        # so what this `except` catches is genuinely the whole root. Pinned by
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
            # No measurement, not a defect (#263, #265): reviewed and corrected before this landed
            # (a first draft folded this into `inconsistent` under its own code, which still drove
            # the ❌ glyph and directly contradicted this comment). A lock this call could not take
            # within the deadline says nothing about whether the session is sound, so it gets a
            # bucket of its own rather than the one that means "broken" -- see `locked` above.
            # `continue`, not a fall-through: cards are not checked either, on the same reasoning
            # `_cmd_session_verify` already applies when its own probe could not run.
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
    """Candidate residue under `lock_root()` — the sibling of `_session_health`, one root over.

    `session_lock` only ever creates `<slug>.lock` for a slug that had a session directory *at that
    instant* (`session_lock` refuses before opening the file if there is none). So a lock file whose
    slug currently names no session is candidate residue from a deleted one (#180) — `session delete`
    (#238) unlinks its own lock file cleanly, so the ordinary way a session goes here now is a
    directory removed by hand (`rm -rf`, bypassing that verb) or by an older Requivo with no delete
    verb at all. It is never reported as *orphan*: this scan and the session scan it is checked
    against run a moment apart, and a session created or removed in that gap reads exactly the same
    way for a tick without being residue at all — the same caution `scan_session_root` states for its
    own second bucket, applied to the root #113 created.

    Three questions, each with its own third state:

    - `readable` / `total` / `error` — could `lock_root()` be listed at all? `False` with `total`
      `None`, never `0`, on the same reasoning `_session_health` gives for its own root: `0` is a
      claim about the workspace, and a directory nobody could open into does not support one.
    - `sessions_checked` / `unmatched` — a lock only counts as candidate residue relative to the
      *current* session list, which is a second read of a second root. When that read itself fails,
      `unmatched` is `None` rather than `[]` — an empty list here would claim every lock was checked
      and none were residue, which is exactly the conflation `_session_health`'s own `cards_checked`
      flag exists to keep apart from a genuinely clean check. "Currently exists" also has to include
      a session `list_slugs()` cannot confirm — a name in `list_unexaminable()` (#80) is not
      confirmed absent, and folding it into `unmatched` told the false story `sessions.unexaminable`
      exists to refuse about that exact name (found by review; see
      `test_a_lock_for_a_session_that_exists_but_is_unexaminable_is_not_claimed_as_unmatched`).
    - `unexpected` — names under this root that `scan_lock_root` does not recognise as either a
      `<slug>.lock` file `session_lock` could have produced or a `<slug>.discovering` guard file
      `_discovery_guard_path` could have produced (#209, #391). Reported, not absorbed into `total`
      -- a recognised `.discovering` file is not a lock and does not belong in that count either.
    - `unexaminable` — entries whose examination itself raised (#80's third bucket, one root over).
    """
    try:
        lock_slugs, unexpected, unexaminable = store.scan_lock_root()
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        return {"readable": False, "error": str(e), "total": None, "sessions_checked": False,
                "unmatched": None, "unexpected": None, "unexaminable": None}
    try:
        # `SessionService().repo.list_slugs()`, not `store.list_session_slugs()`: this is *which
        # slugs currently exist*, and `SessionRepository.list_slugs()` is the backing-neutral
        # primitive for exactly that question (invariant 14's own reasoning for `svc.repo.lock` in
        # `deterministic/sessions/`). `scan_lock_root()` above has no such equivalent to route
        # through -- a lock-root scan is a fact about the file backing, the same way `canonical_dir`
        # is, and it is allowlisted in `tests/test_boundaries.py` on those terms.
        repo = SessionService().repo
        known = set(repo.list_slugs())
        # `list_slugs()` answers *confirmed* sessions alone -- a directory whose `session.json`
        # probe itself raised (EACCES, most often) is `list_unexaminable()`'s, not this one (#80).
        # Folding that name into `unmatched` would tell the exact false story `sessions.unexaminable`
        # exists to refuse about the identical name: "no session currently named that" about a slug
        # the workspace could not confirm is empty. Excluded here rather than surfaced as its own
        # state -- the sessions check already names it, and this check's only obligation is not to
        # repeat the half of it that is false. Found by review.
        known |= {e.name for e in repo.list_unexaminable()}
    except Exception:  # noqa: BLE001 - "could not check" must not read as "none matched"
        known = None
    unmatched = None if known is None else sorted(s for s in lock_slugs if s not in known)
    return {"readable": True, "error": None, "total": len(lock_slugs),
            "sessions_checked": known is not None, "unmatched": unmatched,
            "unexpected": sorted(unexpected), "unexaminable": [e.to_dict() for e in unexaminable]}


def _cmd_schema(a, client) -> None:
    """Print the slot schema (and optionally the human framework spec) so a reasoning caller — Claude
    Code, above all — has the exact slot vocabulary + driver rule to produce a valid proposal offline."""
    from requivo.paths import FRAMEWORK
    print((FRAMEWORK / "model_schema.json").read_text(encoding="utf-8"))
    if a.framework:
        print("\n\n<!-- framework/elicitation.md (human spec) -->\n")
        print((FRAMEWORK / "elicitation.md").read_text(encoding="utf-8"))


def _cmd_context(a, client) -> None:
    """List or print the context cards — the product knowledge that grounds impact estimation. A
    reasoning caller reads this to weigh information value; pure asset I/O, no LLM.

    `--session` prints the cards *that session* was created with. A session's card selection is held
    constant across its turns on purpose — it is what the impact estimates were made against, and it
    keeps the cached prompt prefix alive — so a later turn that reads all the cards is reasoning from a
    wider context than the one the model was built on. Asking for it by session removes the step where
    a caller has to carry the list by hand and can quietly widen it."""
    from requivo.core.context import load_context
    if a.list:
        for c in available_cards():
            print(c)
        return
    if a.session:
        if a.cards:
            # Both spellings named, because #85 made them aliases: a refusal that names one of two
            # accepted flags reads as a claim that the other is not the flag you passed.
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
    # The three rows the bug template asks a reporter to assemble by hand (#247). They sit at the
    # top, together, because the point is that a paste of the first four lines is a bug report.
    print(f"  {ok} os              {r['os']}")
    if not r["model"]["name"]:
        # An exported-but-empty MODEL is not a working install: every provider call would send no
        # model id at all. `doctor` answers *is anything wrong*, so this is a finding it states
        # rather than a blank it renders calmly under a tick.
        print("  ❌ model           MODEL is set but empty — a provider call would send no model id")
    else:
        origin = "MODEL env override" if r["model"]["source"] == "env" else "default"
        print(f"  {ok} model           {r['model']['name']}  ({origin})")
    # The console's codec, reported before anything that depends on it. Only ever a line when there
    # is something to say: on a UTF-8 terminal — every developer's, which is why this shipped — the
    # answer is uninteresting and a clean report should not grow a row per non-finding.
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
    p = r["provider_anthropic"]
    prov = f"installed (v{p['version']})" if p["installed"] else "not installed"
    # #365: a `credential_problem` means a profile IS configured and the SDK could not load it --
    # naming that as "no API key" sends the reader to set a variable that was never the fault, and
    # they have no way to learn that from the output. Checked first, because it is the more specific
    # of the two False causes and the ordinary "no API key" wording must not shadow it.
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
        # Not "0 sessions". We could not look, and saying nothing found is the failure this verb
        # exists to prevent: a user told they have no sessions concludes they were deleted.
        print(f"  ❌ sessions        unreadable — {display_token(h['error'])}")
        print(f"     └─ {r['workspace']['sessions']} could not be listed. This is not the same "
              "thing as having no sessions.")
        return
    bad, lost, noted = h["inconsistent"], h["unresolved_cards"], h["notes"]
    # Only worth saying when there are sessions at all. Since #33 a session with no card selection is
    # *not* exempt: `check_selection(None)` reads the card directory now, because an install with no
    # cards refuses every load, `only=None` included — so "every card" is a selection that can fail
    # like any other.
    unchecked = not h["cards_checked"] and bool(h["total"])
    blind = h["unexaminable"] or []
    locked = h.get("locked") or {}
    # `tallies`, not `notes`: since #260 `notes` is a *key of this report* — the non-defect findings —
    # and binding the same word to the summary fragments would shadow it in the one function that
    # prints both. `_cmd_session_verify` shipped exactly that collision once, on `code`, and it
    # reached a user as a stray line where an exit code should have been.
    tallies = ([f"{len(bad)} inconsistent"] if bad else []) \
        + ([f"{len(lost)} with product context that no longer loads"] if lost else []) \
        + ([f"{len(noted)} with a note"] if noted else []) \
        + (["product context not checked"] if unchecked else []) \
        + ([f"{len(blind)} entr{'y' if len(blind) == 1 else 'ies'} that could not be examined"]
           if blind else []) \
        + ([f"{len(locked)} locked (could not check)"] if locked else [])
    # Three glyphs for three states, on the line a reader actually scans: `bad`/`lost` earn ❌;
    # `unchecked`/`blind`/`locked` are a *could not look*, which must read neither as broken nor as
    # clean, so they share the middle glyph -- pinned for `unchecked` by
    # `test_a_card_directory_that_cannot_be_read_is_unreadable_not_empty`, for `blind` by
    # `test_an_unexaminable_entry_alone_earns_the_warning_glyph_not_the_clean_tick`, and for
    # `locked` by `test_doctor_reports_a_locked_session_as_could_not_check_not_as_broken`. `noted`
    # is deliberately absent from this expression: a note is not a defect and not a could-not-look,
    # so it moves no glyph, only the row's tally. Pinned by
    # `test_a_note_does_not_move_the_sessions_glyph`.
    glyph = "❌" if (bad or lost) else (warn if (unchecked or blind or locked) else ok)
    print(f"  {glyph} sessions        {h['total']} in this workspace"
          + (f" · {' · '.join(tallies)}" if tallies else ""))
    # `display_token` on every slug, for the reason `_print_unexaminable` states two functions down
    # and this loop did not: the name is a raw directory entry. `_scan_session_root` puts it in the
    # *sessions* bucket on `(p/"session.json").exists()` alone, and the `except Exception` above turns
    # a name `validate_slug` would refuse into an ordinary `unreadable` row — so a directory whose
    # name carries a newline and holds a session.json wrote two further lines of this report at
    # column 0, in the shape of real ones. Reproduced before it was fixed; #40 is the same defect on
    # the card-name half of this verb, and `_print_non_sessions` already covers the sibling bucket.
    #
    # `problem['message']` needs no wrap and is left bare deliberately: the card names inside it have
    # been through `normalize_tokens`, which refuses a control character outright
    # (`unsafe_selector_token`) — invariant 14's second door. Wrapping it would say that guard gave us
    # nothing, which is the reading that makes the next person wrap what does not need it.
    for slug, codes in bad.items():
        safe = display_token(slug)
        print(f"     └─ {safe}: {', '.join(codes)} — run `requivo session verify {safe}`")
    for slug, problem in lost.items():
        print(f"     └─ {display_token(slug)}: {problem['message']}")
    # A row of its own, and `session verify` rather than a repair: the codes here name something the
    # session is *entitled* to have (#260), so there is nothing to fix and the remedy — if there is
    # one — is upgrading this install, which only the full message says.
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
    """The `locks` check: candidate residue under `lock_root()`, in the same three-state discipline
    as `sessions`/`other entries` above it -- a check that could not look must not render like one
    that looked and found nothing. Pinned by
    `test_the_lock_root_being_unlistable_is_not_reported_as_no_residue`.

    **Never prints the word "orphan".** The scan that produced `unmatched` and the scan of the
    current session list run a moment apart, so a session created or deleted in that gap reads
    exactly the same way for a tick without being residue at all -- the rendering says what was
    found and leaves the conclusion to the reader. Pinned by
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
    """Names under the session root that could not be examined, under the sessions check.

    Under *sessions* and not under *other entries*, because that is the one thing the failed probe
    did not settle: this may be a session and it may not, and `_print_non_sessions` claiming
    `Requivo does not read these` would be the wrong answer on the reading where it matters — a
    user's own session, invisible. Pinned by
    `test_doctor_reports_the_entry_instead_of_declaring_the_whole_root_unreadable`.

    The count on the line above stays what could be *confirmed* rather than silently absorbing
    these, and the name and error text both go through `display_token`, since the `read_meta` that
    would ordinarily have refused a name carrying a newline is exactly what could not run. Pinned by
    `test_an_unexaminable_name_carrying_a_control_character_cannot_forge_a_line`.
    """
    if not entries:
        # `[]` is a clean workspace and earns no row. The unreadable-root arm passes `None`, but it
        # has already returned above with a line of its own, so it never reaches here.
        return
    n = len(entries)
    # Detail lines under the sessions check, not a check row of its own. The row above already
    # carries the count as one of its notes and already wears the middle glyph for it; a second
    # line at check indent reading `sessions` would be two rows answering one question, and a
    # reader scanning the glyph column could not tell which was the verdict.
    for entry in entries:
        print(f"     └─ {display_token(entry['name'])} — could not be examined: "
              f"{display_token(entry['error'] or _NO_DETAIL)}")
    thing = "this is a session" if n == 1 else "these are sessions"
    print(f"     Requivo cannot tell whether {thing}, so the count above ({total}) is what it could "
          "confirm, not what is there. Nothing has been read, moved or changed; Requivo does not "
          "alter permissions in your workspace.")


def _non_session_detail(entry: dict) -> str:
    """One entry of `sessions.non_sessions`, as a clause naming what is there and nothing else.

    Every branch is an observation. There is no arm that says *a leftover lock directory*, because
    that is a conclusion the directory cannot support — `.lock` and nothing else is what an older
    `session_lock` left and also what an interrupted unzip leaves, and this verb's evidence is the
    directory and only the directory (invariant 14).

    **Every value interpolated here comes off disk, so every one goes through `display_token`** —
    the names and the error text alike, both able to end this line and start another at column 0 of
    `doctor`'s own report. The names were wrapped first and the `error` interpolations went
    unwrapped for a release, from the same open-ended `except Exception` in the store. Pinned by
    `test_a_name_read_off_disk_cannot_forge_a_line_of_the_report_that_names_it` for the names and
    `test_the_error_text_on_a_non_session_line_cannot_forge_a_line_either` for `error`, with
    `test_no_error_string_reaches_a_printed_line_unwrapped` as the class guard over the rest of the
    file."""
    kind, error = entry["kind"], entry["error"]
    if kind == "unknown":
        return f"could not be examined — {display_token(error)}"
    if kind == "file":
        return "a file, not a directory"
    if kind == "symlink":
        # Not followed, and not described as whatever it points at. Reporting a symlink's target
        # contents would list another directory's filenames into a report about this workspace,
        # and would answer `directory` about something that is not one.
        return "a symbolic link, not followed; nothing here is read from its target"
    if kind == "other":
        return "neither a file nor a directory"
    if error:
        # Not "an empty directory". We could not look inside, and an empty directory is the one
        # shape that costs nothing on POSIX (`rename(2)` replaces an empty destination) — so the two
        # answers must not be spelled the same way.
        return f"a directory whose contents could not be listed — {display_token(error)}"
    total, shown = entry["entry_count"], entry["entries"] or []
    if not total:
        return "an empty directory"
    names = ", ".join(display_token(n) for n in shown)
    more = f", … ({total} in total)" if total > len(shown) else ""
    return f"a directory holding {total} entr{'y' if total == 1 else 'ies'}: {names}{more}"


def _print_non_sessions(entries: list[dict] | None) -> None:
    """Things under the session root that are not sessions, named with what they cost.

    `doctor` owns this rather than `session verify` for the reason the state exists: `verify` is
    per-session and takes a slug, and the defining property of one of these is that no listing
    produces its name, so there is no slug for anybody to type. `doctor` already answers about the
    workspace as a whole and already carries the three-state discipline this needs (#67).

    Its own row rather than a note on the sessions row, because `0 in this workspace` stays true —
    none of this is a session — and folding it in would trade a correct count for a vague one.

    The consequence is printed and not left to be inferred. A finding with no remedy is a line
    people learn to scroll past, and this one is invisible until it strikes: the rename that claims
    a slug (invariant 11) loses to anything already occupying the name, and `SessionService` then
    falls through to its hash-suffixed candidate without a word. It is printed only for a name
    `create_session` can actually be asked for — `canonical_dir` refuses anything else long before a
    rename — because a consequence that cannot happen is noise on a report that is already a
    judgement call.

    The hint used to end *which is the only symptom any of this has*, and #114 made that false in the
    same release it would have shipped in: `session import` now refuses such a name by its own code
    rather than converting it into a message about a failed move. Two individually correct commits —
    one teaching a verb to refuse, one leaving the diagnostic describing the world before it — is a
    defect neither diff review can see, so the sentence names both consequences now. Pinned by
    `test_the_name_taken_hint_names_what_import_does_about_it`."""
    if not entries:
        # `None` here is the unreadable-root arm, which has already returned above with its own
        # line; `[]` is a clean workspace, and a clean check earns no row on this report.
        return
    n = len(entries)
    print(f"  🟡 other entries   {n} entr{'y' if n == 1 else 'ies'} under this directory that "
          "Requivo does not read")
    for entry in entries:
        taken = "  [name taken]" if entry["slug_shaped"] else ""
        print(f"     └─ {display_token(entry['name'])} — {_non_session_detail(entry)}{taken}")
    # Marked per row and explained once. Repeating the mechanism under every row buried the rows
    # themselves the moment there was more than one, and the rows are the finding.
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
    sc.set_defaults(func=_cmd_schema)

    cx = sub.add_parser("context", help="list or print the product context cards")
    cx.add_argument("--list", action="store_true", help="list available card stems instead of content")
    # `--context` is the documented primary spelling of this selector across the CLI (#85) and
    # `--cards` is a permanent alias. Here the dest stays `cards` — the option strings are what the
    # user types, the dest is what `_cmd_context` already reads, and moving it would be a rename
    # dressed up as an alias.
    cx.add_argument("--context", "--cards", metavar="CARDS", dest="cards",
                    help="comma-separated subset to print (default: all). Alias: --cards.")
    cx.add_argument("--session", metavar="SESSION",
                    help="print exactly the cards this session was created with")
    cx.set_defaults(func=_cmd_context)
