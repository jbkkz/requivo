"""`requivo model`: show, validate, apply and diff a session's model, the verbs Claude Code drives.
The decision is never taken here: `validate_proposal` and `SessionService.update_model` own it.
"""

from __future__ import annotations

from requivo.core.perimeters import resolve_perimeter
from requivo.core.validation import validate_proposal
from requivo.deterministic._shared import JSON_HELP, _read_document, print_json
from requivo.services.sessions import SessionService


def _cmd_model_show(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    # `resolve_slug` does not check existence; a missing session is `session_not_found` with the root
    # named, a claimed-but-empty one `session_has_no_model` from `load_model` (#250, #720).
    if not svc.exists(slug):
        raise svc.no_session(slug)
    model = svc.load_model(slug)
    if a.json:
        print_json(model.model_dump(mode="json"))   # #717: the same document, under the ensure_ascii contract
        return
    print(model.model_dump_json(indent=2))


def _cmd_model_validate(a, client) -> None:
    """Validate a proposal file, the gate Claude Code runs before applying."""
    data = _read_document(a.proposal)
    require = not a.allow_partial
    perimeter = resolve_perimeter(a.perimeter)   # an unknown id is `unknown_perimeter`, never software
    if a.session:   # #743: the session's own vocabulary, the one `model apply` holds it to
        svc = SessionService()
        slug = svc.resolve_slug(a.session)
        if not svc.exists(slug):
            raise svc.no_session(slug)
        perimeter = resolve_perimeter(svc.meta(slug).perimeter)
    out = validate_proposal(data, require_complete=require, perimeter=perimeter)
    n_slots = len(out.model)
    if a.json:
        print_json({"status": "valid", "slots": n_slots})
        return
    print(f"✅ Proposal is valid ({n_slots} slots).")


def _cmd_model_apply(a, client) -> None:
    """Apply a proposal as a new revision, always the complete slot set: `apply` *replaces* the model,
    and `--allow-partial` used to read as if it merged."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    data = _read_document(a.proposal)
    result = svc.update_model(slug, data, expected_revision=a.expected_revision,
                              provenance={"provider": "claude-code", "surface": "cli-apply"})
    if a.json:
        print_json(result.to_dict())
        return
    print(f"✅ Applied → revision {result.revision}")
    print(f"   changed slots: {', '.join(result.changed_slots) or '(none)'}")
    if result.invalidated_decisions:
        print(f"   decisions to re-validate: {len(result.invalidated_decisions)}")
    if result.invalidated_challenges:
        print(f"   premises to re-examine: {len(result.invalidated_challenges)}")
    if result.invalidated_exclusions:
        print(f"   exclusions to reconsider: {len(result.invalidated_exclusions)}")
    if result.invalidated_thresholds:
        print(f"   thresholds to reconsider: {len(result.invalidated_thresholds)}")
    if result.stale_artifacts:
        print(f"   now stale: {', '.join(result.stale_artifacts)}")
    rd = result.readiness
    print(f"   readiness: {'READY' if rd.ready else 'not ready'}"
          + (f" — blocking: {', '.join(rd.blocking_slots)}" if rd.blocking_slots else ""))


def _cmd_model_diff(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    data = _read_document(a.proposal)
    # `diff` holds the proposal to the same bar as `apply`.
    result = svc.diff(slug, data)
    if a.json:
        print_json(result.to_dict())
        return
    print(f"Would apply as revision {result.revision}")
    print(f"  changed slots: {', '.join(result.changed_slots) or '(none)'}")
    if result.stale_artifacts:
        print(f"  would go stale: {', '.join(result.stale_artifacts)}")


def register_model(sub) -> None:
    """Attach the `model` verb group to the main `requivo` subparser."""
    # model
    mp = sub.add_parser("model", help="show, validate, apply, or diff a model")
    ms = mp.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    msh = ms.add_parser("show", help="print a session's current model")
    msh.add_argument("session", help="session slug or path")
    msh.add_argument("--json", action="store_true", help=JSON_HELP)
    msh.set_defaults(func=_cmd_model_show)

    mv = ms.add_parser("validate", help="validate a proposal file (no session write)")
    mv.add_argument("proposal", help="path to a proposed model JSON, or '-' to read it from stdin")
    # `--session` once lived here read by nothing; since #743 it names the vocabulary, as `--perimeter` does.
    vocab = mv.add_mutually_exclusive_group()
    vocab.add_argument("--session", default=None, metavar="SLUG",
                       help="check against this session's perimeter, the slots `model apply` would hold it to")
    vocab.add_argument("--perimeter", default=None, metavar="ID",
                       help="check against this installed perimeter's slots (default: software)")
    mv.add_argument("--allow-partial", action="store_true",
                    help="check a partial projection for well-formedness only — `apply` and `diff` "
                         "always require the full slot set, because applying replaces the model")
    mv.add_argument("--json", action="store_true", help=JSON_HELP)
    mv.set_defaults(func=_cmd_model_validate)

    ma = ms.add_parser("apply", help="validate a proposal and apply it as a new revision")
    ma.add_argument("session", help="session slug or path")
    ma.add_argument("proposal", help="path to a proposed model JSON, or '-' to read it from stdin")
    ma.add_argument("--expected-revision", type=int, default=None,
                    help="only apply if the session is still at this revision (optimistic lock)")
    ma.add_argument("--json", action="store_true", help=JSON_HELP)
    ma.set_defaults(func=_cmd_model_apply)

    md = ms.add_parser("diff", help="show what a proposal would change (no write)")
    md.add_argument("session", help="session slug or path")
    md.add_argument("proposal", help="path to a proposed model JSON, or '-' to read it from stdin")
    md.add_argument("--json", action="store_true", help=JSON_HELP)
    md.set_defaults(func=_cmd_model_diff)
