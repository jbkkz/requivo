"""`requivo artifact`: save, list and show a session's generated artifacts.

An artifact is a *view* of the model, so a save records the revision it was reasoned from and a
listing reports freshness against the model as it stands now. Neither judgment is made here:
`ArtifactService` owns the source revision and the staleness verdict, and these verbs render what it
returns.

Part of the deterministic surface, so no LLM and no API key. `register_artifacts(sub)` is composed
into the package's single `register()` by `deterministic/__init__.py`.
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
        # `stale` is reported on the *save*, not only on a later `artifact list`. Saving an artifact
        # reasoned from a superseded revision is legitimate and now recorded honestly — but the caller
        # that just did it is the one who can act on it, and it should not have to ask again to find out.
        print_json({"type": a.type, "filename": st.filename, "revision": st.revision,
                     "stale": st.stale})
        return
    # Through the chokepoint, not re-joined here (#36). This line only prints the path, which is
    # exactly how it survived the sweeps that closed the writes and the read — `artifact_path` says
    # why display is not exempt, and which caller can actually hand this an unvalidated `st.filename`.
    # Direct rather than through `SessionRepository` (#76) for the reason that chokepoint exists: the
    # validation is of a *path*, and the repository has no path to validate.
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
        # `items` is keyed by artifact type, so printed bare the payload's top level was data and no
        # metadata key could ever join it — #87's defect on `session list`, one shape along (#107).
        # Wrap, do not restructure: the rows are untouched and `slug` is deliberately the only new
        # key. `test_artifact_list_json_has_a_top_level_that_is_not_data` carries the argument.
        print_json({"slug": slug, "artifacts": items})
        return
    if not items:
        print(f"No artifacts saved for '{slug}'.")
        return
    print(f"Artifacts for '{slug}':")
    for t, info in items.items():
        # The same two untrusted strings `session show`'s artifact block renders, in the other verb
        # that renders them (#70). `ArtifactService.list` passes `session.json`'s `artifact_status`
        # through, so the key and the filename are whatever the file says; `core/integrity.py`
        # already treats that filename as untrusted input. `slug` above is the resolved directory
        # name, not the body's, and `revision`/`stale` are `int`/`bool` — none of the three needs it.
        # Escape before padding: the widths exist so the block can be scanned, and padding a value
        # that is about to grow quotes aligns it to a length the render does not have.
        print(f"  {display_token(t):<12} {display_token(info['filename']):<26} "
              f"rev {info['revision']}  {'STALE' if info['stale'] else 'fresh'}")


def _cmd_artifact_show(a, client) -> None:
    svc = SessionService()
    # Neutralize at print time only (#430): what `.show()` returns is what is on disk and what the web
    # download route serves, and `core/integrity.py`'s hashing rests on both staying byte-identical.
    # `display_document`, not `display_text`, because the document's own newlines and tabs are its
    # layout (`test_artifact_show_cannot_be_made_to_print_a_line_a_session_wrote` and its control,
    # `test_artifact_show_leaves_an_ordinary_document_byte_for_byte`). `cli.py`'s four generation
    # verbs took the identical shape in #449; this was never the class's last unguarded member.
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
    # No `required=True`: the omission has to arrive as a structured `UnstatedSourceRevisionError` the
    # `--json` envelope can carry, not as argparse's usage error and exit 2 (see `ArtifactService.save`).
    # The help string is what has to say it, and until #57 it said the opposite — it still advertised
    # the default #6 removed, which is the text a user reads while deciding whether to pass the flag.
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
