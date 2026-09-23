# Three journey verbs, everything else is plumbing

**Slug:** `three-journey-verbs`

## Context

The surface had grown by accretion (#538). The plugin asked for six skills in order, the user
carrying a slug and typing `/requivo:answer <slug>` after every reply; the CLI's `--help` listed 21
verbs flat, the two a newcomer needs seventh and eighth. Each verb was defensible; together they
asked the user to learn the engine's mechanics — slug, revision, apply, artifact type, staleness —
before the product had done anything.

## Decision

**The user meets three verbs**: `run` (the whole conversation, stopping on ready or on request),
`docs` (a menu of the seven documents, each with its purpose and freshness) and `status` (known,
assumed, open, and what needs updating). Everything else keeps its name and leaves the first screen.

1. **A slug is shown, never required** — the most recent session, or a list when there are several,
   through one resolver, `SessionService.resolve_default_session()`.
2. **`discover`, `answer` and `impact` are not user vocabulary.** Off the plugin entirely (#545); on
   the CLI they stay as the automation contract ([integrations.md](../integrations.md)), grouped
   under plumbing.
3. **Nothing stored changes** — format, `--json`, verb names and artifact types are public; `run`
   and `docs` are thin layers over the shared services.
4. **The plugin's `docs` produces all seven artifacts without a key**, so keyless parity (#542)
   shipped before `docs` (#543, #544).

`web/` is out of scope: it is already the guided experience. Landed across #540–#545.

## What breaking it cost

Nothing went red: a decision ahead of its consequences. The cost it answers is #538's measurement —
a slug typed back every turn, and a flat 21-verb `--help`.

## Alternatives rejected

- **Deprecate `discover`/`answer`/`impact` on the CLI** — a breaking change to the documented
  automation contract, for nothing `run` does not add beside them.
- **A second orchestration inside `run`** — #77 settled it: every interface is a thin layer over
  `DiscoveryService`.
- **A CLI-only default-session guess** — a rule living in one interface is not enforced; the resolver
  sits in the service every surface reads.
