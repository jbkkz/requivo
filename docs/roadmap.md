# Roadmap

> A snapshot of what exists and what's planned. Not a commitment.

## Available today

- **Discovery engine** — the questions that could change the solution, multi-turn refinement, and
  the decision brief (with challenges that contest the premise)
- **The model as a durable product** (`model.json`) — every artifact regenerable from it without
  redoing discovery
- **A dependency graph** — `requivo impact` shows a change's blast radius; a discovery turn flags the
  generated files a change makes stale
- **Artifact generators** — PRD, acceptance criteria, an uncertainty-aware estimate, user stories,
  a delivery epic, release notes
- **Three interfaces over one engine** — the CLI, the Claude Code plugin, and the local Web interface,
  each a thin layer over the same Core and session format
- **Versioned sessions** — per-revision provenance (provider, model, surface, prompt hash), optimistic
  locking, and a **published, forward-compatible format** (see [compatibility.md](compatibility.md))
- **A regression harness** — consensus over repeated runs, separating a real effect from sampling noise
- **A self-contained wheel** — prompts, schema, context cards and the Web assets ship in the package;
  a user-level context directory (`REQUIVO_CONTEXT_DIR`) adds cards without a checkout
- **Tool-neutral epic export** (`epic.json`) and **tracker adapters** — idempotent, n8n-ready
  issue-creation plans for GitHub and GitLab

## Next

- An HTTP API / MCP façade — another thin layer over the same Core (for automation and integrations)
- A Jira adapter, alongside GitHub and GitLab
- Delivery integrations — authenticated push (via n8n), Notion and Confluence
- Context tooling — validation and assisted generation of context cards

## Direction

- A full artifact chain from a single model — the reasoning layer beneath product delivery
- Surfaces over one engine: **Requivo Core**, **Requivo for Claude Code**, **Requivo Web**
