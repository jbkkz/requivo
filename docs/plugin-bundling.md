# Decision: the Claude Code plugin does not bundle the CLI

> Spike [#94](https://github.com/jbkkz/requivo/issues/94), measured against Claude Code **2.1.238**
> and the plugin documentation of **2026-08-21**. Verdict: **no** — every route that removes the
> second install step breaks a constraint or costs more than the step. The preflight of
> [#93](https://github.com/jbkkz/requivo/issues/93) is the answer.

## The question

The plugin ships skills and a manifest; the `requivo` CLI they call is a separate PyPI install, so a
user meets *install plugin → install Python or uv → install requivo → fix PATH*. Can the plugin spec
collapse that, and at what cost? The constraint: a bootstrap must say what it is doing and be
refusable, and the simple answer beats the clever one.

## What the spec supports (each claim documented, observed, or both)

- A plugin-root `bin/` is on the Bash tool's `PATH` (documented and observed), added whether or not
  the directory exists, and — observed on macOS, **documented nowhere** — **last**, which is what would
  keep a shim from shadowing a real install.
- The Bash tool environment carries **no** `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA` or
  `CLAUDE_PROJECT_DIR` (observed; the reference exports them only to hooks and MCP/LSP servers).
  `${CLAUDE_PLUGIN_DATA}` is the documented home for Python dependencies; `${CLAUDE_PLUGIN_ROOT}` is
  version-scoped and ephemeral (orphans swept after ~14 days).
- `SessionStart` is the only unattended bootstrap point and has **no decision control**; stdin is not
  a TTY. A `PreToolUse` hook **can** ask (`permissionDecision: "ask"`) and rewrite the command
  (`updatedInput`). `CLAUDE_ENV_FILE` lets a `SessionStart` hook export values to later Bash calls.
- Submissions go through unpublished automated safety screening.
- On Windows without Git Bash there is **no Bash tool**, and `bin/` is specified against it only.
- The wheel is small: Python 3.9 floor, 7 wheels / 2.7 MB, a 17 MB venv, ~7 s cold. Size was never
  the problem.

## Options

- **A — a `bin/requivo` shim repairing `PATH` only**, deferring to any real install ahead of it and
  otherwise running `python -m requivo` from an interpreter that can import it. Prototyped on macOS;
  it never shadows a real install.
- **B — a `SessionStart` bootstrap** into a venv under `${CLAUDE_PLUGIN_DATA}`, exported through
  `CLAUDE_ENV_FILE` for the shim. Every mechanism exists.
- **C — vendoring the source.** Excluded: two copies of the engine.
- **D — a `PreToolUse` hook** on `Bash(requivo:*)` that finds `requivo` missing and asks to run the
  install first. Automatic, visible, refusable — and it receives `CLAUDE_PLUGIN_DATA`.

## The decision

**Nothing ships; the plugin stays inert and the second install step stays.**

1. **B cannot ask**: `SessionStart` is a silent network install by construction; made opt-in, it is
   #93's preflight with 17 MB of machinery. **D can ask, at the cost of the plugin's character**: a
   hook that intercepts shell calls and substitutes command text is the largest surface a plugin can
   ship and the one screening scrutinises, traded for one `pip install` it only moves (a rewritten
   `pip install` still needs a `pip`).
2. **On Windows the mechanism may be absent**: no Git Bash, no Bash tool, no `bin/`. (The skills'
   `allowed-tools: Bash(requivo:*)` rests on the same assumption; #121 made Git for Windows a stated
   prerequisite.)
3. **A shim cannot find its data directory** without adding a hook to a plugin whose virtue is having
   none.

A survives all three and still loses: it removes one step of four, adds the plugin's first executable,
and rests on undocumented `PATH` ordering — to fix what #93 fixes in prose.

## Cost by platform

macOS cells were run; Linux, Windows and screening are reasoned.

| | macOS | Linux | Windows | Screening |
| :--- | :--- | :--- | :--- | :--- |
| **Ship nothing (#93)** | one `pip install` | same | same; where `PATH` breaks most | unchanged: skills and a manifest, no executable, hook or network |
| **A — shim** | works; needs `python3` that imports requivo | as macOS | undefined without Git Bash; else needs a `.cmd` twin | first executable in the plugin |
| **B — bootstrap** | 2.7 MB, 17 MB venv, ~7 s; rebuilt per version unless in `PLUGIN_DATA` | plus distros without `ensurepip` | plus interpreter discovery (`python` may be the Store stub) | a hook at every session start, an executable, a runtime network install |
| **D — install prompt** | a hook process on every `requivo` call | as macOS | needs a PowerShell matcher and rewrite | changed most: rewrites shell commands |

## Claude Cowork

Cowork runs skills in an ephemeral Linux VM. Sessions survive when the project folder is connected:
`workspace_root()` is the cwd or `REQUIVO_WORKSPACE`/`--workspace`, and `requivo doctor --json` reports
`workspace.root`. The binary does not survive — a per-session install is the same gap this record
closes as "no" — so Cowork is not claimed.

## What would change the answer

- **A manifest-declared Python dependency** installed by Claude Code under its npm-style constraints
  (frozen resolution, no lifecycle scripts, bounded timeout). This alone reopens it: no executable or
  hook of ours.
- A self-contained per-platform binary beside the wheel — out of proportion today, not if Cowork were
  claimed.
- `bin/` specified for the PowerShell tool, and documented `PATH` precedence for plugin `bin` entries —
  each necessary, neither sufficient.

## Recommendation

Close #94 and make the #93 preflight good: the cause named, one command, an immediate retry, and the
statement that nothing was half-created. Submit for **Claude Code only**. Open question: whether a
submitted plugin's surface list can be amended without resubmitting.
