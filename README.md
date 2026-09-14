# Requivo

[![PyPI](https://img.shields.io/pypi/v/requivo)](https://pypi.org/project/requivo/)
[![Python](https://img.shields.io/pypi/pyversions/requivo)](https://pypi.org/project/requivo/)
[![CI](https://github.com/jbkkz/requivo/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/jbkkz/requivo/actions/workflows/ci.yml?query=branch%3Amain)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)][license]

> Find what could change the solution before you commit to the scope.

Paste a client or stakeholder request. Requivo identifies the assumptions and missing decisions that
could change the workflow, integrations, permissions, timeline or effort — then produces one brief you
can review before estimating.

**The shared understanding is the source of truth. Every document is generated from it.**

Built for the person who owns scoping — solutions engineers, consultants, technical PMs and
agency leads — on complex, configurable B2B products. Run it locally, then hand the decision brief
(or the Requivo Web session) to the PM or client who needs to sign off.

---

## Start here

### You have an Anthropic API key → Requivo Web

1. Install [uv](https://docs.astral.sh/uv/), if you don't already have it: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Set your key: `export ANTHROPIC_API_KEY="…"`
3. Run it: `uvx --from "requivo[web,anthropic]" requivo web`

Opens `http://127.0.0.1:8765`. No key? It still opens and offers **Explore a worked example** — the
same client email `requivo demo` replays, materialised as a real session ([demo video][demo-video]).
Analysing your own request costs roughly **$0.03–$0.06 per call**, **$0.47–$1.01** for a complete
session end to end — derived, not typed: the per-step table and its method are in
[`docs/providers.md`][providers]. Other install routes and the full platform list:
[`docs/getting-started.md`][getting-started].

[![The Requivo Web session page: the objective, the request, and what Requivo understood, split into what is confirmed and what is being assumed.][shot-session]][web]

### You use Claude Code → the plugin

On native Windows, install [Git for Windows](https://git-scm.com/downloads/win) first — it gives
Claude Code the Bash tool the skills run through; nothing extra is needed on macOS, Linux or WSL.

All four steps are typed inside Claude Code:

1. `/plugin marketplace add jbkkz/requivo`
2. `/plugin install requivo@requivo`
3. `/reload-plugins`
4. Run it: `/requivo:run "We'd like a leave approval system."`

No Anthropic key needed — reasoning happens in your own Claude session. The first
`/requivo:run` doubles as the CLI check: if `requivo` isn't on your PATH yet and you already have
`uv` or `pipx`, the plugin names the exact install command and only runs it once you say yes. Have
neither? It tells you the two lines to run yourself and to re-run the skill after — the one case
that still touches a terminal. Full skill list and detail: [plugin README][claude-code].

## Why Requivo

An LLM will happily turn a half-understood request into a polished PRD. Clean documentation is not the
same as a correct understanding — and the expensive mistakes come from the question nobody thought to
ask, the one that turns a "small feature" into a three-month build.

Requivo asks a question only when the answer would **materially change the solution**. The rest, it
infers and marks as an assumption to confirm. You spend discovery time where it moves the needle.

And because it keeps the understanding rather than just the answer, it can tell you what a *changed*
answer costs:

```text
You change one answer:
  "The migration is one-time. After cutover, the legacy system is read-only."

Requivo:
  What changed        Integrations & notifications
  Needs review        the two-way sync decision · the reconciliation risk · the decision brief
```

That is the part a chat transcript cannot do.

**The canonical example is [`examples/leave-approval/`][leave-approval]** — one line of
request, taken through the questions, the brief, and a changed answer that moves the scope. A harder,
messy multi-feature one lives in
[`examples/event-checkin-reconciliation/`][event-checkin-reconciliation], and is what
`requivo demo` replays.

## How it works

1. **Bring a request** — a sentence or a rambling email; a symptom, not a spec.
2. **Clarify high-impact unknowns** — Requivo asks only what would change the solution, and infers the
   rest as assumptions to confirm.
3. **Build and validate the understanding** — a versioned, typed model that is the durable product.
4. **Write the decision brief, and see what a changed answer costs** — a computed answer to "what does
   this invalidate?", read off the dependency graph rather than re-reasoned from scratch — and, once
   an assumption has been confirmed, which decisions on record were made before that evidence arrived
   and are worth re-reading.

The decision rule is **information value = uncertainty × impact**. Impact is estimated from the product
context you give it, so better context means sharper questions.

The **decision brief** is the smallest document a scope review can be run from: what is confirmed,
what is assumed, the decisions on record, the premises worth contesting, and what is still open. It is
not a PRD; it is what you read *before* writing one. And every downstream document — a PRD, user
stories, acceptance criteria, an uncertainty-aware estimate — generates from the same understanding,
without redoing the discovery. A delivery epic — with GitHub/GitLab issue plans — and release notes
follow the same rule once scoping is settled.

The vocabulary — what we know, what we are assuming, open question, needs updating, are we ready — and
the model underneath it: [`docs/requirements-model.md`][requirements-model].

## Architecture, data and privacy

```text
       Web          Claude Code        CLI / API
   (the product)   (an integration)  (infrastructure)
         \               |                /
                    Requivo Core
            validated, versioned understanding
```
Every interface uses the same session format and the same validated apply path — no fork, no
interface holding business logic of its own; the package also ships a stable import surface for
building on Requivo as a library. **Local by default**, no telemetry: nothing leaves your machine
except what a provider call sends to Anthropic. More, and the erasure primitive:
[`docs/architecture.md`][architecture] · [SECURITY.md][security].

## Documentation

| Doc | What it covers |
|---|---|
| [Getting started][getting-started] | Install and first run for each interface, supported platforms |
| [Web][web] | The primary interface — the local browser workspace |
| [CLI reference][cli] | Every command and flag |
| [Requirements model][requirements-model] | The vocabulary, readiness, dependencies |
| [Everything else][docs-index] | Session format, providers, context cards, evaluations, roadmap |

## Status

Actively developed; what is stable is stated, not inferred — see the [roadmap][roadmap] and the
[compatibility promise][compatibility]. **Ran it on a real request? [Tell us how it did][discovery-feedback]**
— say whether the questions were useful, useless or redundant, and what a senior PM/BA would have
asked instead. Anonymise anything client-confidential first.

## Contributing and license

Contributions are welcome — see [CONTRIBUTING.md][contributing]. Requivo is written by AI coding
agents under maintainer direction and review — CONTRIBUTING.md has the account of the controls around
that. The Core, CLI and Claude Code integration are open source under [Apache-2.0][license]; the
Requivo **name and identity** are separate from the code license — see [TRADEMARKS.md][trademarks].

[Apache-2.0][license] © jbkkz — _Requivo was previously named Product Copilot._

<!-- Links stay absolute reference definitions: PyPI renders README.md verbatim (no relative-href rewrite). -->
[license]: https://github.com/jbkkz/requivo/blob/main/LICENSE
[shot-session]: https://raw.githubusercontent.com/jbkkz/requivo/main/docs/images/web-session.webp
[demo-video]: https://github.com/jbkkz/requivo/releases/download/v3.0.0/requivo-demo.mp4
[getting-started]: https://github.com/jbkkz/requivo/blob/main/docs/getting-started.md
[web]: https://github.com/jbkkz/requivo/blob/main/docs/web.md
[claude-code]: https://github.com/jbkkz/requivo/tree/main/plugins/claude-code/
[cli]: https://github.com/jbkkz/requivo/blob/main/docs/cli.md
[providers]: https://github.com/jbkkz/requivo/blob/main/docs/providers.md
[leave-approval]: https://github.com/jbkkz/requivo/tree/main/examples/leave-approval/
[event-checkin-reconciliation]: https://github.com/jbkkz/requivo/tree/main/examples/event-checkin-reconciliation/
[requirements-model]: https://github.com/jbkkz/requivo/blob/main/docs/requirements-model.md
[architecture]: https://github.com/jbkkz/requivo/blob/main/docs/architecture.md
[security]: https://github.com/jbkkz/requivo/blob/main/SECURITY.md
[docs-index]: https://github.com/jbkkz/requivo/blob/main/docs/README.md
[compatibility]: https://github.com/jbkkz/requivo/blob/main/docs/compatibility.md
[roadmap]: https://github.com/jbkkz/requivo/blob/main/docs/roadmap.md
[discovery-feedback]: https://github.com/jbkkz/requivo/issues/new?template=discovery-feedback.md
[contributing]: https://github.com/jbkkz/requivo/blob/main/CONTRIBUTING.md
[trademarks]: https://github.com/jbkkz/requivo/blob/main/TRADEMARKS.md
