# Getting started

> Install Requivo and run a first discovery on each interface. For orientation, read the
> [main README](../README.md) first.

Requivo is one engine with three interfaces over the same local session format. Sessions live in
`.requivo/sessions/<slug>/` in your workspace, and a session created by one interface opens in the
others — so nothing is locked to where you start.

**Start in the browser.** Use Claude Code or the CLI when they fit your workflow better.

## Installing

Requivo needs Python 3.9 or newer. The shortest route installs nothing at all — [uv](https://docs.astral.sh/uv/)
fetches the package into a temporary environment and runs it:

```bash
uvx --from "requivo[web,anthropic]" requivo web   # uv: curl -LsSf https://astral.sh/uv/install.sh | sh
```

That is the right form for trying Requivo or running it occasionally. To keep the `requivo` command
around, install it as a tool instead:

```bash
uv tool install "requivo[web,anthropic]"     # then: requivo web
pipx install "requivo[web,anthropic]"        # equivalent, if you already use pipx
```

Both install Requivo in its own environment and put the `requivo` command on your PATH. If you would
rather use pip directly, install into a virtualenv rather than with `pip install --user` — the latter
succeeds but frequently leaves `requivo` off the PATH:

```bash
python3 -m venv ~/.venvs/requivo && source ~/.venvs/requivo/bin/activate
pip install -U pip
pip install "requivo[web,anthropic]"         # drop [anthropic] to work offline
requivo demo
```

Drop `[anthropic]` from any of these to install without the provider SDK: the interface still opens,
reads existing sessions and replays the demo — it just cannot analyse or generate.

## Supported platforms

| Platform | Python | Tested in CI |
|---|---|---|
| Linux | 3.9 – 3.14 | every version, every push |
| macOS | 3.9 – 3.13 | 3.9 and 3.13 |
| Windows | 3.9 – 3.13 | 3.9 and 3.13 |

macOS and Windows are tested at the ends of the range, where a platform's standard library differs
(Windows 3.9 cannot resolve a dangling symlink; 3.13 can); Linux runs every version (3.14 since #298).
CI tests the `requivo` **package**, not the Claude Code plugin, which on native Windows needs
[Git for Windows](https://git-scm.com/downloads/win) (see the [plugin README](../plugins/claude-code/)).

Requivo reads and writes **UTF-8 everywhere**, whatever the locale or console codepage. A character a
console cannot show is escaped, never dropped and never fatal (`requivo doctor` reports the console's
encoding); a file you pass in that is not UTF-8 is refused by name.

## Try it with no key, no setup

`requivo demo` replays a real run from bundled output — no API key, no network. It ships inside the
package, so it works straight after any install above:

```bash
requivo demo
```

From a clone instead, with nothing installed: `uv run requivo demo`.

It ends on the step the engine exists for: one answer changes, and Requivo reports which decisions
to re-validate and which documents go stale — computed from the dependency graph, so it costs nothing.

## 1. Web — start here

A local, single-user browser workspace, and the shortest path from a request to something reviewable.
The server binds to localhost; the Anthropic key is read from the server environment and is only needed
to analyse and generate.

```bash
export ANTHROPIC_API_KEY="…"               # or put it in a .env file; optional to just read sessions
requivo web                                # opens http://127.0.0.1:8765
```

Then: paste a request on the home page → read what Requivo understood → answer the questions it raises
→ read what those answers moved → **Generate decision brief**. Come back later, change one answer, and
it will tell you what needs reviewing.

Details and security notes: [web.md](web.md).

## 2. Claude Code — an integration

Use the same sessions inside the Claude Code workflow you already have — reasoning goes through your
own Claude session, so **no Anthropic API key is needed**. The deterministic CLI validates and applies
what Claude proposes.

On **native Windows**, install [Git for Windows](https://git-scm.com/downloads/win) first. The skills
reach the CLI through Claude Code's Bash tool, and Git Bash is what provides that tool on native
Windows; under WSL nothing extra is needed. See the
[plugin README](../plugins/claude-code/) for the detail.

1. Install the plugin. In Claude Code:

   ```text
   /plugin marketplace add jbkkz/requivo
   /plugin install requivo@requivo
   /reload-plugins
   ```

   (From a checkout instead: `claude --plugin-dir ./plugins/claude-code`.)
2. Then:

   ```text
   /requivo:run     We'd like a leave approval system.
   ... answer the questions in prose; it folds each one in and stops when the session is ready ...
   /requivo:status  <slug>
   /requivo:docs    <slug>
   ```

See the [plugin README](../plugins/claude-code/) for the full skill list and workflow.

## 3. CLI — inspect, automate, script

With [uv](https://docs.astral.sh/uv/) — no virtualenv to manage. The conversation calls the Anthropic
API, so pull in the `anthropic` extra and set a key:

```bash
cp .env.example .env                       # set ANTHROPIC_API_KEY
uv run --extra anthropic requivo run examples/case1_leave.md
```

`run` is the one verb for the whole conversation: it discovers, asks, waits for your prose answers and
folds each one in, stopping when the session is ready, converged, or you say so. No argument resumes
the workspace's default session; a slug jumps straight to one.

<details><summary>Classic pip + venv install</summary>

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools   # a fresh venv may ship a pip too old for editable installs
pip install -e '.[anthropic]'   # deps + the anthropic SDK + the `requivo` command
cp .env.example .env
requivo run examples/case1_leave.md
```

</details>

Stopping early, or a provider failure part-way, keeps the turns already paid for; `requivo run <slug>`
resumes. Once it is ready:

```bash
requivo status <slug>                      # where the session stands, no network
requivo docs   <slug>                      # menu of every document the model can produce; pick one or several
```

`docs` is a loop over the same seven generators (`brief`, `prd`, `stories`, `estimate`, `criteria`,
`epic`, `release`) `requivo <type> <slug>` already reaches — regenerate any one directly once you know
which you want, or script over them individually (`--json`, `epic --export-json/--github/--gitlab`,
`requivo impact <slug> <slot>` for what a change reaches): see
[`docs/integrations.md`](integrations.md) for the automation contract, including `discover`/`answer`,
the two verbs `run` is built on.

Full reference: [cli.md](cli.md).

## Upgrading (and rolling back)

`pip install -U requivo` is safe for your sessions: an upgrade touches nothing until you write, a
newer session opens in an older Requivo, and a future `format_version` bump would refuse with a
structured error rather than corrupt. Avoid two versions writing one workspace *concurrently*. The
full promises: [compatibility.md](compatibility.md).

## Your sessions stay out of git

Sessions hold the originating request verbatim — usually a client's words — and are written to the
directory you run from, so Requivo writes `.requivo/.gitignore` (`*`) once, when it creates the store.
To share one session, `requivo session export` / `session import`. Details:
[session-format.md](session-format.md#sessions-and-git).
