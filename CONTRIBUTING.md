# Contributing to Requivo

Thanks for your interest. Requivo is a solo-maintained open-source project. Contributions,
issues and real-world feedback are all welcome — the feedback we most want is *did the engine ask the
questions a good PM/BA would ask?* (see the **Real-world discovery feedback** issue template).

Before a large change, please open an issue to discuss it first — it saves everyone a wasted PR.

## Project layout in one line

Requivo is one engine behind three interfaces (CLI, Claude Code plugin, local Web). The layers form a
strict DAG: `core/` (no LLM, no provider, no argv/stdout — reading and writing files *is* core's
job) → `providers/` (the only LLM callers) → `services/` (the single validated apply path) →
`render/` + `cli.py` + `web/`. The full map is in
[docs/architecture.md](docs/architecture.md), and the distribution boundary is in
[docs/open-source-strategy.md](docs/open-source-strategy.md). Read those before a structural change.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools        # a fresh venv often ships pip too old for editable installs
pip install -e ".[dev]"              # deps + the `requivo` command + pytest + ruff
```

`uv run requivo …` also works without managing a venv.

## The `.claude/` directory is maintainer tooling — you need none of it

This repository tracks a `.claude/` directory: an empty `settings.json`, and a `jit-context/` rule
layer beneath it. One of the rules
(`jit-context/tools/01-oss/supertool-required.md`) declares `mode: block` over `Read`, `Edit`,
`Write`, `Glob` and `Grep`, with a `match:` of everything, and names a `supertool` command as the
replacement. Read cold, that looks like a repository which refuses to let you open a file unless you
have a tool you have never heard of.

It is not, and the reason is mechanical. A jit-context rule is **data**. The only thing that reads it
is a `PreToolUse` hook, and that hook ships inside the `claude-jit-context` plugin, registered from
that plugin's own manifest. This repository registers no hooks of its own, and it goes further than
that: `.claude/settings.json` is tracked as an empty JSON object. No plugin enablement, no key naming
a command, and no hook script tracked anywhere under `.claude/`. Without that plugin installed there
is no hook, nothing reads the layer, and every file operation behaves exactly as it normally does.
`tests/test_agent_layer.py` is the guard that keeps that true, so it cannot quietly stop being true.

The file is empty rather than merely hook-free because it did stop being true once. A `statusLine`
command pointing at a maintainer script *outside* `.claude/` sat in the tracked settings from #186
until #215, executing on the machine of anyone who cloned `main` in between, while the guard — which
read the `hooks` key and only the `hooks` key — stayed green. No tagged release carries it; the
plugin enablement beside it shipped in every release from 0.10.0 to 1.2.0. Both live in
`.claude/settings.local.json` now, which `.gitignore` excludes. If a key ever returns to the tracked
file it has to be added to the allowlist in `tests/test_agent_layer.py` and described here, in the
same change; the test fails otherwise.

Three more tracked files belong to the same maintainer loop and are inert for a contributor in the
sense that matters — you never run any of them: `.oss.json` and `.oss/` configure the `oss` plugin
that runs this repository's maintenance (see [.oss/README.md](.oss/README.md)), and `.supertool.json`
configures its shell tooling. Nothing in the build or the product reads them.

One test does, and it is worth knowing before it goes red on you: `tests/test_version_sites.py`
cross-checks `.oss.json`'s `version_sites` list against the files it can actually find declaring a
version, in both directions. So adding a new file that declares the project version means adding it
to that list, and it is the one place a contributor has a reason to edit a maintainer-loop file.

So: **contributing needs Python, git and the setup above — nothing else.** If the directory bothers
you, delete it in your working copy; just do not commit the deletion. A fresh git worktree takes its
rule layer from git, and this repository's maintenance loop cuts one worktree per issue, which is why
the layer is tracked here rather than kept in a personal config.

## The checks a PR must pass

```bash
.venv/bin/python -m pytest tests/ -q         # pure-logic + offline CLI units (no API calls)
.venv/bin/ruff check src tests scripts        # lint (same invocation as CI)
.venv/bin/pyright                              # types (same invocation as CI; config is in pyproject.toml)
python -m build --wheel                        # the wheel must still build (needs `pip install build`)
```

CI runs the tests and `ruff check` on Python 3.9–3.14, runs `pyright`, and builds the wheel (then
imports it and builds every prompt from the installed package). It runs more legs than these four —
the platform matrix, a dependency-floor install, secret scanning, plugin-manifest validation and the
changelog gate — but these four are the ones that reproduce locally in seconds. Please run them
first. The project lints with ruff but does **not** enforce `ruff format` — match the surrounding
style rather than reformatting.

### Coverage is measured and never gated

```bash
.venv/bin/python -m pytest tests/ -q --cov=requivo --cov-report=term-missing
```

Deliberately not in the list above: **there is no coverage threshold and no coverage check**, and a
pull request cannot fail on this number. `Test (py3.12)` prints the same table into its log through
one `continue-on-error` step, so the report is there to read on every pull request without ever being
something to pass. Adding a `fail_under` would turn it into a percentage to pad, and a padded number
that has cleared a check is worse than no number at all.

Read it for what it says about *your* change: a module whose new code lands entirely in the
`Missing` column is worth a second look. It is not a target to raise. Some uncovered lines are
correctly uncovered — `render_stale`'s empty-input early return has no caller in the tree that can
reach it, and a test for it would assert the renderer's own contract rather than any behaviour a user
gets. Say so in the pull request instead of writing the test.

**The table cannot see inside a process pytest spawned.** Code this suite exercises only by running
the real CLI in a subprocess — `tests/test_encoding.py` drives `python -m requivo` that way — is
reported as `Missing` even though a test runs it on every leg, every time. `src/requivo/__main__.py`'s
`app()` line is the standing example: it is covered by the encoding suite and the table says it is
not. So check *how* a line is reached before reading its absence as a gap. `pyproject.toml`'s
`[tool.coverage.run]` block records what enabling subprocess tracing was measured to cost, and why it
is not on.

### What the changelog gate does not cover

The changelog gate (`.github/workflows/oss-changelog.yml`, which requires a `changelog.d/` fragment)
triggers on **`pull_request` only**. Direct pushes to `main` are never checked by it.

That matters here because a direct push to `main` is not impossible, only rare. Every change now
lands as a squash-merged pull request, with one deliberate exception: the `chore(release)` commit
that cuts a version goes straight to `main`. So the uncovered class is small and known rather than
hypothetical — and a release commit is precisely the one whose changelog entry a reader is most
likely to go looking for. (A count is deliberately not quoted here — it changes with the next
release, and a stale number is its own small version of this same problem.)

So **a green board means the changelog gate passed on the commits it was shown**, not that every
change in the release carries a fragment. Those are different claims, and nothing on the board
distinguishes them.

This limit is stated rather than closed, deliberately. A `push:` trigger on `main` would go red
*after* the fact — a fragment cannot be added retroactively to a commit already pushed — installing a
permanently red default branch, which is a worse lie than the one it fixes. If you push directly to
`main`, add the fragment in that same commit; nothing will remind you.

### The guards that read your source and your prose

A share of this suite does not test the product at all: it tests the repository's *form* — an import,
an encoding declaration, a version string, a comment that names a test, a heading a test parses. Each
one is incident-backed and each one states its reason in its own file. What follows is only the map:
what trips it, and where the fix goes. A small PR can trip several, and none of them needs you to
read the guard to appease it.

| If your change… | This goes red | The fix |
|---|---|---|
| reads or writes a text file anywhere in `src/`, `scripts/` or `tests/` | `test_encoding.py` | name the codec: `encoding="utf-8"`. A deliberate locale-default read is exempted **by name, with a reason**, inside the guard |
| prints a character a console may not encode | `test_encoding.py` | route the entry point through `streams.py` / `configure_output()`; never add a bare `print` in a new harness script |
| imports a provider from `core/`, or any new provider name from `cli.py`, `render/`, `deterministic/` or `web/` | `test_boundaries.py` | `core/` may never import one. A surface import needs an allowlist entry keyed by **(file, name)** carrying its reason — and if the concept is not the vendor's, move it out of `providers/` instead |
| calls `core.persistence` directly from a surface | `test_boundaries.py` | use `SessionRepository`, or add a **(file, function)** allowlist entry saying why no backing-neutral form exists |
| renames or deletes a test | `test_narrative_references.py` | **grep for the old name first** — see the coupling below |
| adds a decision record under `docs/decisions/` | `test_narrative_references.py` | give it a `**Slug:**` line, and leave a `` `decision: <slug>` `` pointer, on one line, at whatever the record explains |
| adds a file that declares the project version | `test_version_sites.py` | make it agree with the others, and register the path in `.oss.json`'s `version_sites` |
| edits a heading in `docs/compatibility.md` | `test_cli_flag_names.py`, `test_cli_degraded_listing.py` | the page is parsed as data — see the coupling below |
| adds a `--json` output, or changes a payload's shape | `test_cli_flag_names.py`, `test_public_payload_shapes.py` | add the row to `docs/compatibility.md`'s promise table and update the payload pin in the same change |
| edits a prompt's `# Output format` example | `test_prompt_contracts.py` | the example must validate against the contract that operation actually parses replies with |
| changes a CLI verb or flag the plugin invokes | `test_plugin.py`, `test_plugin_cli_drift.py` | update the skill that invokes it; the drift guard compares the plugin against a *released* Requivo, not this checkout |
| adds a key to the tracked `.claude/settings.json` | `test_agent_layer.py` | allowlist it **and** describe it in this file, in the same change |
| edits anything under `.github/workflows/` | `test_workflow_permissions.py`, `test_workflow_untrusted_output.py` | state the `permissions:` block, and never interpolate third-party output into a line that starts at column 0 |
| adds or moves a runtime dependency bound | `test_dependency_floor.py` | the floor set must stay complete — a dependency that drops out makes the floor leg report a test it never ran |
| adds a slot to `framework/model_schema.json` | `test_dependencies.py` | add it to `_ARTIFACT_SLOTS_RAW`, or name it in `_SLOTS_WITH_NO_SPECIFIC_ARTIFACT` with a reason |
| renames the user-facing caption for `brief` in an asset | `test_vocabulary_boundary.py` | an asset keeping the older wording is a declared exception, never an accident |

