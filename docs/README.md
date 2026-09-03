# Requivo documentation

The [main README](../README.md) is the orientation and activation guide. These documents hold the
depth — each has one clear responsibility; the README links to them rather than repeating them.

## Guides

- [Getting started](getting-started.md) — install and first run for each interface
- [Web](web.md) — the primary interface: the local, single-user browser workspace
- [Claude Code plugin](../plugins/claude-code/) — the integration: skills, workflow, install (no extra API key)
- [CLI reference](cli.md) — every command and flag
- [Integrations](integrations.md) — driving Requivo from an automation: the CLI contract, the
  `requivo-epic` envelope, a worked n8n flow, and what deliberately is not built

## Reference

- [Architecture](architecture.md) — Core, providers, services, interfaces
- [Requirements model](requirements-model.md) — slots, evidence/coverage, readiness, dependencies,
  and which outputs mirror the client's language
- [Session format](session-format.md) — the `.requivo/` layout, revisions, provenance
- [Compatibility](compatibility.md) — what is public, what may change, what is deprecated
- [Providers](providers.md) — the Anthropic provider, models, cost
- [Context cards](context-cards.md) — teaching the engine your product
- [Evaluations](evaluations.md) — the golden harness for prompt/context changes, and for the
  interactive loop's deep-turn grounding
- [Product validation](product-validation.md) — the manual protocol for "is this better than a strong prompt?"

## Project

- [Roadmap](roadmap.md) — what exists and what's next
- [Open-source strategy](open-source-strategy.md) — what is public and what stays private
- [Cloud boundary](cloud-boundary.md) — the consumption contract any hosted deployment builds
  against, and the upstream changes that make it clean
- [Readiness audits](audits/) — point-in-time 360° assessments; latest:
  [2026-09](audits/2026-09-product-readiness-audit.md)
- [Plugin bundling](plugin-bundling.md) — why the Claude Code plugin does not bundle the CLI
- [Decision records](decisions/) — the arguments no test can go red for: a fact about something
  outside the repository, a rejected alternative, a cost tradeoff with a threshold
- [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Governance](../GOVERNANCE.md) ·
  [Trademarks](../TRADEMARKS.md)
