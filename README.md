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

### 1. See a real run — no key, nothing installed

```bash
uvx --from requivo requivo demo
```

Ten seconds, offline — or [watch the short demo video][demo-video] instead. A messy client
email, the questions Requivo raised against it, the decision
brief it wrote, and then the part that is not reasoned at all: change one answer, and it reports what
that invalidates, computed from the dependency graph. No Anthropic key, no network, no extras — the
`demo` path needs neither the provider SDK nor a credential.

### 2. Requivo Web — the way to use it

A local browser workspace. Paste a request, answer the few questions that could change the solution,
see what each answer moved, generate one decision brief.

```bash
uvx --from "requivo[web,anthropic]" requivo web    # opens http://127.0.0.1:8765
```

[![The Requivo Web session page: the objective, the request, and what Requivo understood, split into what is confirmed and what is being assumed.][shot-session]][web]

Analysing a request of your own needs a key: set `ANTHROPIC_API_KEY` in your environment or a `.env`
file. Without one the interface still opens, reads existing sessions and tells you what is missing —
and its home page offers **Explore a worked example**, which materialises the same messy client email
`requivo demo` replays as a real, browsable session in your workspace. One click, no key, no network:
the understanding, the open questions, the readiness verdict and the decision brief, all read from the
payload bundled with the install. Refining it or generating any other document is the part that needs
the key.

**What it costs.** You pay Anthropic directly, on your own key: roughly **$0.03 to $0.06 per call**,
and **$0.46 to $1.01** for a complete session — a full discovery plus every artifact — at current
rates. Every command that spends prints its own exact tokens and estimated cost when it finishes.
The per-step table, its method and its limits are in [`docs/providers.md`][providers]; the figures
there are recomputed from the rate table on every build, so they cannot quietly go stale.