**Two of these run in the direction nobody predicts. They are the ones worth reading twice.**

- **A test's *name* is load-bearing API for source prose.** This repository answers "why is this line
  here?" by naming the test that enforces it, in `src/`, in `scripts/`, in `docs/` and in `CLAUDE.md`
  — dozens of such references. Renaming or deleting a test therefore breaks documentation, and
  `test_narrative_references.py` goes red naming the file that now points at nothing. *The fix:*
  before you rename, `grep -r <the_old_test_name> src scripts docs tests CLAUDE.md` and update every
  hit in the same commit. The same guard also fails if an identifier is **split by a line wrap**, so
  keep a reference on one line — a name you cannot grep for is a name nobody can follow.
- **`docs/compatibility.md` is parsed as data, not read as prose.** Two tests locate a section by its
  exact heading string and one matches a row of the exit-code table by regex, so an innocent heading
  edit breaks the build in a file that looks like documentation. *The fix:* if you must rename a
  heading, update the literal in the same change — the failure message names which one, and each
  assertion carries the promise it is protecting so you can tell a rename from a real removal.

  That coupling is mechanical and this table's rows are only about the mechanics. **Whether a
  given change is breaking or compatible turns on direction, not on judgement** — the CLI
  exit-code promise now says so explicitly (#382): moving a condition onto 0 is compatible, moving
  one onto or between nonzero codes is breaking. The sibling `RequivoError.code` promise still
  reads as "moving a condition from one code to another is breaking" with no direction clause of
  its own, so treat a move there the same way and say which direction it is in the PR either way;
  do not read a green suite as the page having agreed with you.

## Conventions

- **English everywhere** — code, comments, docs, prompts, context cards, and the engine's own output
  are all in English. (Chat/issues can be in any language.)
- **Match the surrounding style** — the codebase uses deliberate alignment and compact imports; ruff
  is configured for that (`E501` off, `split-on-trailing-comma` off). Don't reformat unrelated code.
- **Python 3.9 floor** — Pydantic model fields must use `Optional[X]`, not `X | None` (that raises at
  class-definition time on 3.9). `UP045` is disabled for this reason.
- **Tests are required** for logic changes. The test suite must run with **no network / no API key**
  — reasoning-dependent code is tested through an injected fake client, never a live call.
- **Behaviour is tuned in the assets, not the Python.** Prompt and context-card changes must be
  measured through the golden harness (`scripts/golden_run.py` → `scripts/golden_diff.py`); a card
  that helps one request can quietly cost a neighbour. Commit an updated baseline only when the change
  is intended.
- **Keep the output contract in sync.** Each stage's Pydantic contract must agree with its prompt's
  "Output format" block, and slot ids must stay in `framework/model_schema.json`.
- **Session-format compatibility.** The on-disk session format is a product surface. If a change
  alters it, say so explicitly in the PR and describe the migration path — don't break existing
  saved models silently.
- **Documentation** — update the README / CLAUDE.md / relevant docs when you change behaviour, a
  command, or the architecture.

## Adding a context card or an example

- **Context card:** copy `src/requivo/assets/context/_template.md` to `…/context/<name>.md`. It is
  picked up automatically (any non-`_`-prefixed file). Cards must be **generic** — no client name, no
  real request, no confidential business rule. Company-specific cards belong in your own private
  `REQUIVO_CONTEXT_DIR`, never in the repo.
- **Example:** see [examples/README.md](examples/README.md). Public examples must be **synthetic or
  properly anonymised** — no client names, emails, identifiers, or confidential data.

## Do not commit

- **Secrets** — API keys, tokens, passwords, `.env` files. `.env` is gitignored; keep it that way.
- **Real, non-anonymised customer requests or data** — see the data boundary in
  [docs/open-source-strategy.md](docs/open-source-strategy.md#data-what-may-be-public-what-stays-private).
- **Local sessions / generated output** — `.requivo/`, `out/` and `demo-out/` are gitignored.

If you accidentally commit a secret, tell the maintainer so the credential can be **revoked** — a key
that has touched Git history must be considered compromised even after removal.

## Licensing of contributions

By submitting a contribution, you agree that your contribution will be licensed under the same
**Apache License 2.0** that covers the project.

There is no Contributor License Agreement (CLA) or Developer Certificate of Origin (DCO) sign-off in
force today. A lightweight DCO or CLA may be introduced before accepting large external contributions;
if that happens it will be documented here first. Contributing now does not assign any additional
rights beyond the Apache-2.0 terms above.

## Trademark

The Apache-2.0 license covers the *code*. Section 6 of that license is explicit that it grants no
trademark rights, and this project relies on that rather than adding a term of its own: it does not
grant rights to the **Requivo name, logo, or identity** — see [TRADEMARKS.md](TRADEMARKS.md). Forks
are welcome and may say they are "based on Requivo"; a substantially modified fork should use a
distinct name.
