"""`requivo session init/list/show/migrate/rescope/delete`: the lifecycle verbs (#550).
`export`/`import`/`restore` are `archives.py`'s; `verify` is `verify.py`'s.
"""
from __future__ import annotations

from pathlib import Path

from requivo.core import persistence as store
from requivo.core.errors import InvalidModelError, RequivoError, SessionExistsError, SessionUnreadableError
from requivo.core.persistence import UnexaminableEntry
from requivo.core.selectors import display_token
from requivo.deterministic._shared import _NO_DETAIL, EXIT_DEGRADED, _read_source, _resolve_cards, print_json
from requivo.paths import session_root
from requivo.services.sessions import SessionService


def _cmd_session_init(a, client) -> None:
    request = _read_source(a.request)
    if not request.strip():
        raise InvalidModelError("session init needs a request (a sentence or a file path)")
    cards = _resolve_cards(a.context)
    meta = SessionService().create_session(
        request, context_cards=cards, slug=a.slug, provider=a.provider)
    # `canonical_dir` direct, justified (#76): where the session landed is the answer asked for, and
    # `SessionRepository` exposes no path.
    if a.json:
        # init is idempotent, so `revision` tells a caller whether it got an existing session with a model.
        print_json({"slug": meta.slug, "session_id": meta.session_id,
                     "path": str(store.canonical_dir(meta.slug)), "context_cards": meta.context_cards,
                     "revision": meta.current_revision})
        return
    print(f"Created session '{meta.slug}' → {store.canonical_dir(meta.slug)}")
    print("  No model yet. Produce a proposal and run:")
    print(f"    requivo model apply {meta.slug} proposal.json")




def _session_list_row(entry) -> dict:
    """One `--json` row with the same key set whether the session could be read or not (invariant 8):
    `null` where the fact is missing, never `0` or `""`; `readable` is what a consumer branches on."""
    if not entry.readable:
        return {"slug": entry.slug, "revision": None, "provider": None, "updated_at": None,
                "readable": False, "error": entry.error or _NO_DETAIL}
    m = entry.meta
    return {"slug": m.slug, "revision": m.current_revision, "provider": m.provider,
            "updated_at": m.updated_at, "readable": True, "error": None}




def _session_list_line(entry) -> str:
    """One terminal row; a session that could not be read still gets one, and still names itself.
    Every text field on both branches is untrusted and goes through `display_token` (#40, #62):
    `SessionMeta.slug` is the file body's own field, not the validated directory name, and the error
    text is collapsed to one escaped line. `--json` is covered by `json.dumps`'s `ensure_ascii`:
    `test_session_show_json_escapes_a_control_character_before_it_reaches_a_line` (#70)."""
    if not entry.readable:
        return (f"  {display_token(entry.slug):<40} could not be read — "
                f"{display_token(entry.error or _NO_DETAIL)}")
    m = entry.meta
    return (f"  {display_token(m.slug):<40} rev {m.current_revision}  "
            f"({display_token(m.provider or '—')}, {display_token(m.updated_at)})")




def _cmd_session_list(a, client) -> None:
    """Every session, degrading the ones that cannot be read rather than failing for the set
    (invariant 15, #7, #62), through `list_entries()`: the guard is above the rows. Adding a read
    to this row means adding its guard: `test_a_break_below_the_metadata_does_not_reach_this_listing`."""
    entries = SessionService().list_entries()
    degraded = [e for e in entries if not e.readable]
    if a.json:
        # An object, not a bare array (#87), so a field can be added; `degraded` makes exit 4 readable on stdout.
        print_json({"sessions": [_session_list_row(e) for e in entries],
                     "degraded": len(degraded), "session_root": str(session_root())})
    elif not entries:
        print(f"No sessions under {session_root()}.")
    else:
        print(f"Sessions under {session_root()}:")
        for e in entries:
            print(_session_list_line(e))
        if degraded:
            n = len(degraded)
            print()
            # `entr{y,ies}`, not `session{,s}` (#80): one row can be an entry nobody could examine.
            print(f"{n} entr{'y' if n == 1 else 'ies'} could not be read. "
                  f"`requivo session verify <slug>` reports what is wrong in full.")
    # Raised after the listing is printed, never instead of it: the exit code is the third state.
    if degraded:
        raise SystemExit(EXIT_DEGRADED)




