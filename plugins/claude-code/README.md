# Requivo for Claude Code

Requirements discovery for Claude Code. Turn a vague request into a structured, versioned requirements
model that separates what is known, what is assumed and what is still open. From it you can
generate a decision brief or a PRD. **Reasoning runs in your Claude Code session, so there is no API key to configure.**

Requivo asks a question only when the answer would materially change the solution. The rest it infers
and marks as an assumption to confirm, so you can see what a plan is resting on before you commit to
the scope.

The plugin adds three journey skills — `/requivo:run`, `/requivo:status` and the documents — plus the
generator skills listed under *The generators* below. `/requivo:run` is the whole conversation in one
skill: give it a request, a path or a slug (or nothing, to resume), and it discovers, asks, waits for
your prose answers and folds each one in, stopping when the session is ready, converged, or you say
so. `/requivo:status` is where things stand, a local read. The rest turn the model into a document.

## Two things to know before the first command

**The reasoning happens here.** The skills read the request and the product context, reason in *this*
Claude Code session, and pipe a structured proposal into the deterministic `requivo` CLI, which
validates it, versions it and tracks what rests on what. There is no API key, no model choice and no
endpoint to set. (Requivo has a second, optional mode where the CLI calls the Anthropic API and does
the reasoning itself. That is not this plugin, and you do not need it.)

**Sessions are written next to you.** A discovery lands in `.requivo/sessions/<slug>/` under your
current workspace, meaning the directory Claude Code is running in, unless `REQUIVO_WORKSPACE` says
otherwise. So the directory you run `/requivo:run` from decides where the work lives, and running
it from the wrong one fails in no visible way: the session is created, valid, and somewhere you will
not think to look. `requivo doctor` prints the workspace it resolved and the exact sessions directory
it will use; `requivo session list` prints what that directory already holds.

## Installing

The plugin and the engine are two separate installs, and you need both.

**The plugin**, which is the skills. This is what you already have if you installed from a marketplace; from
a fresh Claude Code it is:

```
/plugin marketplace add jbkkz/requivo
/plugin install requivo@requivo
/reload-plugins
```

Then `/help` → **Custom commands**: the skills appear under the `requivo` namespace, and are typed
as `/requivo:run`, `/requivo:status` and so on. Claude Code always namespaces a plugin's skills as
`/<plugin>:<skill>`.

**The `requivo` CLI**, the deterministic engine every skill drives. It is a Python package on PyPI,
installed the way you install any command-line tool: `uv tool install requivo`, `pipx install requivo`,
or `pip install requivo` into an environment already on your `PATH`. You do **not** need the
`requivo[anthropic]` extra; that is for the optional standalone mode above.