One command, nothing installed — [uv](https://docs.astral.sh/uv/) fetches Requivo into a temporary
environment and runs it (`curl -LsSf https://astral.sh/uv/install.sh | sh` if you don't have uv). To
keep it around, `uv tool install "requivo[web,anthropic]"` and then just `requivo web`; `pipx install`
works the same way. Prefer plain pip? [`docs/getting-started.md`][getting-started] has the
virtualenv route — avoid `pip install --user`, which succeeds while leaving `requivo` off your PATH.

Sessions stay on your machine, the server binds to localhost, and nothing leaves your workspace — no
accounts, no database, no remote storage. See [`docs/web.md`][web].

Two other ways in, on the same local sessions — nothing is locked to the interface you start in:

- **[Claude Code][claude-code]** — an integration. `/plugin marketplace add jbkkz/requivo`,
  `/plugin install requivo@requivo`, `/reload-plugins`, then `/requivo:discover <request>`. Reasoning goes through your own Claude session, so there is no
  extra API key — but the skills drive the `requivo` CLI, so it still has to be installed (above) and
  on your PATH. If it is not, every skill says so and how to fix it, rather than failing at the shell.
  On native Windows it also needs [Git for Windows](https://git-scm.com/downloads/win), which is what
  gives Claude Code the Bash tool the skills run through.
- **[CLI][cli]** — infrastructure. `requivo discover | status | brief …`, for automation and
  anything you drive from a script or a pipeline. The same services are also reachable over HTTP:
  `requivo api serve` (the `[api]` extra, experimental) serves a local REST API on
  `127.0.0.1:8767` with its OpenAPI docs at `/docs` — loopback needs no credential; anything
  wider requires `REQUIVO_API_TOKEN` and refuses to start without it.

Install and first run in depth: [`docs/getting-started.md`][getting-started].

---

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

---

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

---

## Architecture

```text
       Web          Claude Code        CLI / API
   (the product)   (an integration)  (infrastructure)
         \               |                /
                    Requivo Core
            validated, versioned understanding
```

Every interface uses the same session format and the same validated apply path — there is no fork, and
no interface holds business logic of its own. The three differ in weight, not in what they can reach.
The Core is provider-independent: no LLM, no network. More:
[`docs/architecture.md`][architecture].

**Building on Requivo as a library?** The package ships a [PEP 561 marker][py-typed] (`py.typed`) and
declares a small, deliberately stable import surface — the services, the `SessionRepository` and
`ReasoningProvider` protocols, the boundary contracts, the error vocabulary, `requivo.usage` — priced
like every other promise on the [compatibility page][compatibility]. Pin exactly (`requivo==X.Y.Z`);
everything not on that list, including `providers.anthropic` internals, can move in a minor. Building
a non-file `SessionRepository` (a Postgres backing, most concretely)? `pip install 'requivo[testing]'`
and subclass `requivo.testing.repository_conformance.SessionRepositoryConformance` in your own pytest
suite — the same behavioural proof `FileSessionRepository` and this repo's in-memory fake both run
against, extracted so an external implementation can hold itself to it too.

---

## Data and privacy

**Local by default.** Sessions live in `.requivo/sessions/` in your workspace. No telemetry, no
analytics — Requivo never phones home. When a provider is used, each turn sends your request, answers,
prompts, schema and loaded context cards to the Anthropic API, and to no other server; in Claude Code
mode, reasoning goes through your own Claude session. Treat anything you type as content sent to a
model provider. **`requivo session delete <session>`** (or the same control on a session's own page in
Requivo Web) is the erasure primitive: it removes a session's directory and everything under it,
irreversibly — export it first if you might want it again. Full notes: [SECURITY.md][security].

---

## Documentation

| Doc | What it covers |
|---|---|
| [Getting started][getting-started] | Install and first run for each interface |
| [Web][web] | The primary interface — the local browser workspace |
| [CLI reference][cli] | Every command and flag |
| [Requirements model][requirements-model] | The vocabulary, readiness, dependencies |
| [Architecture][architecture] | Core, services, surfaces |

Everything else — session format, providers, context cards, evaluations, product validation, roadmap,
open-source strategy — is indexed in [`docs/`][docs-index].

---

## Supported platforms

| Platform | Python | Tested in CI |
|---|---|---|
| Linux | 3.9 – 3.14 | every version, every push |
| macOS | 3.9 – 3.13 | 3.9 and 3.13 |
| Windows | 3.9 – 3.13 | 3.9 and 3.13 |

An untested platform and a supported platform look identical from outside, so this table says which
is which. The ends of the version range are tested on macOS and Windows rather than every minor
version, and the ends are the point: a platform's own standard library can behave differently at each
one. Windows on 3.9 cannot resolve a symlink whose target is missing, where Windows on 3.13 can —
which once left a path-containment guard holding on twelve of thirteen CI legs and not on the
thirteenth. Differences in the language itself show on the Linux axis, which runs all six (a `3.14`
leg landed in #298; the macOS/Windows ends and the `3.9` floor itself are a separate, not yet
decided, question — see `docs/compatibility.md`).

Those legs test the `requivo` **package**. Nothing in CI exercises the Claude Code plugin, which runs
inside Claude Code rather than inside Python, and on native Windows that plugin carries a prerequisite
of its own: [Git for Windows](https://git-scm.com/downloads/win), for the reason the
[plugin README][claude-code] gives.

Requivo reads and writes **UTF-8 everywhere**, regardless of the machine's locale or the console's
codepage. A session written on one machine reads back byte-identically on another. Where a console
cannot represent a character Requivo prints, the character is escaped rather than dropped and never
crashes the command — `requivo doctor` reports your console's encoding when there is something worth
saying about it. A file you pass in (`requivo discover ./brief.md`) must be UTF-8; one that is not is
refused by name rather than silently decoded into something that reads like prose and is wrong.

---

## Status

Actively developed. The Core, CLI, Claude Code plugin and local Web interface are usable today, and
what is stable is stated rather than inferred from the release number: Requivo is versioned with
SemVer, and the **session format is a published contract** — versioned, forward-compatible, and
shared by every interface. What is guaranteed and what is deprecated is written down in
[compatibility][compatibility]. Output is non-deterministic — treat the decision brief as a
senior colleague's read, not an oracle, and get expert review for any legal/tax/compliance flag it
raises.
See the [roadmap][roadmap].

**Ran it on a real request? [Tell us how it did][discovery-feedback].** No telemetry means this
report is how the questions get better — say whether they were useful, useless or redundant, and
what a senior PM/BA would have asked that Requivo did not. Anonymise anything client-confidential
before posting.

---

## How this is built

Requivo is written by AI coding agents under maintainer direction and review. Most commits carry an
agent co-author trailer, and the maintainer decides what gets built, reviews every change and merges
it.

The controls around that are the interesting part, and they are not incidental to it. Nothing reaches
`main` except through a squash-merged pull request that passed every required check, on the platform
matrix above. The test suite is hermetic — no API calls, no network, no build step — and a large share
of it guards the codebase against its own authors rather than against users: a boundary test that
fails when the engine imports a provider, an encoding test that walks every file read in the
repository, a test that fails when a comment cites a test that does not exist. Each of those exists
because a plausible change broke something quietly, and the fix was to make the next such change
loud.

That is the honest account of who wrote this. Judge it on the guards and the record, not on the
authorship.

---

## Contributing and license

Contributions are welcome — see [CONTRIBUTING.md][contributing]. The Core, CLI and Claude Code
integration are open source under [Apache-2.0][license]. The Requivo **name and identity** are separate from
the code license — see [TRADEMARKS.md][trademarks].

[Apache-2.0][license] © jbkkz

> _Requivo was previously named Product Copilot._

<!-- Repo-relative links are written as references and resolved absolutely, because pyproject
     sets `readme = README.md`: PyPI renders this whole file as the project page and does not
     rewrite relative hrefs, so every one of them 404s there. Keeping the URLs in one block is
     what stops that from costing the prose its line width. -->
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
[py-typed]: https://github.com/jbkkz/requivo/blob/main/docs/compatibility.md#the-python-import-surface--the-declared-seam-423
[roadmap]: https://github.com/jbkkz/requivo/blob/main/docs/roadmap.md
[discovery-feedback]: https://github.com/jbkkz/requivo/issues/new?template=discovery-feedback.md
[contributing]: https://github.com/jbkkz/requivo/blob/main/CONTRIBUTING.md
[trademarks]: https://github.com/jbkkz/requivo/blob/main/TRADEMARKS.md
