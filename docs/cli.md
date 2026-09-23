# CLI reference

> Every `requivo` command. For a first run, see [getting-started.md](getting-started.md).

Run as `requivo <command>` after an install, `uv run requivo <command>` with uv, or
`python scripts/requivo_cli.py <command>` from a bare clone. Commands that call the Anthropic API need
the `anthropic` extra and `ANTHROPIC_API_KEY`; everything else is offline.

Verbs take a session **slug**. `run`, `status` and `impact` accept it as an *optional* positional
(#541): omit it and the CLI resolves the workspace's default session — the only one, or the most
recently written when there are several, with every candidate listed and the default marked before
anything paid happens. `session`, `model` and `artifact` keep the slug required, because a script must
never act on "whichever session is newest". `status` and `impact` also accept a path to a saved
`model.json`, so they can read a model that is not in a session store; every other verb resolves a
session, because it writes a revision or an artifact back into one.

**This page is a reference.** Every table below is read against `--help` by
`tests/test_cli_flag_names.py`, so a flag documented here is one the parser binds, and a flag the
parser binds cannot ship undocumented. Why a check exists is on the issue it cites.

## Global flags, and reading `--help`

| Flag | Does |
|---|---|
| `requivo --version` | Print `requivo <version>` and exit 0. Read from the package, so it is the version you actually have |
| `requivo --workspace DIR <command>` | Where sessions are read and written (default: cwd). Accepted *before or after* the command |

`requivo --help` groups the verbs into three tiers (#546, `decision: three-journey-verbs`):

- **Start here** — `demo`, `run`, `docs`, `status`, `web` — the first screen, each with its own
  help line and the `(API)` marker where it applies.
- **For scripts and integrations** — `discover`, `answer`, `brief`, `prd`, `stories`, `estimate`,
  `criteria`, `epic`, `release`, `impact` — the automation contract this page documents, listed by
  name only; `requivo <verb> --help` still shows each one's own usage.
- **Plumbing** — `doctor`, `schema`, `context`, `session`, `model`, `artifact`, `api` — session/
  model/artifact CRUD and install diagnostics, also by name only.

`(API)` marks a verb that spends money on your own key; everything without it is offline and free,
including `status` and `impact`, which take a slug exactly like `brief` does (or resolve the default
session, #541) and cost nothing. What the marked verbs cost is in
[providers.md](providers.md#what-a-run-costs).

## When a session cannot be found

Every route to it says the same three things: the reference it was given, **the sessions root it
searched**, and `requivo session list`.

```
no session named leave-aproval under /home/you/project/.requivo/sessions. `requivo session list`
shows the sessions in this workspace; a different --workspace (or REQUIVO_WORKSPACE) changes where
Requivo looks.
```

The usual cause is a different working directory rather than a typo: sessions live under the
workspace you run from. The `--json` envelope is `session_not_found`, with the reference in `details`.

## Discovery and refinement

`run` is the one verb a person types for the whole conversation; `discover`/`answer`/`impact` stay
as the automation contract underneath it (`decision: three-journey-verbs`).

| Command | Does |
|---|---|
| `requivo run [request\|file\|-\|slug]` | The one verb over the conversation (#540): no argument resumes the workspace's default session, or prompts for a request when none exists; a request/file/`-` is `requivo discover`, unchanged; an existing session's slug resumes it through the *answer* path, never a second discovery (interactive; `--once` for a single pass on a new discovery, `--context a,b`/`--cards` to scope cards on a new discovery — both **refused** when resuming, since a resume reuses the session's own cards and has no single-pass shape of its own) |
| `requivo discover <request\|file\|->` | Analyse a request and create a session (interactive; `-` reads the request from stdin, `--once` for a single pass, `--context a,b` to scope cards, `--perimeter ID` to choose the decision structure — default `software`, frozen at creation) |
| `requivo answer <slug> "<answers>"` | Fold answers in and refine the model one more turn |
| `requivo status [slug]` | Understanding checklist + readiness, closing with the single next command (`--json` for a machine snapshot, with no pointer). Omit the slug to resolve the workspace's default session (#541). No network |
| `requivo impact [slug] [slots…]` | What rests on given slots — decisions to re-validate + artifacts that go stale (no slots = full map), then the decisions derived from thinner evidence than the session now holds. Omit the slug to resolve the workspace's default session (#541). No network |

The context-card selector is spelled **`--context`** everywhere — on `run`, on `discover`, on
`session init`, on `session rescope` and on `context`. `--cards` is a permanent alias of it on all
five, kept because `context` spelled it that way first (#85); the two are one option, so they can
never mean different things.

When `run` or `docs` cannot determine whether a named session exists, it reports the storage error
and stops before a provider call. It does not reinterpret an unreadable session as a new request
or a document type for another session.

A selector — `--context a,b`, or the slot names given to `impact` — is checked rather than best-guessed.
An **empty** name (what an unset shell variable expands to) is refused. A slot name that matches
nothing is listed as unmatched and the rest still resolve; an unknown *card* is a hard error, since
dropping it would silently load every card. Pass no selector to select everything deliberately. See
[context-cards.md](context-cards.md#scoping-a-session-to-relevant-cards).

### Decisions derived from thinner evidence (#493)

`impact` closes, in both forms, with the decisions **derived from thinner evidence than exists
now**: at least one slot a decision was derived from was `empty` or `inferred` at the revision it
was first recorded, and is `explicit` now. The wording is *worth re-reading*, never *contradicted* —
whether the evidence disagrees is a judgment that costs a call; this is a free comparison of two
confidence values. The list is not narrowed to the slots you named.

Three outcomes: the decisions worth re-reading, with the revision they were derived at and the topics
that thickened (or a line saying how many were checked and none qualifies); *Could not check*, with
the reason (a revision an older Requivo wrote without confidence data, a decision naming no slots);
and *not reviewed* for a bare `model.json`, which has no history. Two limits: a **reworded decision
counts as newly derived at its rewording** (the derivation revision is the earliest carrying its
content-derived id), and **challenges are not reviewed** — they contest a premise rather than rest
on evidence.

**`status` ends by naming one next command**, never a menu: open questions win over a stale
artifact (regenerating against a model about to move is a paid call thrown away), a stale artifact
over a missing brief. A converged session with a fresh brief gets no pointer. `--json` never carries it.

**The slug is derived from the request**, and it drops function words and folds accents, so
*"We need a way to track vendor invoices"* becomes `track-vendor-invoices` rather than
`we-need-a-way-to`. Pass `--slug` to `session init` for an explicit one. A request in a script the
ASCII fold cannot romanize — Japanese, Cyrillic — still lands on `discovery`, and the second such
session on `discovery-<hash>`; that is a documented limit, not a bug, and an explicit slug is the way
past it. Sessions already on disk keep the names they were created with; see
[compatibility.md](compatibility.md#what-is-explicitly-not-stable) for what changes about re-running
`discover` on a request first analysed by an older Requivo.

**`discover` claims its session before it reasons**, interactive or `--once`:

- Re-running `discover` on a session that already carries a model is refused (`revision_conflict`)
  **before any API call** — it would replace that work rather than refine it. Use
  `requivo answer <slug> "…"`, or a different slug.
- **Nothing paid for is discarded.** Stopping early (`q`, an empty answer, Ctrl-C) or a provider
  failure part-way keeps the turns that ran — revision 1 with its questions open, as `--once`
  leaves it — and names the `requivo answer` that continues. Only a failure on the *first* turn
  leaves revision 0, where `discover` is still the right retry.

## Artifact generators (provider-backed)

Each is a view of the saved model: `requivo <verb> <slug>`.

| Command | Produces |
|---|---|
| `requivo brief <slug>` | The decision brief — what to review before estimating |
| `requivo prd <slug>` | Product Requirements Document |
| `requivo stories <slug>` | User stories (`stories.md`) |
| `requivo criteria <slug>` | Given/When/Then acceptance criteria |
| `requivo estimate <slug>` | Uncertainty-aware estimate (`estimate.md`). Derives the stories first and saves them too, as `stories.md`, against the same revision — the estimate is reasoned against those stories, so they are half of its provenance |
| `requivo epic <slug> [--export-json] [--github] [--gitlab]` | Delivery epic + optional tracker issue plans and a tool-neutral `epic.json` |
| `requivo release <slug> [version]` | Client-facing release notes |

Every row saves its document under `<session>/artifacts/` and records it in `session.json` with the
revision it was generated from, so `requivo status` can flag it stale when the model moves past what
it rests on (`decision: the-estimate-graduates` for `stories` and `estimate`, saved since #519).

`--export-json`/`--github`/`--gitlab` write versioned envelopes outside `ArtifactService` (no
staleness row of their own) that stamp `source_revision` and `slug` — the basis they were rendered
from, never a freshness verdict. That verdict is `requivo status --json`'s `artifacts.epic.stale`;
comparing revision numbers directly is the anti-pattern the staleness model exists to replace. The
full envelope shape, per-version skeleton and a worked consumer flow live in
[integrations.md](integrations.md#the-epic-export-envelope-requivo-epic).

## `docs` — a menu over the seven generators (#544)

| Command | Does |
|---|---|
| `requivo docs [slug] [type...] [--all]` | No type: print the seven-row menu (label, one-line purpose, state) and prompt a pick — numbers, names, or `all`. A type (or several): generate them, no prompt — the scriptable form, `requivo docs my-slug prd criteria`. `--all`: every document, skipping the menu |

Each row's state — *not generated* / *up to date (rev N, filename)* / *needs updating (from rev N,
filename)* — comes from `ArtifactStatus.stale`, never from comparing revision numbers (invariant 1).
A session at revision 0 gets no menu; it is pointed at `requivo run` instead. Every generation is a
call to the same verb body `requivo <type> <slug>` already uses — `docs` is a loop, never a second
generation path — so `requivo docs <slug> prd` and `requivo prd <slug>` write the identical file
under the identical provenance. Picking `stories` and `estimate` together writes the stories once:
`estimate`'s own two-call branch of `generate()` already reasons and saves both against one revision
(invariant 6), so `docs` drops the separate `stories` write rather than repeating it.

`docs` grows no tracker flags of its own — `epic --export-json/--github/--gitlab` stays on `epic`,
the n8n contract in [integrations.md](integrations.md).

## Local browser interface

| Command | Does |
|---|---|
| `requivo web [--host --port --workspace --no-open --reload]` | Launch the local Web interface (needs the `[web]` extra). Binds to `127.0.0.1` by default. See [web.md](web.md) |

## Local HTTP API (experimental)

| Command | Does |
|---|---|
| `requivo api serve [--host --port --workspace]` | Serve the local REST API (needs the `[api]` extra; `pip install 'requivo[api]'`). Binds to `127.0.0.1:8767` by default, with the OpenAPI docs at `/docs`. **Bound to anything but loopback it refuses to start unless `REQUIVO_API_TOKEN` is set**; with that variable set, every route under `/api/v1` except `/api/v1/health` requires `Authorization: Bearer <token>` (401 `unauthorized` otherwise), whatever the bind. The surface is experimental -- paths and shapes may still change; the design and the freeze conditions are `decision: the-http-api-facade` |

## Offline / deterministic verbs (no LLM, no key)

| Command | Does |
|---|---|
| `requivo demo` | Replay a bundled run — no key, no network |
| `requivo doctor [--json]` | Environment + install check (see [What `doctor` answers](#what-doctor-answers)) |
| `requivo schema [--framework] [--perimeter ID]` | Print the slot schema (the model vocabulary + driver rule); `--framework` also prints the human elicitation spec; `--perimeter` selects which installed perimeter (default `software`) |
| `requivo context [--list] [--context/--cards CARDS] [--session SLUG]` | Inspect available context cards. `--list` prints the stems only; `--session <slug>` scopes to exactly the cards that session uses |

The deterministic verbs and `--json` outputs are what the Claude Code plugin drives — Claude reasons,
these apply.

### `session` — session lifecycle

| Command | Flags | Does |
|---|---|---|
| `requivo session init <request\|file\|->` | `--slug`, `--context`/`--cards`, `--provider`, `--json` | Create a session from a request (no LLM). `--slug` sets an explicit slug instead of one derived from the request; `--provider` is an informational tag (e.g. `claude-code`) recorded on the session |
| `requivo session list` | `--json` | List canonical sessions |
| `requivo session show <session>` | `--json` | Show a session's metadata + artifacts |
| `requivo session migrate` | `--json` | Migrate ALL legacy `out/` sessions into `.requivo/sessions/` |
| `requivo session export <session>` | `-o`/`--output`, `--json` | Export a session as a `.zip` archive; `--output` sets the destination path |
| `requivo session verify <session>` | `--json` | Check that a session's files agree with each other |
| `requivo session restore <session>` | `--revision N` | Copy a readable `revisions/NNNN-model.json` over `model.json` — the recovery path for a torn or inconsistent session; see [Recovering a torn or inconsistent session](#recovering-a-torn-or-inconsistent-session). Defaults to the newest revision this build can read |
| `requivo session rescope <session>` | `--context`/`--cards` (required), `--json` | Re-scope an existing session's context cards — see [context-cards.md](context-cards.md#re-scoping-an-existing-sessions-cards) |
| `requivo session import <archive>` | `--force`, `--json` | Import a session archive into the workspace; `--force` replaces a session of the same slug — see [Importing a session](#importing-a-session) |
| `requivo session delete <session>` | `--json` | Irreversibly remove a session — its directory and its `.requivo/locks/<slug>.lock` file. Refuses a nonexistent slug with `session_not_found`. No trash, no undo — `session export` first if you might want it again |

### `model` — inspect and mutate the model through the validated path

| Command | Flags | Does |
|---|---|---|
| `requivo model show <session>` | `--json` | Print a session's current model |
| `requivo model validate <proposal\|->` | `--allow-partial`, `--json` | Validate a proposal file, no session write — `--allow-partial` checks a partial projection instead of requiring the complete slot set |
| `requivo model apply <session> <proposal\|->` | `--expected-revision N`, `--json` | Validate a proposal and apply it as a new revision — `--expected-revision` is the optimistic-locking check, see [What `model apply` takes](#what-model-apply-takes) |
| `requivo model diff <session> <proposal\|->` | `--json` | Show what a proposal would change, no write |

### `artifact` — record and read generated artifacts

| Command | Flags | Does |
|---|---|---|
| `requivo artifact save <session>` | `--type` (required), `--file` (required), `--revision N` (required), `--json` | Record an artifact; `--revision` is the revision the content was reasoned from — the one fact only the caller holds |
| `requivo artifact list <session>` | `--json` | List a session's artifacts + freshness — see "What `artifact list --json` answers" below |
| `requivo artifact show <session>` | `--type` (required) | Print a saved artifact's content |

### What `doctor` answers

Every `doctor` check has three answers: it passed, it failed, or **it could not be made** — a check
that reports "nothing found" when it could not look is worse than none.

| `--json` field | Reads |
|---|---|
| `requivo_version` / `python_version` / `os` | The three facts a bug report needs, printed as the first rows of the human view so a paste of them is a bug report |
| `model.name` / `model.source` | The model this install will reason with, and whether it came from an environment override (`env` — `REQUIVO_MODEL`, or the deprecated bare `MODEL`) or the built-in `default` |
| `schema.ok` / `schema.slots` / `schema.error` | The legacy software-perimeter schema result; `slots` remains `0` on failure |
| `perimeters.schemas` | Each installed perimeter's schema result (`ok`, `slots`, `error`), also shown as a named row in the human view. A failed load has `slots: null` and its own error; other rows still report. A successful load reports the observed slot count, not validation against an expected count |
| `context.status` | `ok`, `empty` (the install has no context cards) or `unreadable` (a card directory exists but could not be enumerated — permissions, usually). `context.ok` is true only for `ok` |
| `context_cards` | The card names themselves — the plain list it has always been |
| `sessions.readable` / `sessions.total` / `sessions.error` | Whether the session directory could be listed at all. When it could not, `total` is `null` rather than `0`, because *no sessions* and *we could not look* are different answers and a user told the first concludes their sessions were deleted |
| `sessions.inconsistent` | `{slug: [integrity codes]}` — run `session verify <slug>` on each |
| `sessions.notes` | `{slug: [integrity codes]}` for findings that are **not** defects (#260) — today, an artifact type this build has no generator for, which [compatibility.md](compatibility.md) permits without a `format_version` bump. Reported so a type nobody can see is not a type nobody upgrades for; kept out of `inconsistent` so it moves neither the glyph nor any consumer's verdict |
| `sessions.unresolved_cards` | `{slug: error}` for a session whose saved context cards no longer resolve here |
| `sessions.cards_checked` | False when the card directory itself was unreadable, so `unresolved_cards` being empty means nothing |
| `sessions.non_sessions` | What is under the session root and is **not** a session — see [Something here that is not a session](#something-here-that-is-not-a-session). `null`, not `[]`, when the root could not be listed |
| `sessions.unexaminable` | Names under the session root that could **not be examined**, so whether they are sessions is unknown — `name` and `error` per entry. Not folded into `non_sessions`, which states a fact, nor into `total`, which stays what could be confirmed. `null`, not `[]`, when the root could not be listed. See [below](#something-here-that-could-not-be-examined) |
| `locks.readable` / `locks.total` / `locks.error` | Whether `.requivo/locks/` could be listed at all, and how many `<slug>.lock` files it holds (#180) |
| `locks.sessions_checked` / `locks.unmatched` | Which of those slugs currently name no session — candidate residue from a session removed by hand (`rm -rf`, bypassing `session delete`) or by an older Requivo with no delete verb; `session delete` itself unlinks its own lock file as its last step, so an ordinary delete leaves nothing here. `unmatched` is `null`, not `[]`, when the *current session list* itself could not be read, on the same reasoning as `sessions.cards_checked` |
| `locks.unexpected` | Names under `.requivo/locks/` that are neither a `<slug>.lock` file `session_lock` could have written nor a `<slug>.discovering` guard file a first discovery could have left (#391) — a stray file, a directory, a symlink, a stem neither writer could have been given. A reserved-name stem such as `nul` or `con` is recognised by shape alone (#372, #401, #409) whether or not a session by that name currently exists on disk — `locks.unmatched` is the separate question of whether one does. `null`, not `[]`, when the lock root could not be listed at all |
| `locks.unexaminable` | Entries under `.requivo/locks/` whose examination raised — `name` and `error` per entry, on the same terms as `sessions.unexaminable`. `null`, not `[]`, when the lock root could not be listed |
| `output.streams[].state` | `safe` (a character the console cannot encode is escaped visibly, never fatal), `lossy` (it cannot crash but drops or blanks the character with no mark — only reachable by setting `errors=replace`/`ignore` yourself), `will_crash` (a strict handler on a narrow codec, so a glyph would kill the command mid-report) or `unknown` (the stream does not expose a codec, so this check could not look) |

An `empty` context is a broken install: impact is estimated against the cards, so discovery would
still run and ask duller questions, for a reason nothing on screen would name.

### What `artifact list --json` answers

```json
{"slug": "leave-approval",
 "artifacts": {"prd": {"revision": 3, "filename": "prd.md",
                       "updated_at": "2026-08-20T14:25:28Z", "stale": false}}}
```

The rows live under `artifacts`, keyed by type, and `stale` is the dependency graph's verdict — not
a comparison of `revision` against the session's current one, which is provenance (see
[dependencies and staleness](requirements-model.md#dependencies-and-staleness)).

`slug` is the name you asked under, not the one stored inside `session.json` (a stored value is
untrusted on every read). A session with nothing saved answers `{"slug": …, "artifacts": {}}`. The
payload was the bare inner map before #107; see [compatibility.md](compatibility.md).

### What `model apply` takes

A proposal replaces the model, so it carries the **complete** slot set and a non-empty
`summary.objective`. The five reasoning collections are the exception, and they are tri-state: leave
`decisions`, `challenges`, `opportunities`, `exclusions` or `thresholds` out and the established ones
stand; send `[]` and they are deleted (and what rested on them goes stale); send a list and it
replaces. A refinement normally says nothing about them. To check a partial projection without
applying it, use `model validate --allow-partial`. See
[compatibility.md](compatibility.md#what-a-proposal-means).

### Exit codes, and what 3 and 4 mean

Requivo reads and writes UTF-8 throughout, whatever the machine's locale. A file *you* name —
`requivo discover ./brief.md`, `requivo model apply <slug> proposal.json` — must be UTF-8 too; one
that is not is refused by name, with the offending byte and its position, rather than decoded with
the locale's codec into something that would look like prose and be wrong.

On output, a console that cannot represent a character gets a visible backslash escape in its place,
rather than a crash or a silent hole — `backslashreplace`, deliberately not `replace`, because a
reader cannot tell a substituted question mark from a character that was never there. Where even that
is impossible — a stream Requivo could not reconfigure, which
`doctor` names — the command exits **3** instead of dying in a traceback:

| Exit | Means |
|---|---|
| 0 | Success |
| 1 | A clean, expected failure — an invalid proposal, a missing session, a provider error |
| 2 | Bad arguments (argparse) |
| 3 | **The command's work finished and its output could not be encoded.** The message says whether a provider call was billed |
| 4 | **The work was done and part of the answer was unreachable.** What was produced is on stdout in full |
| 130 | **The operator interrupted the run** (Ctrl-C / SIGINT). Distinct from 1 so a script can tell a refusal from an interruption; on `discover`, the message names the claimed session and the continuation verb whenever one was claimed |

Three exists because 1 would invite a re-run that pays twice: `requivo brief` applies the revision
and writes the artifact *before* it prints. The message says a call **was** billed only when the
run's usage ledger shows one.

Four describes a **shape of answer rather than a verb**. Two commands reach it today.

`requivo session list` lists every session it can and gives one it could not read its own row:

```
Sessions under /work/.requivo/sessions:
  leave-approval                           rev 3  (anthropic, 2026-08-19T09:04:11Z)
  event-checkin                            could not be read — session format v2 is newer than this Requivo understands (v1) — upgrade requivo.

1 entry could not be read. `requivo session verify <slug>` reports what is wrong in full.
```

The footer counts **entries**, not sessions (a row may be an entry nobody could examine). The
degraded row **states nothing it could not read**. A session at **revision 0** is a normal row: it
simply has not been analysed.

`requivo session verify` reaches 4 from the other side, and answers three different things:

| What happened | Exit |
|---|---|
| The session is internally inconsistent — a complete answer | 1 |
| Its product context was read and does not resolve — also complete | 1 |
| Its product context **could not be read at all** — not an answer | 4 |

Where both an inconsistency and an unreadable card happen at once, the **firm negative wins**: a
session that is inconsistent *and* whose cards were unreadable exits 1, because a script gating on
*is this usable* wants the definite answer and there is one. `--json` carries the whole story at
every code.

**`requivo doctor` exits 0 whatever it finds, and that is deliberate.** `verify` is a **gate**: you
run it to decide, and its exit code is the decision. `doctor` is a **report** — it describes what is
on this machine and never concludes what it means, because the same directory can be a
half-extracted archive or a leftover lock and nothing in it says which. Read `doctor`'s output, not
its status.

### Documents on stdin

Every command that takes a document accepts `-` in place of a path, and reads it from stdin:

```bash
requivo model apply <slug> - --expected-revision 3 --json <<'JSON'
{ "model": { … }, "questions": [], "summary": { "objective": "…" } }
JSON

requivo artifact save <slug> --type prd --file - --revision 3 --json < prd.md
echo "We need a leave approval system." | requivo session init -
cat request.txt | requivo discover -
```

This is what the Claude Code skills use: a caller holding the content need not invent a file for it.

### Importing a session

`session import` validates before it writes anything: the archive must hold exactly one session
directory whose name is a valid slug, within a file-count and expanded-size ceiling, with no entry
that could escape the session root. It is then extracted to scratch space and put through the same
integrity check as `session verify` — the revision log accounts for the model, every revision file is
there and matches the hash recorded for it, the current model *is* the last revision, every artifact
has a file — and only then moved into place.

A slug that already exists is refused unless `--force`, and a forced replacement is a swap: the
existing session steps aside and is deleted only once the new one is in place, so a failure leaves you
with the session you had rather than neither.

**What a `--json` consumer branches on.** Every refusal here names the archive or the store, never a
model. Assert on the code, never on the message.

| Code | HTTP | The archive… |
|---|---|---|
| `unreadable_archive` | 400 | is not a readable `.zip` at all. `details`: `{archive}` |
| `invalid_archive` | 400 | opens, but is not shaped like an export. `details`: `{problem, …}` |
| `inconsistent_archive` | 400 | holds a session that fails the integrity check. `details`: `{slug, problems}` |
| `session_exists` | 409 | is fine; that slug is taken and `--force` was not passed. `details`: `{slug}` |
| `import_destination_occupied` | 409 | is fine; something that is **not** a session already sits at the slug's directory. `details`: `{slug, path}` |

`invalid_archive` covers eight conditions under one code because they share one remedy — *give me a
different archive*. `details["problem"]` is present on all eight and says which: `empty`,
`too_many_entries`, `too_many_files`, `too_large`, `unsafe_entry`,
`entry_outside_session_directory` or `multiple_sessions`. The size and count arms add the numbers
they quote (`{entries, max_entries}`, `{files, max_files}`, `{bytes, max_bytes}`), the path arms add
`{entry}` and the multi-session arm adds `{slugs}`; nothing is padded to a common shape, so read the
shape after you have branched on `problem`.

The first three are arms of `InvalidSessionError`, so `except InvalidSessionError` catches every
*archive* refusal without enumerating them.

Three more codes reach this verb and are about neither the archive's shape nor the store's state:
`session_not_found` when the path you named is not a file, `invalid_slug` when the archive's one
directory is named something that could not be a session, and `import_move_failed` (500) when the
validated session could not be moved into place — the archive was fine and the store refused it.

`--force` does **not** lift the `import_destination_occupied` refusal — it replaces a *session*, and
the point of this code is that there is no session there. Move or delete the directory yourself; the
import never removes something it cannot interpret. What changed with #101 is in
[compatibility.md](compatibility.md#the-import-path-names-the-archive-not-the-model-101).

`session export` reads under the session's write lock, so an archive can never combine an old
`session.json` with a newer `model.json`, and it excludes any dot-prefixed entry — a scratch file
from an interrupted write, and a legacy `.lock` left inside a session by an earlier Requivo. The write
lock itself lives at `.requivo/locks/<slug>.lock`, outside every session directory; see
[session-format.md](session-format.md#layout) for why.

### Recovering a torn or inconsistent session

`doctor` and `session verify` diagnose and never write. When `model.json` no longer matches the
revision it claims, or is gone, `revisions/` still holds every model ever applied, and
`session restore <session> [--revision N]` is the repair. It copies a readable
`revisions/NNNN-model.json` over `model.json`, under the session's write lock, and nothing else:

- **The revision history is untouched.** No new revision is recorded, `current_revision` does not
  move, and no new `revisions/` file is written. This is not `model apply` under a different name —
  it is `model.json` catching up with a history that was already the truth.
- **Defaults to the newest revision this build can still read**, skipping over one that is itself
  corrupt rather than stopping at it, and says which one it picked. `session verify`'s own remedy
  line, printed beside a restorable problem, names the same revision — the two searches are the same
  function.
- **`--revision N` picks one explicitly**, and is refused rather than silently substituted when that
  revision does not exist, is missing on disk, or does not parse.
- **Refuses outright** when the session has no applied revision yet (`current_revision` is 0 — there
  is nothing to restore *from*), or when nothing in the requested range can be read at all. Recovery
  in that state is manual JSON surgery, or a backup.

Only the codes `session restore` can actually address get a remedy line from `session verify`:
`invalid_model`, `model_is_not_the_last_revision` and `missing_model` — model.json disagreeing with,
or absent against, a revision history that is otherwise intact. A broken revision log or a corrupt
`session.json` gets no remedy line: restoring would not fix it.

## Sessions from the `out/` layout

Before the versioned session store, discovery wrote to `out/<slug>/`. Nothing has written there since
0.8.0 or read it implicitly since 0.9.8. One command remains, the only thing that opens it:

```bash
requivo session migrate        # convert every out/<slug>/ session into .requivo/sessions/
```

It copies rather than moves — the originals stay where they are — and the converted model becomes
revision 1, with its artifacts recorded against it. A session still only in `out/` is reported as
missing with that command named in the error, rather than silently working at half capability.

**The receipt has five rows** (#262, #411): migrated; `errors` (a legacy `model.json` that will not
parse, named with its message while the rest still migrate); `skipped_already_present` (done — the
session is at revision 1 or later); `interrupted` (a previous run claimed the slug and crashed before
copying, so the session sits empty at revision 0 — delete `.requivo/sessions/<slug>` and re-run); and
`unreadable` (a legacy directory the process could not stat into, named with the OS error).

The command exits `4` (the same code `session list` and `session verify` use for "the work was done
and part of the answer was unreachable") whenever `errors`, `interrupted` or `unreadable` is
non-empty, so a script reading only the exit code still learns the run was not a clean success.

An **unlistable `out/` root** is firmer: nothing could be examined, so it exits `1` with a one-line
refusal (or the `--json` error envelope) naming the root.

## Reading `doctor`'s session findings

Each is **a report, not a repair**: Requivo does not delete, move, rewrite or re-permission anything
it finds, and does not say what an entry *is* — a leftover lock, a half-extracted archive and an
interrupted copy look the same from outside, and the evidence is the directory and only the directory.

### Something here that is not a session

A directory (or file) under `.requivo/sessions/` with no `session.json` is invisible to every listing,
yet **takes the name**: `create_session`'s rename loses to it, and asking for `leave-approval` yields
`leave-approval-a1b2c3` silently. Older Requivos left such directories behind when locking a slug with
no session (#22); nothing in this version creates one. `doctor` names them under
`sessions.non_sessions` and on its own row (#67) — `doctor` rather than `session verify`, because no
listing produces a slug to type:

```
  ✅ sessions        0 in this workspace
  🟡 other entries   1 entry under this directory that Requivo does not read
     └─ leave-approval — a directory holding 1 entry: .lock  [name taken]
```

Each entry carries `name`, `kind` (`directory` / `file` / `symlink` / `other` / `unknown`), `entries`
(up to five names), `entry_count`, and `slug_shaped` — whether a session asking for that name would
collide with it (a Windows reserved name such as `con` is `true`, #408; a name too long to be a slug
is `false`). A symlink is not followed. `entries: null` with an `error` means the directory could not
be listed; with no error, a `file` or `other`. An empty directory costs nothing on Linux and macOS
(`rename(2)` replaces it) but is still `[name taken]` on Windows. Dot-prefixed entries are
`create_session` staging directories and are never reported.

### Something here that could not be examined

An entry whose `session.json` cannot even be checked — mode `000`, another user's directory — is
neither a session nor a non-session, so it is reported as *could not tell* (#80): under
`sessions.unexaminable` (`name`, `error`), with the session count staying what could be
**confirmed**. `session list` gives it a degraded row and exits **4**; every healthy session is still
listed. `doctor`'s *whole root unreadable* arm is kept for `.requivo/sessions/` itself failing to list.

### Context cards a session can no longer find

A session records its card names, and the cards live **outside** it — in the package or
`REQUIVO_CONTEXT_DIR` — so a renamed card, a replaced install or another machine leaves a selection
that no longer resolves, refused at the next paid turn. `doctor` lists these under
`sessions.unresolved_cards` and `session verify` in a `context_cards` block, both offline. Put the
card back or point `REQUIVO_CONTEXT_DIR` at it. It is **environment, not integrity**: `problems`
answers whether the directory tells the truth about itself, so a colleague's archive is not refused by
`session import` for a card you lack. Both count toward the top-level `ok`.

### A card name cannot write a line of the receipt

Stored strings are untrusted on every read (#40, #70). A selector token carrying a control character
is **refused** as `unsafe_selector_token`, named in escaped form on one line; `doctor` and
`session verify` report such a session as unhealthy. Every string `session show` and `artifact list`
print is rendered through the same one-line rule, so a stored value cannot forge a line at a fixed
column; typed `int`/`bool` fields are refused by `read_meta` before rendering. Document bodies —
`artifact show`, and the `prd`/`criteria`/`epic`/`release` verbs (#430, #449) — go through
`display_document`, which neutralises the same C0/C1/DEL class but keeps `\n` and `\t`; it applies at
print time only, so files on disk and web downloads stay byte-identical.

`--json` is unaffected and keeps the bytes: JSON escapes controls below `U+0020`, and `ensure_ascii`
covers `U+007F`–`U+009F`. The terminal rule does not treat `U+2028`/`U+2029` as line breaks
(terminals do not either); parse `--json`, not human output, which escapes them as well.