def _cmd_session_show(a, client) -> None:
    """One session's metadata. Every string on this path comes out of `session.json`'s body and is
    untrusted, so all eight go through `display_token` (#70); the ints and bools are not wrapped,
    since `read_meta` refuses a string there. `session_id` is sliced *before* it is escaped, or the
    slice can cut an escape sequence. `test_session_show_leaves_an_ordinary_session_byte_for_byte`,
    `test_session_show_json_escapes_a_control_character_before_it_reaches_a_line`."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    meta = svc.meta(slug)
    if a.json:
        print_json(meta.model_dump())
        return
    print(f"Session '{display_token(meta.slug)}'  (id {display_token(meta.session_id[:12])}…)")
    print(f"  created  {display_token(meta.created_at)}")
    print(f"  updated  {display_token(meta.updated_at)}")
    print(f"  revision {meta.current_revision}")
    print(f"  provider {display_token(meta.provider or '—')}   "
          f"model {display_token(meta.model_name or '—')}")
    # `display_token` (#40): the one card-name render site `normalize_tokens` never reaches.
    print("  context  " + (", ".join(display_token(c) for c in meta.context_cards)
                           if meta.context_cards else "all cards"))
    if meta.artifact_status:
        print("  artifacts:")
        for t, st in meta.artifact_status.items():
            # The explicit stale flag is the whole rule (invariant 1). Padded *after* escaping.
            print(f"    {display_token(t):<12} {display_token(st.filename):<26} "
                  f"rev {st.revision}  {'STALE' if st.stale else 'fresh'}")




def _legacy_request_text(legacy_dir: Path) -> str:
    """The exact request text `migrate_legacy` would read from this legacy directory, mirroring its
    fallback byte for byte, so `_cmd_session_migrate` can tell an interrupted migrate from an
    unrelated session on the same slug (#262). Takes the directory, not the slug (#76)."""
    for name in ("request.md", "request.txt"):
        p = legacy_dir / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""




def _scan_legacy_root(root: Path) -> tuple[list[str], list[UnexaminableEntry]]:
    """Partition the legacy `out/` root into legacy sessions and entries that could not be examined,
    the three-outcome scan `_scan_session_root` applies to the canonical root (invariant 15). A missing
    root is two empty lists; a root that cannot be listed still raises.
    `test_the_bulk_migrate_command_degrades_an_unreadable_legacy_directory_rather_than_crashing`,
    `test_a_totally_unlistable_legacy_root_refuses_cleanly_instead_of_crashing`."""
    if not root.exists():
        return [], []
    slugs: list[str] = []
    unreadable: list[UnexaminableEntry] = []
    for p in sorted(root.iterdir(), key=lambda p: p.name):
        try:
            is_legacy = (p / "model.json").exists()
        except Exception as e:  # noqa: BLE001 - the third outcome, not a failure of the listing.
            # `Exception`, not `OSError`: the ways a probe can fail are open-ended. `BaseException` is not caught.
            unexaminable_entry = UnexaminableEntry(p.name, str(e))
            unreadable.append(unexaminable_entry)
            continue
        if is_legacy:
            slugs.append(p.name)
    return slugs, unreadable




def _cmd_session_migrate(a, client) -> None:
    """The bulk migration of every legacy `out/<slug>/` session into the canonical store, the only
    reader of that layout. The `session_exists` check is reporting; the guard is `migrate_legacy`'s
    atomic claim. A revision-0 shell whose request text matches the legacy one is `interrupted`, never
    `skipped_already_present` (`test_an_interrupted_migration_is_reported_distinctly_from_already_present`,
    `test_an_unrelated_revision_zero_session_at_a_legacy_slug_is_not_called_interrupted`). Every
    per-slug read is inside a per-slug guard (invariant 15; #262, #371, #411), and an entry the scan
    could not examine is reported under `unreadable` and folds into `EXIT_DEGRADED`."""
    from requivo.paths import output_root
    root = output_root()
    try:
        slugs, unreadable = _scan_legacy_root(root)
    except OSError as e:
        # A root that could not be listed is a clean `RequivoError` (exit 1, "no answer"), not
        # `EXIT_DEGRADED`, since nothing was examined (#411).
        raise SessionUnreadableError(
            f"could not list legacy sessions under {display_token(str(root))}: {e}",
            details={"source": str(root)},
        ) from e
    migrated, skipped, interrupted, errors = [], [], [], []
    repo = SessionService().repo
    for slug in slugs:
        try:
            # `repo.exists(slug)` can refuse a reserved-name legacy slug (#372) and belongs inside the guard:
            # `test_session_migrate_survives_a_reserved_name_legacy_directory_beside_a_healthy_one`.
            occupied = repo.exists(slug)
        except RequivoError as e:
            errors.append({"slug": slug, "error": str(e)})
            continue
        if occupied:
            try:
                meta = repo.read_meta(slug)
                # Both reads that decide `interrupted` vs `skipped` are inside the same try (#371):
                # `test_session_migrate_survives_one_undecodable_legacy_request_beside_a_healthy_session`.
                is_interrupted = (meta.current_revision == 0
                                   and repo.request_text(slug) == _legacy_request_text(root / slug))
            except (RequivoError, OSError, UnicodeDecodeError) as e:
                errors.append({"slug": slug, "error": str(e)})
                continue
            if is_interrupted:
                interrupted.append(slug)
            else:
                skipped.append(slug)
            continue
        try:
            # No repository equivalent: this verb is a statement about two filesystem layouts.
            store.migrate_legacy(slug)
        except SessionExistsError:
            skipped.append(slug)
            continue
        except RequivoError as e:
            errors.append({"slug": slug, "error": str(e)})
            continue
        migrated.append(slug)
    degraded = bool(errors or interrupted or unreadable)
    if a.json:
        print_json({"migrated": migrated, "skipped_already_present": skipped,
                     "interrupted": interrupted, "errors": errors,
                     "unreadable": [e.to_dict() for e in unreadable], "source": str(root)})
    else:
        print(f"Legacy sessions under {root}:")
        print(f"  migrated: {', '.join(migrated) or '(none)'}")
        if skipped:
            print(f"  skipped (already in canonical store): {', '.join(skipped)}")
        if interrupted:
            print(f"  present but empty (interrupted migrate?) — delete .requivo/sessions/<slug> and "
                  f"re-run: {', '.join(interrupted)}")
        if errors:
            print("  could not migrate:")
            for e in errors:
                # Both fields are untrusted: a directory name, and an error message that can quote file content.
                print(f"    {display_token(e['slug'])}: {display_token(e['error'])}")
        if unreadable:
            # Never established to be a session, only a name the scan could not stat into (#411).
            print("  could not examine (skipped, not counted as a session):")
            for e in unreadable:
                print(f"    {display_token(e.name)}: {display_token(e.error)}")
        print("  Legacy files were preserved (read-only).")
    # Raised after the receipt is printed, never instead of it.
    if degraded:
        raise SystemExit(EXIT_DEGRADED)




def _cmd_session_rescope(a, client) -> None:
    """Re-scope a session's context-card selection; the rules live on `SessionService.rescope`.
    `--context` is required, and `--context ""` means every card: `test_session_rescope_requires_context`,
    `test_session_rescope_to_all_cards_reports_none`."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    cards = _resolve_cards(a.context)
    result = svc.rescope(slug, cards)
    if a.json:
        print_json(result.to_dict())
        return
    previous = (", ".join(display_token(c) for c in result.previous_context_cards)
               if result.previous_context_cards else "all cards")
    now = (", ".join(display_token(c) for c in result.context_cards)
          if result.context_cards else "all cards")
    if not result.changed:
        print(f"Session '{display_token(slug)}' is already scoped to these cards — nothing changed.")
        print(f"  context  {now}")
        return
    print(f"✅ Re-scoped '{display_token(slug)}' → revision {result.revision}")
    print(f"  previous  {previous}")
    print(f"  now       {now}")
    if result.revision > 0:
        print("  Turns already reasoned were reasoned under the previous selection and are "
             "untouched; the next turn reasons against the new one.")


def _cmd_session_delete(a, client) -> None:
    """Irreversibly remove a session under the same lock as every compound mutation; `session
    export` first is the undo story. A nonexistent slug is refused as `session_not_found`:
    `test_session_delete_refuses_a_nonexistent_slug_with_session_not_found`,
    `test_a_session_removed_through_session_delete_leaves_no_lock_residue`."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    svc.delete_session(slug)
    if a.json:
        print_json({"slug": slug, "deleted": True})
        return
    print(f"Deleted session '{slug}'.")




