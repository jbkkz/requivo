"""`requivo artifact`: save, list and show a session's artifacts. `ArtifactService` owns the source
revision and the staleness verdict; these verbs render what it returns. No LLM, no API key.
"""

from __future__ import annotations

from requivo.core import persistence as store
from requivo.core.selectors import display_document, display_token
from requivo.deterministic._shared import _read_document, print_json
from requivo.services.artifacts import ARTIFACT_FILENAMES, ArtifactService
from requivo.services.sessions import SessionService


def _cmd_artifact_save(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    content = _read_document(a.file)
    st = ArtifactService().save(slug, a.type, content, source_revision=a.revision)
    if a.json:
        # `stale` is reported on the save, since the caller who just did it is the one who can act on it.
        print_json({"type": a.type, "filename": st.filename, "revision": st.revision,
                     "stale": st.stale})
        return
    # Through the chokepoint (#36), direct rather than through the repository (#76): a printed path is a disclosure.
    where = store.artifact_path(slug, st.filename)
    print(f"Saved {a.type} → {where} (from revision {st.revision})")
    if st.stale:
        print(f"  Marked stale: the model has moved past revision {st.revision} in ways this "
              f"{a.type} rests on. Regenerate it to bring it current.")


def _cmd_artifact_list(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    items = ArtifactService().list(slug)
    if a.json:
        # An object, not a bare dict of rows, so a metadata key can join it (#87, #107).
        # `test_artifact_list_json_has_a_top_level_that_is_not_data`.
        print_json({"slug": slug, "artifacts": items})
        return
    if not items:
        print(f"No artifacts saved for '{slug}'.")
        return
    print(f"Artifacts for '{slug}':")
    for t, info in items.items():
        # The key and the filename are whatever `session.json` says (#70); escape before padding.
        print(f"  {display_token(t):<12} {display_token(info['filename']):<26} "
              f"rev {info['revision']}  {'STALE' if info['stale'] else 'fresh'}")


def _cmd_artifact_show(a, client) -> None:
    svc = SessionService()
    # Neutralized at print time only (#430): the saved bytes stay byte-identical for `integrity.py`'s
    # hashing. `test_artifact_show_cannot_be_made_to_print_a_line_a_session_wrote`.
    print(display_document(ArtifactService().show(svc.resolve_slug(a.session), a.type)))


def register_artifacts(sub) -> None:
    """Attach the `artifact` verb group to the main `requivo` subparser."""
    # artifact
    ap = sub.add_parser("artifact", help="save, list, or show generated artifacts")
    aps = ap.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    asv = aps.add_parser("save", help="save an artifact against a session")
    asv.add_argument("session", help="session slug or path")
    asv.add_argument("--type", required=True, choices=sorted(ARTIFACT_FILENAMES),
                     help="artifact type")
    asv.add_argument("--file", required=True, help="path to the artifact content, or '-' to read it from stdin")
    # No `required=True`: the omission must arrive as a structured `UnstatedSourceRevisionError`, not exit 2 (#57).
    asv.add_argument("--revision", type=int, default=None,
                     help="required: the model revision this content was reasoned from. There is no "
                          "default — the session's current revision is a different fact, and only you "
                          "know what you read")
    asv.add_argument("--json", action="store_true")
    asv.set_defaults(func=_cmd_artifact_save)

    al = aps.add_parser("list", help="list a session's artifacts + freshness")
    al.add_argument("session", help="session slug or path")
    al.add_argument("--json", action="store_true")
    al.set_defaults(func=_cmd_artifact_list)

    ash = aps.add_parser("show", help="print a saved artifact's content")
    ash.add_argument("session", help="session slug or path")
    ash.add_argument("--type", required=True, choices=sorted(ARTIFACT_FILENAMES))
    ash.set_defaults(func=_cmd_artifact_show)
