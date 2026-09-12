# Decision: the Claude Code plugin does not bundle the CLI

> Spike [#94](https://github.com/jbkkz/requivo/issues/94). Measured against Claude Code **2.1.238**
> and the plugin documentation as published on **2026-08-21**. Verdict: **no** — every route that
> removes the second install step either breaks a constraint outright or costs more than the step it
> saves. The preflight in [#93](https://github.com/jbkkz/requivo/issues/93) is the answer.

## The question

The plugin ships skills and a manifest. The `requivo` CLI those skills call is a separate PyPI
install, deliberately not in the plugin — so a user meets four steps where they expected one:

```
install plugin → install Python or uv → install requivo → fix PATH → use plugin
```

The spike asked whether the current plugin spec offers a way to collapse that, and what it costs.
The spec moves, so nothing below is recalled; each claim names how it was established.

## What the plugin spec measurably supports today

| Claim | How it was established |
| :--- | :--- |
| A plugin-root `bin/` puts executables on the Bash tool's `PATH` | Documented (*Plugins*, file-locations table: "Executables added to the Bash tool's `PATH`. Files here are invokable as bare commands in any Bash tool call while the plugin is enabled") **and observed**: five `…/plugins/cache/<mkt>/<plugin>/<version>/bin` entries were on `PATH` inside a Bash tool call on this machine |
| The `bin` entry is added whether or not the directory exists | Observed: **four of those five** plugins ship no `bin/` at all, and their entry is on `PATH` regardless. Only one of the five directories exists on disk |
| Plugin `bin` entries come **last** | Observed on macOS, and **not documented anywhere**. This is the property that would keep a shim from shadowing a real install, and it is the one property with no written guarantee behind it |
| `claude plugin validate` accepts a plugin carrying `bin/` and an executable | Ran it on a minimal probe plugin: `✔ Validation passed` |
| The Bash tool environment does **not** carry `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA` or `CLAUDE_PROJECT_DIR` | Observed: 34 variables in a Bash tool call, none of the three. Corroborated by the reference, which exports them "to hook processes and to MCP and LSP server subprocesses" and lists the Bash tool nowhere |
| `${CLAUDE_PLUGIN_DATA}` is the blessed home for a bootstrap | Documented, and it names our case: "Use it for **Python dependencies**, dependencies locked with Yarn or pnpm, and packages whose lifecycle scripts must run" |
| `${CLAUDE_PLUGIN_ROOT}` is version-scoped and must not hold state | Documented: "treat it as ephemeral and don't write state there". Observed: the cache is `<plugin>/<version>/`, and two plugins here have two version directories side by side. Orphans are swept "roughly 14 days later" |
| `SessionStart` is the only *unattended* bootstrap point, and it cannot ask | Documented: `SessionStart` is "Context only… **No blocking or decision control**", and "runs on every session, so keep these hooks fast" |
| A `PreToolUse` hook **can** ask, and can rewrite the command it asks about | Documented: `permissionDecision` of `"ask"` "prompts the user to confirm", and `updatedInput` "Modifies the tool's input parameters before execution… Combine with `allow` to auto-approve, or `ask` to show the modified input to the user". This is the one place in the spec where an automatic, visible, refusable install is expressible — see option D below |
| A skill's Bash call cannot prompt either | Observed: stdin is not a TTY in a Bash tool call |
| A `SessionStart` hook can hand values to later Bash calls | Documented: `CLAUDE_ENV_FILE`, a file the hook appends `export` lines to |
| Submissions are screened | Documented: "The review pipeline runs the same check on every submission, along with **automated safety screening**." The criteria themselves are not published |
| On Windows without Git Bash, **there is no Bash tool** | Documented: "On Windows without Git Bash, the tool is enabled automatically and Claude Code doesn't register the Bash tool at all" — the PowerShell tool takes over. `bin/` is specified against the Bash tool only |

What the released wheel needs at runtime, measured with `pip download requivo` and a clean venv:

- Python **3.9** floor; exercised end-to-end on 3.9.6 (`requivo doctor --json` returns `schema.ok`
  and `context.ok`).
- **7 wheels, 2.7 MB** downloaded: `requivo`, `pydantic`, `pydantic-core`, `typing-extensions`,
  `typing-inspection`, `annotated-types`, `python-dotenv`.
- **17 MB** on disk as a venv; **6.6 s** cold with pip's cache disabled, on a fast link.

That is small. Size was never what made this hard.

## What was tried

**A — a `bin/requivo` shim that repairs only `PATH`.** No network. Written and exercised: it
excludes its own directory from the `PATH` scan, defers to any real `requivo` found ahead of it, and
otherwise looks for an interpreter that can `import requivo` and execs `python -m requivo`
(`src/requivo/__main__.py` exists, so that entry point is real and was confirmed to work). Two
prototype runs settled the precedence question: with a real install earlier on `PATH`, `requivo`
resolves to the real one and the shim never executes; with none, the shim is what a bare `requivo`
resolves to. So a shim genuinely does not compromise `pip install requivo`.

**B — a first-run bootstrap into a plugin-local virtualenv.** A `SessionStart` hook comparing a
bundled marker against a copy in `${CLAUDE_PLUGIN_DATA}` — the pattern the reference documents for
exactly this — creating a venv there, `pip install requivo` into it, and exporting its path through
`CLAUDE_ENV_FILE` so the shim from A can find it. Every mechanism this needs exists. It was not
built past the shim, because it fails a constraint before it fails a mechanism.

**C — vendoring the source tree.** Excluded by the issue, and rightly: two copies of the engine is a
worse problem than one install step.

**D — a `PreToolUse` hook that offers the install.** Not in the original option set; it came out of
review of this record and it is the only design that satisfies the *refusable* constraint outright.
A plugin-shipped hook matching `Bash(requivo:*)` fires before the tool call, finds `requivo` missing,
and returns `permissionDecision: "ask"` with `updatedInput` rewriting the command to run the install
first. Claude Code then shows the user the modified command and asks. That is automatic, visible,
and refusable by pressing no. It also solves the data-directory problem for free, because a hook is
a hook process and does receive `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA`. Not built: it is
rejected below on grounds that do not need a prototype.

## The decision

**Nothing ships. The plugin stays inert and the second install step stays.**

Three reasons, in the order that decided it. The first covers both bootstrap shapes, B and D.

**An unattended bootstrap cannot ask, and the one mechanism that can ask is the most invasive thing
a plugin can install.** The constraint was that a bootstrap "must say what it is doing and be
refusable", and that splits the option set cleanly. `SessionStart` — option B — is the only hook
that fires with no user action at all, and it is documented as having no decision control: it cannot
block, cannot prompt, and its stdout becomes context for the model rather than a question for the
person. Stdin is not a TTY. So B is a silent network install by construction, which is the exact
thing the constraint forbids; make it opt-in behind an environment variable and the first run fails
with instructions, which *is* #93's preflight with 17 MB of machinery bolted to it.

Option D does satisfy the constraint, and is refused for a different reason. A `PreToolUse` hook
returning `ask` with `updatedInput` genuinely shows the user the command and lets them decline. What
it costs is the plugin's whole character: a hook that intercepts shell calls and substitutes command
text is the most powerful thing in the plugin surface, and it is precisely what the screening column
added to this spike is asking about. Trading "no hooks, no executables, nothing to review" for "a
plugin that rewrites Bash commands" to save one `pip install` is a bad trade at any screening
outcome, and it is the clever answer where the constraint list asked for the simple one. It also
does not remove the install — it only moves the prompt — and it leaves every interpreter problem
below untouched, because a rewritten `pip install requivo` still needs a `pip` to run.

**On Windows the mechanism may not be there at all.** `bin/` is specified against the Bash tool, and
on a Windows machine without Git Bash the Bash tool is not registered. Nothing in the documentation
extends `bin/` to the PowerShell tool. So the one platform where "fix your `PATH`" hurts most is the
platform where the fix is least reliable. (This is worth knowing beyond this spike: the skills'
`allowed-tools: Bash(requivo:*)` rests on the same assumption. It was out of scope here and was
taken up as [#121](https://github.com/jbkkz/requivo/issues/121), which settled the reader-facing
half: Git for Windows is now stated as a native-Windows prerequisite on the plugin README, the
repository README and `docs/getting-started.md`. What a skill declaring that grant actually does on
a machine with no Bash tool is still unmeasured.)

**The shim cannot find its own data directory.** `CLAUDE_PLUGIN_DATA` is absent from the Bash tool
environment. A shim can derive its plugin root from `$0`, but that root is version-scoped and
documented as ephemeral, so a venv placed there is rebuilt on every plugin update and each orphaned
copy lingers about two weeks. Routing the real path through `CLAUDE_ENV_FILE` from a `SessionStart`
hook works, and it means adding a hook to a plugin whose main virtue is having none — while doing
nothing about the first reason. Option D escapes this one: a hook process does receive
`CLAUDE_PLUGIN_DATA`. That is the only reason of the three D escapes, and it is the cheapest of
them.

Option A survives all three, and still loses. It removes one of the four steps — the `PATH` one —
and leaves the other three. To buy that it puts the first executable into a plugin that currently
ships none, and rests its safety on an undocumented `PATH` ordering. #93 fixes the same failure with
prose. *Prefer the simple maintainable answer over the clever one* was in the constraint list, and
it points at #93.

## What each option costs

Every macOS cell below was **run on a macOS machine**. Every Linux and Windows cell is **reasoned**,
from the documentation quoted above and from how those platforms package Python — none of it was
executed there, and this record has no Linux or Windows leg behind it. The screening column is
reasoned throughout: the criteria are not published, so only the size of the artifact being screened
is a fact.

| | macOS (observed) | Linux (reasoned) | Windows (reasoned) | Screening profile (reasoned) |
| :--- | :--- | :--- | :--- | :--- |
| **Ship nothing (#93 preflight)** | Unchanged: one `pip install` | Unchanged | Unchanged; still the platform where `PATH` breaks most often | **Unchanged.** Eleven skills and a manifest (nine files, 52 KB measured before #542 added five more skills; not re-measured), no executable, no hook, no MCP server, no network call at any point in the plugin's life |
| **A — `PATH`-repair shim** | Works. Needs an interpreter that can `import requivo`; `python3` is present, `python` and `py` are not | Expected to behave as macOS does — same POSIX shell, same `python3` convention — but not run there | Undefined without Git Bash, where the Bash tool is not registered. With Git Bash, needs an extensionless shebang script to be found and executed from `PATH`, plus a `.cmd` twin for any PowerShell route | **Changed.** First executable in the plugin. No network and no hook, so a reviewer's question is "what does this script do" rather than "what does it fetch" |
| **B — first-run bootstrap** | 2.7 MB fetched, 17 MB venv, ~7 s cold on a fast link; rebuilt per plugin version unless routed to `${CLAUDE_PLUGIN_DATA}` | As macOS, plus distributions that ship Python without `ensurepip`, where `python -m venv` fails and the failure has to be explained rather than retried | Everything above, plus interpreter discovery: `python3` is typically absent, `python` may be the Microsoft Store stub that opens the Store instead of running | **Changed materially.** A hook running a shell command at every session start, an executable on `PATH`, and a network install of a package resolved at runtime. The screening criteria are not published, so the outcome cannot be measured here; the direction of the change is not in doubt |
| **D — `PreToolUse` install prompt** | Adds a hook process to every matching Bash call, so every `requivo` invocation pays it | As macOS | As macOS where the Bash tool exists; needs a matcher covering PowerShell too, and a second rewrite shape where it does not | **Changed most.** A plugin that intercepts shell commands and substitutes command text — the largest surface in this table, and the one furthest from "reviews trivially" |

## Claude Cowork raises the value of a yes and does not change this one

The community-marketplace form asks which surfaces a plugin supports, and Cowork runs skills in a
sandboxed Linux VM that is destroyed at session end, with only explicitly connected folders
surviving. For Requivo the two halves fall on opposite sides, and only one of them is this issue's.

**Sessions survive, and that turns out not to be a code question.** `workspace_root()` reads
`REQUIVO_WORKSPACE` and otherwise returns the current working directory, evaluated per call. So
where a session lands is decided by where the CLI is run from, and it can be pinned explicitly.
Measured all three ways on a session started in one directory and pointed at another: with no flag
it lands under the cwd, `--workspace <dir>` lands it under `<dir>`, and `REQUIVO_WORKSPACE=<dir>`
does the same. `requivo doctor --json` reports the resolved `workspace.root`, so a user can confirm
in one command which disk they are writing to. Connect the project folder, run from it or name it,
and sessions persist — no change to this repository required. (Measured on macOS; the resolution is
plain `Path.cwd()` with an environment override, so nothing in it is platform-specific.)

**The binary does not survive, and that is exactly the gap this record closes as "no".** A
per-session `pip install` inside an ephemeral VM is worse than a one-time install on a laptop, and
it is the same fix. So a yes here would have been worth more than better Claude Code onboarding: it
would have made a second surface claimable. It was not enough to buy any of the options above, and
a surface whose first step is a fresh install every session should not be ticked on a form. Cowork
stays unclaimed, which is the honest state rather than a loss.

## What would change the answer

- A plugin-manifest declaration of a Python dependency, installed by Claude Code under the same
  constraints it already applies to npm dependencies — frozen resolution, no lifecycle scripts, a
  bounded timeout. That is the shape the answer wants, and it does not exist today. **This one alone
  would be enough to reopen this**, because it is the only candidate that adds no executable and no
  hook of ours: the install would be Claude Code's, declared rather than performed.
- A single-file distribution of `requivo` with no interpreter requirement — a self-contained binary
  per platform, published beside the wheel. Then `bin/` carries a real program rather than a shim
  looking for a Python, and every interpreter-discovery row in the table above disappears. It is a
  large change to how this project ships and is out of proportion to one install step today; it
  would not be if Cowork were being claimed.
- `bin/` specified against the PowerShell tool as well as the Bash tool. Necessary but not
  sufficient — it fixes the Windows row and nothing else.
- Documented `PATH` precedence for plugin `bin` entries, so a shim's safety rests on a promise
  rather than an observation. Also necessary, also not sufficient.

Deliberately **not** on this list: a hook event that can ask the user. That already exists — see
option D — and it is refused above on its cost, not on its absence.

## Recommendation

Close #94. Ship the #93 preflight and make it good: the cause named, one command, an immediate
retry, and the statement that nothing was half-created. That is the product for as long as this
answer holds.

Submit for **Claude Code only**. Cowork is not tested and is not claimed. One question this record
could not answer and that decides how expensive "later" is: whether a submitted plugin's surface
list can be amended without resubmitting.
