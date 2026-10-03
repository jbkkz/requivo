"""`requivo session export/import/restore`: the archive verbs (#550). Export and import are thin wrappers
over `SessionService.export_archive`/`import_archive`, whose one implementation is
`services/archives.py` (#702). The transient-rename retry they share with the store is
`core/persistence/atomic.py`'s.
"""
from __future__ import annotations

import os
from pathlib import Path

from requivo.core import persistence as store
from requivo.core.errors import InvalidModelError, ModelUnreadableError
from requivo.core.integrity import newest_readable_revision, readable_revision
from requivo.core.perimeters import resolve_perimeter
from requivo.core.persistence import _replace_with_retry
from requivo.core.selectors import display_token
from requivo.deterministic._shared import print_json
from requivo.services.sessions import SessionService


def _cmd_session_export(a, client) -> None:
    """Write `SessionService.export_archive`'s zip to `-o` (default `<slug>.requivo.zip`), renamed into
    place complete or not at all. `test_session_export_survives_a_transient_permission_error`.
    The default filename's reserved-stem overlap: `decision: reserved-stem-export-filenames-are-not-a-live-gap`."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    data = svc.export_archive(slug)
    dest = Path(a.output) if a.output else Path.cwd() / f"{slug}.requivo.zip"
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    try:
        tmp.write_bytes(data)
        _replace_with_retry(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    if a.json:
        print_json({"slug": slug, "archive": str(dest)})
        return
    print(f"Exported session '{slug}' → {dest}")


def _cmd_session_restore(a, client) -> None:
    """Copy a readable `revisions/NNNN-model.json` over `model.json`: the explicit repair for a torn
    session (#210). Consent, not automation; the revision history is untouched. A candidate is trusted
    only when its `content_hash` matches the recorded `model_hash` (an empty record is unconfirmed);
    a named `--revision` is refused, never substituted, and only the default searches for the newest
    trusted one. No `--json` and no repository seam, both scoped out deliberately."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    with svc.repo.lock(slug):
        meta = svc.meta(slug)
        n = meta.current_revision
        if n <= 0:
            raise InvalidModelError(
                f"session '{slug}' has no applied revision to restore from (current_revision is {n})",
                details={"slug": slug, "current_revision": n})
        hashes = {r.revision: r.model_hash for r in meta.revisions}
        d = store.canonical_dir(slug)
        if a.revision is not None:
            target_rev = a.revision
            if not 1 <= target_rev <= n:
                raise ModelUnreadableError(
                    f"session '{slug}' has revisions 1..{n}; {target_rev} is out of range",
                    details={"slug": slug, "revision": target_rev, "current_revision": n})
            found = readable_revision(d, target_rev, expected_hashes=hashes,
                                       perimeter=resolve_perimeter(meta.perimeter))
            if found is None:
                raise ModelUnreadableError(
                    f"revisions/{target_rev:04d}-model.json is missing, does not parse, or does not "
                    "match the hash session.json recorded for it -- refusing to restore from it",
                    details={"slug": slug, "revision": target_rev})
            payload = found.payload
        else:
            found = newest_readable_revision(d, n, expected_hashes=hashes,
                                              perimeter=resolve_perimeter(meta.perimeter))
            if found is None:
                raise ModelUnreadableError(
                    f"session '{slug}' has no readable, trusted revision file (1..{n}) to restore "
                    "from",
                    details={"slug": slug, "current_revision": n})
            target_rev, payload = found.revision, found.payload
        model_path = d / "model.json"
        # Temp file + rename, through `_replace_with_retry` for a transient Windows lock (#524).
        tmp = model_path.with_name(f".{model_path.name}.{os.getpid()}.restore.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            _replace_with_retry(tmp, model_path)
        finally:
            tmp.unlink(missing_ok=True)
    print(f"✅ Restored model.json for '{display_token(slug)}' from revision {target_rev}.")
    print("  The revision history is untouched — this is not a new revision.")
    if target_rev != n:
        print(f"  This is a partial repair: revision {n}'s own content could not be read or "
              f"trusted and cannot be recovered. `session verify` will keep reporting this session "
              f"as inconsistent, correctly.")
    print(f"  requivo session verify {display_token(slug)}")


def _cmd_session_import(a, client) -> None:
    """Import an archive file through `SessionService.import_archive`'s report, into this workspace."""
    meta, replaced = SessionService()._import_archive_report(Path(a.archive), force=a.force)
    target = store.canonical_dir(meta.slug)
    if a.json:
        # `slug`/`path`, the spelling every sibling verb uses; `replaced` is the guard's own answer (#111).
        # `test_import_json_names_the_session_and_its_directory_the_way_its_siblings_do`.
        print_json({"slug": meta.slug, "path": str(target), "replaced": replaced})
        return
    # Same as `session init`: the line's subject is where the session landed on this machine.
    print(f"Imported session '{meta.slug}' → {target}" + (" (replaced an existing session)" if replaced else ""))