Then run `requivo doctor` in a terminal. It reports what it found and what it is missing; a missing
Anthropic SDK or API key is reported as informational rather than an error, which for this plugin is
the expected state. Install routes in depth, including the `pip install --user` trap that leaves
`requivo` off your `PATH`:
[getting started](https://github.com/jbkkz/requivo/blob/main/docs/getting-started.md).

**One platform prerequisite: on native Windows you also need
[Git for Windows](https://git-scm.com/downloads/win).** Every skill reaches the `requivo` CLI through
Claude Code's Bash tool, and on native Windows that tool is Git Bash. Claude Code's setup
documentation states that installing Git for Windows "enables the Bash tool by providing Git Bash",
and that without it Claude Code "runs shell commands via the PowerShell tool". If Git Bash is
installed and Claude Code does not find it, name the path in your `settings.json` under
`CLAUDE_CODE_GIT_BASH_PATH`. Under WSL there is nothing extra to install: the same page says WSL
setups do not need Git for Windows.

What a Requivo skill does on a native Windows machine with no Git Bash has not been measured, so this
page does not describe it. Treat Git for Windows as required and the case does not arise.

The plugin version tracks the Requivo release it was tested against, and the skills call CLI verbs.
Every skill's shared preflight (`REASONING.md`) now compares the two automatically on each run —
warning and continuing when the CLI is older, and saying so explicitly when the check itself could
not be made, rather than leaving the drift for a human to notice as an argparse error partway
through a skill (#251).

To run the plugin from a checkout instead, for development:
`claude --plugin-dir ./plugins/claude-code`.

## The arc

**The direct path is `/requivo:run [request | path | slug]`.** Give it the request the first time —
or nothing, to resume the session you left off on (it lists more than one to choose from) — or a slug
to jump straight to a session. It discovers, asks the questions, waits for your prose answers, folds
each one in as a new revision, and tells you when it stops: ready, no high-value question left, or
because you said so. It ends with one pointer, `/requivo:docs` when you want a document — that skill
does not exist yet (#543), so until it lands the way to get one is steps 3 and 4 below,
`/requivo:brief` or `/requivo:prd`.

The commands, in the order they are usually reached. Every one after the first takes the session
slug, which `/requivo:run` reports when it creates the session.

1. **`/requivo:run [request | path | slug]`**. Paste the client or stakeholder request in whatever
   shape it arrived. You get the first structured read of it — what the request states outright, what
   Requivo inferred and marked as an assumption, what is genuinely unknown — and the few questions
   whose answers would change the solution. Answer in prose; each answer is validated and applied as
   a new revision, and you are told what moved and what that made stale. When you revise something
   already settled, the skill says what that change reaches — the decisions to re-validate and the
   documents to regenerate, read off the dependency graph — *before* applying it. It stops when the
   session is ready, when nothing left is worth asking, or when you say so.
2. **`/requivo:status <slug>`**. Where it stands: readiness, what is still blocking, which generated
   documents need updating. A local read, so use it as often as you like.
3. **`/requivo:brief <slug>`**. The decision brief: what a reviewer needs before estimating or
   committing to scope. Saved as a tracked document, tied to the revision it was written from.
4. **`/requivo:prd <slug>`**. A PRD from the same model, with the unknowns still visible and the open
   decisions still open. Also saved and tracked.

Steps 3 and 4 are not the end of anything. The model is the durable product and each document is a view
of it, so any of them can be regenerated later from the saved model without redoing discovery.

## The skills

| Skill | What you get | Where the thinking happens |
|---|---|---|
| `/requivo:run` | The whole conversation: discovery, questions, answers, revisions, and what a revised answer reaches, in one loop | this Claude session |
| `/requivo:status` | Readiness, open questions, which documents need updating | local read, no reasoning |
| `/requivo:brief` | The decision brief, saved and tied to its revision | this Claude session |
| `/requivo:prd` | A PRD from the same model, saved and tied to its revision | this Claude session |

The ones that reason spend this session's context window. The local read does not: it calls the CLI
and prints what it computed. None of them calls an API. The dependency-graph query behind *what a
change reaches* is deterministic too — `/requivo:run` runs it for you, and the CLI's `requivo impact`
answers it directly.

## What a session holds

```
.requivo/sessions/<slug>/
├── session.json          # metadata, provenance, artifact status
├── request.md            # the request it started from
├── model.json            # the current understanding, the durable product
├── revisions/            # every applied revision, in order
└── artifacts/            # generated documents, each tied to the revision it was written from
```

The model is the source of truth and every document is a view of it, so Requivo knows what rests on
what: when the model moves, `/requivo:status` names the documents that need regenerating and
`/requivo:run` answers the same question *before* it applies a revised answer. Each revision records the
provider, the model and a hash of the prompt it was reasoned against, so a document can be traced back
to the understanding it came from.

The session format is versioned and shared. A session created here opens in the CLI and in Requivo Web,
and one created there opens here.

**Sessions stay out of git.** `.requivo/` is written into the directory you are working in, which here
is your project repository, and `request.md` holds the request verbatim. That is usually a client's own
words. So the first time Requivo creates `.requivo/`, it writes `.requivo/.gitignore` containing `*`:
git ignores the whole store, and your own `.gitignore` is left alone.

It is written once and never put back. Delete it if you want sessions committed, and they stay
committed. To share one session instead of all of them, use `requivo session export <slug> -o <slug>.zip`
and `requivo session import <slug>.zip`.

## What this is, and what it is not

What it is: structured discovery, with **known / assumed / open** kept apart on purpose; assumptions
stated rather than dissolved into confident prose; a versioned shared understanding rather than a
document; impact analysis and stale-document detection over a real dependency graph; provenance on
every revision; and deterministic validation, so a malformed proposal is refused rather than
half-applied.

It is not a general product-management assistant and does not try to be one. It does a narrow thing:
finding what could change the solution before the scope is committed.

Two things that would be reasonable to assume and are not true:

- **There is no automatic relevance routing over the product context.** Cards are read as a set. A
  session can opt into a subset when it is created, and that selection is then held constant for its
  lifetime. But nothing picks the relevant ones for you, and adding a card can sharpen one discovery
  while diluting another.
- **A generator producing something on disk does not mean an API call happened.** `stories`,
  `estimate`, `criteria`, `epic` and `release` each print and save a document and are tracked for
  staleness like every other artifact — since #542 they also reason in this Claude session, the same
  as `brief` and `prd` (see *The generators*, below).

## What the skills send to Claude

The request, the product context cards, the slot schema, and the current model of the session you are
working on. Nothing the CLI did not already hold on your machine. The request and the context cards
are treated as **data**: the skills reason about them and never follow instructions embedded in them.

The skills also never require `ANTHROPIC_API_KEY`, never hand-edit `model.json` (they propose, and the
CLI validates and writes), and never invent an answer the client did not give; an unknown is left
honestly empty.

## The generators

Seven artifact types can be produced from a session's model, and — since #542 — every one of them
reasons in *this* Claude session, with no `ANTHROPIC_API_KEY` anywhere in the loop:

| Skill | Produces | Where the thinking happens |
|---|---|---|
| `/requivo:brief` | The decision brief | this Claude session |
| `/requivo:prd` | A PRD | this Claude session |
| `/requivo:stories` | Implementable user stories | this Claude session |
| `/requivo:estimate` | A day-based, uncertainty-aware estimate (saves stories, then the estimate, against one revision) | this Claude session |
| `/requivo:criteria` | Given/When/Then acceptance criteria (a recette checklist) | this Claude session |
| `/requivo:epic` | A delivery epic — the work breakdown a dev team tracks | this Claude session |
| `/requivo:release` | Client-facing release notes | this Claude session |

Each is saved as a tracked artifact tied to the model revision it was reasoned from, and flagged stale
the moment something it rests on changes — `/requivo:status` names which ones need regenerating.

The `requivo` CLI can also produce all seven itself, in its own **optional API mode**: useful for
automation outside a Claude Code session, and it needs the `requivo[anthropic]` extra plus an
`ANTHROPIC_API_KEY` to do it. That mode is unrelated to the skills above, which never call it.
The one piece that stays CLI-only is the epic's tracker export (`--export-json`, `--github`,
`--gitlab`): each reasons the epic afresh through that API mode before writing the export, so there is
no way to export the epic a skill just saved without a new, key-requiring call. `requivo doctor`
reports whether you have the extra and the key.

Requivo Web is a local browser workspace over those same sessions: paste a request, answer the
questions, watch what each answer moved. It needs a key to analyse and generate; reading sessions the
plugin created needs none.

## More

- [Documentation](https://github.com/jbkkz/requivo/tree/main/docs): architecture, the requirements model, the session format, context cards
- [Getting started](https://github.com/jbkkz/requivo/blob/main/docs/getting-started.md)
- [The CLI](https://github.com/jbkkz/requivo/blob/main/docs/cli.md)
- [Requivo Web](https://github.com/jbkkz/requivo/blob/main/docs/web.md)
- [Repository and issues](https://github.com/jbkkz/requivo)

Licensed under the Apache License 2.0.
