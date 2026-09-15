"""`requivo model`: show, validate, apply and diff a session's model.

The model is the product and the artifacts are views of it, so these are the verbs Claude Code
drives: it reasons a proposal with its own Claude, pipes the JSON in on stdin, and `validate` or
`apply` decides. The decision is never taken here. `validate_proposal` in Core states the slot
vocabulary and the completeness rule, and `SessionService.update_model` is the single validated
apply path, which is what keeps the revision, the diff and the staleness flags in one place.

Part of the deterministic surface, so no LLM and no API key. `register_model(sub)` is composed into
the package's single `register()` by `deterministic/__init__.py`.
"""

from __future__ import annotations

from requivo.core.errors import SessionNotFoundError
from requivo.core.validation import validate_proposal
from requivo.deterministic._shared import _read_document, print_json
from requivo.services.sessions import SessionService


def _cmd_model_show(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    # `resolve_slug` does not check existence -- a bare slug it does not recognise passes through
    # unchanged -- and `load_model` below raises the identical `session_not_found` code whether the
    # directory is missing entirely or exists with no model yet. Checked here so the two do not share
    # a message: the pre-existing behaviour already blurred them ("has no model yet" for a slug that
    # was never created at all), and reusing #250's friendlier wording without this check would have
    # made that worse -- "only the request was captured" is affirmatively false when nothing was.
    if not svc.exists(slug):
        raise svc.no_session(slug)
    try:
        model = svc.load_model(slug)
    except SessionNotFoundError:
        # The existence check above ruled out "no such session", so this is the narrower "claimed but
        # never discovered" case -- the same one `cli.py`'s `_resolve_ref` reconstructs for `status`
        # and `impact` (#250). Kept in sync with that copy rather than shared with it: the two live on
        # opposite sides of a layer boundary this package does not import across.
        raise SessionNotFoundError(
            f"session '{slug}' has no model yet — only the request was captured. Run "
            f"`requivo discover` on the same request to analyse it (or, in Claude Code, "
            f"/requivo:discover).",
            details={"slug": slug},
        ) from None
    print(model.model_dump_json(indent=2))


def _cmd_model_validate(a, client) -> None:
    """Validate a proposal file — the gate Claude Code runs before applying. On success prints a tiny
    confirmation (or `--json` {status: valid}); on failure the structured error surfaces via app()."""
    data = _read_document(a.proposal)
    require = not a.allow_partial
    out = validate_proposal(data, require_complete=require)
    n_slots = len(out.model)
    if a.json:
        print_json({"status": "valid", "slots": n_slots})
        return
    print(f"✅ Proposal is valid ({n_slots} slots).")


def _cmd_model_apply(a, client) -> None:
    """Apply a proposal as a new revision. Always the complete slot set: `apply` *replaces* the model,
    and `--allow-partial` used to read as if it merged — it did not, so applying one slot left a
    one-slot model where fifteen had been. Validating a projection is `model validate --allow-partial`;
    a real partial update needs a merge semantics this command never had."""
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
    if result.stale_artifacts:
        print(f"   now stale: {', '.join(result.stale_artifacts)}")
    rd = result.readiness
    print(f"   readiness: {'READY' if rd.ready else 'not ready'}"
          + (f" — blocking: {', '.join(rd.blocking_slots)}" if rd.blocking_slots else ""))


def _cmd_model_diff(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    data = _read_document(a.proposal)
    # `diff` is the dry run of `apply`, so it holds the proposal to the same bar — a projection that
    # `apply` would refuse must not be previewed here as though it would land.
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
    # (A `--session` flag lived here, promising validation "against a session's context", and was read
    # by nothing. Whatever it was going to mean, `model diff <slug> <proposal>` already means it: it
    # reports exactly what applying the proposal to that session would change, without writing.)
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
