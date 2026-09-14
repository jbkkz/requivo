# Security

Requivo is a solo-maintained open-source project. This document states what the tool does with your
data and how to report a problem.

## What leaves your machine

The engine makes calls to the **Anthropic API** and nothing else. On each run it sends, as the prompt:

- your **client request** (and any answers you provide in refinement turns);
- the **prompts**, the **model schema**, and the **context cards** (`context/*.md`) that inform the run;
- the **saved model** (`.requivo/sessions/<slug>/model.json`) when you regenerate an artifact from it.

There is **no telemetry, no analytics, and no other network call**. Nothing is sent to any server
operated by this project. Your `.requivo/` session store stays local. Do not put secrets (API keys, passwords,
personal data) into a request or a context card — treat everything you type as prompt content that
will be sent to the model provider.

Your `ANTHROPIC_API_KEY` is read from the environment / `.env` and used only to authenticate those
API calls. Keep `.env` out of version control (it is gitignored).

In **Claude Code** mode Requivo makes no provider call of its own and needs no `ANTHROPIC_API_KEY`:
the reasoning happens in your Claude Code session. The same content — your request, your answers,
the model and the context cards — still reaches that session's model provider, under Claude Code's
configuration and data policy rather than Requivo's. Treat it as prompt content either way.

**`requivo session delete <session>`** (or the same control on a session's own page in Requivo Web)
is the erasure primitive: it removes a session's directory and everything under it, irreversibly —
export it first if you might want it again.

## The local Web interface

`requivo web` is a **local, single-user** interface. It has **no authentication** and binds to
`127.0.0.1` by default. Do not expose it on an untrusted network (`--host 0.0.0.0` prints a warning).
The Anthropic key is read from the server environment and is never rendered into a page or logged;
slugs are validated (no path traversal) and only the interface's own static assets are served — never
your workspace, `.requivo/`, `.env` or `.git`. See [docs/web.md](docs/web.md).

## Untrusted input / prompt injection

The **client request**, the client's **answers**, and the **context cards** are treated as *untrusted
business data* — material for the engine to analyse, never instructions for it to obey. The engine
prompts state this trust boundary explicitly, so text like "ignore the above instructions" embedded
in a request is modelled as a requirement to capture, not a command to follow.

This is a mitigation, not a guarantee: LLM prompt-injection defences are imperfect. Do not run
Requivo on requests from a source you would not trust to read your prompts, and review
generated artifacts before acting on them.

Artifacts saved to `.requivo/sessions/<slug>/artifacts/` are unsanitized markdown: they are the
model's own reply, written to disk as-is, so that the file, the integrity hash and the web download
all stay byte-identical to what was generated. `requivo artifact show`, and `requivo prd`/`criteria`/
`epic`/`release` on the ordinary generation that first produces the document, neutralize control
characters (a raw escape sequence, for instance) before printing to your terminal; opening the file
directly with `cat` or another tool that does not do the same is at the reader's own risk.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use **[GitHub private vulnerability reporting](https://github.com/jbkkz/requivo/security/advisories/new)**
— it is enabled on this repository, it is the link the new-issue chooser already points at, and it
gives you an authenticated private thread with the maintainer that does not depend on an email
address being published anywhere. If you cannot use it, email the maintainer via the address on the
repository owner's GitHub profile.

Either way, please include:

- a description of the issue and its impact;
- steps to reproduce;
- any suggested remediation.

You will get an acknowledgement as soon as possible. This is a solo-maintained project with no formal
SLA, but security reports are prioritised over feature work.
