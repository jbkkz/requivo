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

## Try it with no key, no setup

`requivo demo` replays a real run from bundled output — no API key, no network. It ships inside the
package, so it works straight after any install above:

```bash
requivo demo
```

From a clone instead, with nothing installed: `uv run requivo demo`.

It ends on the step the engine exists for: one answer changes, and Requivo reports which decisions
have to be re-validated and which documents go stale. That block is computed from the dependency
graph rather than reasoned, so the same change gives the same answer every time — and it costs
nothing, which is why the keyless demo is where it is shown.

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
   /requivo:brief   <slug>
   ```

See the [plugin README](../plugins/claude-code/) for the full skill list and workflow.

## 3. CLI — inspect, automate, script

With [uv](https://docs.astral.sh/uv/) — no virtualenv to manage. Discovery calls the Anthropic API, so
pull in the `anthropic` extra and set a key:

```bash
cp .env.example .env                       # set ANTHROPIC_API_KEY
uv run --extra anthropic requivo discover examples/case1_leave.md
```

<details><summary>Classic pip + venv install</summary>

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools   # a fresh venv may ship a pip too old for editable installs
pip install -e '.[anthropic]'   # deps + the anthropic SDK + the `requivo` command
cp .env.example .env
requivo discover examples/case1_leave.md
```

</details>

Discovery claims the session under `.requivo/sessions/<slug>/` before the first paid call, and nothing
you pay for is discarded after that: stopping the loop early — or a provider failure part-way through
it — saves the turns that had already run, and tells you the `requivo answer <slug> "…"` that picks up
where you left off. Every verb takes the session **slug** (`status` and `impact` also accept a path to
a saved `model.json`, since they read it directly rather than writing back into a session); regenerate
any artifact without redoing discovery:

```bash
requivo prd    <slug>                      # also: stories · estimate · criteria · release · brief
requivo epic   <slug> --export-json --github --gitlab   # + a tool-neutral epic.json and tracker plans
requivo impact <slug> permissions          # what rests on a slot
```

Full reference: [cli.md](cli.md).

## Upgrading (and rolling back)

`pip install -U requivo` is safe for your sessions. Upgrades never touch a session until you write
to it; a session written by a newer Requivo still opens in an older one (unknown fields are
preserved, unknown artifact types are tolerated); the one hard refusal is a future `format_version`
bump, which will say so in a structured error rather than corrupting anything. Avoid running two
Requivo versions against one workspace *concurrently* — the full promises, including that caveat,
live in [compatibility.md](compatibility.md).

## Your sessions stay out of git

Sessions are written to `.requivo/` in the directory you run from — your project repository, for the
Claude Code plugin. They hold the originating request **verbatim**, which for most users is a client's
own words.

Requivo writes `.requivo/.gitignore` containing `*` the first time it creates that directory, so
`git add .` picks up nothing and your own `.gitignore` is left alone. It is written once and never
restored: delete it to commit sessions deliberately and they stay committed. To share a single session
instead, use `requivo session export <slug> -o <slug>.zip` and `requivo session import <slug>.zip`.

Details in [session-format.md](session-format.md#sessions-and-git).
