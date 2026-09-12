# Three journey verbs, everything else is plumbing

**Slug:** `three-journey-verbs`

## Context

The user-facing surface had grown by accretion (#538). On the plugin, six skills asked the user to
run them in order, carrying a slug from one to the next and typing `/requivo:answer <slug>`
themselves after every reply. On the CLI, `--help` listed 21 top-level verbs flat — eleven journey
verbs (seven of them artifact generators) beside `doctor`, `schema`, `context`, `session`, `model`,
`artifact`, `web` and `api`, with `session` alone carrying ten sub-commands. Every verb was
defensible on its own; together they asked a first-time user to learn the model's mechanics — slug,
revision, apply, answer, artifact type, staleness — before the product had done anything for them.
Those mechanics are the engine's, not the user's.

## Decision

**The user meets three verbs.** Everything else still exists and keeps its name — scripts, n8n
flows and the plugin's own steps call it — but leaves the first screen.

| Verb | What the user gets |
|---|---|
| `run` | The whole conversation: give the request, answer questions as they come, stop when it is ready or when you say so. Discovery, answers, revisions, `expected_revision` are steps inside it, never something the user is asked for. |
| `docs` | A menu of the documents the model can produce, each with one line: what it is for, and whether it is up to date, needs updating, or has not been generated. Pick one or several. |
| `status` | Where the session stands: what is known, assumed, open, and which documents need updating. |

Four rules hold across both surfaces:

1. **A slug is shown, never required.** `run`/`docs`/`status` take the most recent session in the
   workspace, or offer a list when there are several. A slug argument is accepted, never demanded.
2. **`discover`, `answer` and `impact` are not user vocabulary.** On the plugin they are removed
   from the surface entirely (no alias, no deprecation notice — the plugin is not shipped in the
   wheel and stays on its own release line). On the CLI they stay as documented verbs, because they
   are the automation contract ([integrations.md](integrations.md)), but `--help` groups them under
   plumbing rather than beside `run`.
3. **Nothing stored changes.** Session format, `--json` payloads, verb names and artifact types are
   public ([compatibility.md](compatibility.md)); this is presentation and orchestration, one layer
   above. `run` and `docs` are thin layers over the same services every other surface uses — never a
   second implementation of an apply, a generation or a staleness rule.
4. **The plugin's `docs` must be able to produce all seven artifacts without an API key**, which is
   why keyless parity for the five generators that lacked a skill (#542) shipped before `docs`
   itself, rather than as a follow-up that could ship a five-sixths-keyless menu.

`web/` is out of scope: it is already the guided experience these two verbs bring the CLI and the
plugin toward.

**Status at #540/#541 (this change):** `run` exists on the CLI as the verb over `discover`'s
interactive loop and `answer`'s apply path, and rule 1 is enforced on the CLI's `run`, `status` and
`impact` through one resolver, `SessionService.resolve_default_session()`. `docs` (#543, #544) and
taking `discover`/`answer`/`impact` off the plugin surface (#545) are separate, later slices of this
same lot — see the issue's own checklist for the rest.

## What breaking it cost

Nothing has gone red for this yet — it is a decision made ahead of most of its own consequences,
the shape `docs/decisions/README.md`'s "Tense" section describes. The cost it answers is measured
directly in #538's own problem statement: a plugin arc that required typing a slug back after every
turn, and a flat 21-verb `--help` that put the two verbs a first-time user needs seventh and eighth.

## Alternatives rejected

- **Deprecate `discover`/`answer`/`impact` outright, on both surfaces.** Rejected for the CLI: they
  are the documented automation contract other tools and scripts already call
  ([integrations.md](integrations.md)), and a deprecation notice on a stable interface is a breaking
  change under [compatibility.md](compatibility.md) for no functional gain — `run` is additive
  beside them, not a replacement for the scripted surface.
- **Give `run` its own second orchestration of discovery and refinement**, reasoning and applying
  directly rather than calling `DiscoveryService`. Rejected on the same grounds #77 already settled
  for `discover`'s own loop (see `CLAUDE.md`'s architecture section): every interface is a thin layer
  over the shared services, and a second implementation of an apply or a generation is exactly the
  drift `tests/test_boundaries.py` exists to catch.
- **Make the resolver a CLI-only convenience**, guessing a default session in `cli.py` without a
  service-layer seam. Rejected because a second surface — Requivo Web, or a future one — would need
  the identical rule, and a rule that lives in one interface is not enforced (CLAUDE.md, invariant
  13's own argument, one layer over): `SessionService.resolve_default_session()` is the one resolver
  every journey verb on every surface reads.
