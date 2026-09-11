<!--
Thanks for contributing! Please read CONTRIBUTING.md first if you haven't.
Keep unrelated reformatting out of the diff.
-->

## Goal

<!-- What does this change do, and why? Link the issue it addresses if there is one. -->

## Layer(s) touched

<!-- Delete the ones that don't apply. -->

- [ ] Core (`requivo.core`) — engine, schema, validation, dependencies
- [ ] Providers — the LLM callers
- [ ] Services — the shared apply / artifact path
- [ ] Render / CLI
- [ ] Assets — prompts / context cards / schema
- [ ] Claude Code plugin
- [ ] Docs / meta

## Checklist

- [ ] Every commit is signed off (`git commit -s`) — see CONTRIBUTING.md's *Sign off your commits*
- [ ] `pytest tests/ -q` passes (no network / no API key needed)
- [ ] `ruff check src tests scripts` passes
- [ ] `.venv/bin/pyright` passes (same invocation as CI)
- [ ] The wheel still builds (`python -m build --wheel`) — if package layout or assets changed
- [ ] Docs updated (README / CLAUDE.md / relevant doc) if behaviour or a command changed
- [ ] If a **prompt or context card** changed: measured through the golden harness, and an intended
      baseline update is included
- [ ] No secrets, and no real / non-anonymised customer data added
- [ ] The output contract (Pydantic ↔ prompt "Output format") is still in sync, if touched

## Session-format impact

<!-- Does this change the on-disk session/model format? If yes, describe the compatibility /
     migration story. If no, write "none". -->

## Notes

<!-- Anything reviewers should know. -->
