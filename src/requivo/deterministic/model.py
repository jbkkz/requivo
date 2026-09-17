"""`requivo model`: show, validate, apply and diff a session's model, the verbs Claude Code drives.
The decision is never taken here: `validate_proposal` and `SessionService.update_model` own it.
"""

from __future__ import annotations

from requivo.core.errors import SessionNotFoundError
from requivo.core.validation import validate_proposal
from requivo.deterministic._shared import _read_document, print_json
from requivo.services.sessions import SessionService


def _cmd_model_show(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    # `resolve_slug` does not check existence, and `load_model` raises the same code for a missing
    # directory and a claimed-but-empty session; checked here so the two messages differ (#250).
    if not svc.exists(slug):
        raise svc.no_session(slug)
    try:
        model = svc.load_model(slug)
    except SessionNotFoundError:
        # The narrower "claimed but never discovered" case, kept in sync with `cli.py`'s `_resolve_ref` (#250).
        raise SessionNotFoundError(
            f"session '{slug}' has no model yet — only the request was captured. Run "
            f"`requivo discover` on the same request to analyse it (or, in Claude Code, "
            f"/requivo:discover).",
            details={"slug": slug},
        ) from None
    print(model.model_dump_json(indent=2))


def _cmd_model_validate(a, client) -> None:
    """Validate a proposal file, the gate Claude Code runs before applying."""
    data = _read_document(a.proposal)
    require = not a.allow_partial
    out = validate_proposal(data, require_complete=require)
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
    msh.set_defaults(func=_cmd_model_show)

    mv = ms.add_parser("validate", help="validate a proposal file (no session write)")
    mv.add_argument("proposal", help="path to a proposed model JSON, or '-' to read it from stdin")
    # A `--session` flag lived here and was read by nothing; `model diff <slug> <proposal>` already means it.
    mv.add_argument("--allow-partial", action="store_true",
                    help="check a partial projection for well-formedness only — `apply` and `diff` "
                         "always require the full slot set, because applying replaces the model")
    mv.add_argument("--json", action="store_true")
    mv.set_defaults(func=_cmd_model_validate)

    ma = ms.add_parser("apply", help="validate a proposal and apply it as a new revision")
    ma.add_argument("session", help="session slug or path")
    ma.add_argument("proposal", help="path to a proposed model JSON, or '-' to read it from stdin")
    ma.add_argument("--expected-revision", type=int, default=None,
                    help="only apply if the session is still at this revision (optimistic lock)")
    ma.add_argument("--json", action="store_true")
    ma.set_defaults(func=_cmd_model_apply)

    md = ms.add_parser("diff", help="show what a proposal would change (no write)")
    md.add_argument("session", help="session slug or path")
    md.add_argument("proposal", help="path to a proposed model JSON, or '-' to read it from stdin")
    md.add_argument("--json", action="store_true")
    md.set_defaults(func=_cmd_model_diff)
