# CLI reference

> Every `requivo` command. For a first run, see [getting-started.md](getting-started.md).

Run as `requivo <command>` after an install, `uv run requivo <command>` with uv, or
`python scripts/requivo_cli.py <command>` from a bare clone. Commands that call the Anthropic API need
the `anthropic` extra and `ANTHROPIC_API_KEY`; everything else is offline.

Verbs take a session **slug**. `status` and `impact` also accept a path to a saved `model.json`, so
they can read a model that is not in a session store; every other verb resolves a session, because it
writes a revision or an artifact back into one.

**This page is a reference, top to bottom.** Design history — why a check exists, what bug it closed,
how a behaviour used to differ — lives in [Design notes](#design-notes) at the foot of the page, out
of the way of a flag lookup. Every table below is read against `--help` by
`tests/test_cli_flag_names.py`, so a flag documented here is a flag the parser actually binds, and a
flag the parser binds cannot ship silently undocumented.

## Global flags, and reading `--help`

| Flag | Does |
|---|---|
| `requivo --version` | Print `requivo <version>` and exit 0. Read from the package, so it is the version you actually have |
| `requivo --workspace DIR <command>` | Where sessions are read and written (default: cwd). Accepted *before or after* the command |

`requivo --help` lists the verbs in journey order — `demo` and `discover` first, then refinement,
then the generators, then the offline plumbing — and marks the nine that spend money **`(API)`**.
Everything without the marker is offline and free, including `status` and `impact`, which take a
slug exactly like `brief` does and cost nothing. What the marked verbs cost is in
[providers.md](providers.md#what-a-run-costs).

## When a session cannot be found

Every route to it says the same three things: the reference it was given, **the sessions root it
searched**, and `requivo session list`.

```
no session named leave-aproval under /home/you/project/.requivo/sessions. `requivo session list`
shows the sessions in this workspace; a different --workspace (or REQUIVO_WORKSPACE) changes where
Requivo looks.
```

Naming the root is the point. The usual cause is not a typo but a different working directory —
sessions live under the workspace you run from, so a valid session is invisible from one directory
and present from another, and nothing about a bare "not found" says which of the two you are in.
The `--json` envelope is unchanged: `code` is still `session_not_found`, with the reference in
`details`.

## Discovery and refinement

| Command | Does |
|---|---|
| `requivo discover <request\|file\|->` | Analyse a request and create a session (interactive; `-` reads the request from stdin, `--once` for a single pass, `--context a,b` to scope cards) |
| `requivo answer <slug> "<answers>"` | Fold answers in and refine the model one more turn |
| `requivo status <slug>` | Understanding checklist + readiness, closing with the single next command (`--json` for a machine snapshot, with no pointer). No network |
| `requivo impact <slug> [slots…]` | What rests on given slots — decisions to re-validate + artifacts that go stale (no slots = full map). No network |

The context-card selector is spelled **`--context`** everywhere — on `discover`, on `session init`,
on `session rescope` and on `context`. `--cards` is a permanent alias of it on all four, kept because
`context` spelled it that way first (#85); the two are one option, so they can never mean different
things.

A selector — `--context a,b`, or the slot names given to `impact` — is checked rather than best-guessed.
An **empty** name is refused: `requivo impact <slug> ""`, which is what an unset shell variable expands
to, used to match every label and report the whole model as changed with nothing in the output to say
the input was malformed. A slot name that matches nothing is listed as unmatched and the rest still
resolve; an unknown *card* is a hard error, since dropping it would silently load every card instead of
the ones you asked for. Pass no selector at all to select everything deliberately. See
[context-cards.md](context-cards.md#scoping-a-session-to-relevant-cards).

**`status` ends by naming one next command**, never a menu, and the order it picks in is deliberate:
open questions win over a stale artifact (regenerating against a model that is about to move is a
paid call thrown away), a stale artifact wins over a missing brief. A converged session whose brief
is already fresh gets no pointer — there is no single next step, and inventing one would be the menu
this rule exists to refuse. `--json` never carries it.

**The slug is derived from the request**, and it drops function words and folds accents, so
*"We need a way to track vendor invoices"* becomes `track-vendor-invoices` rather than
`we-need-a-way-to`. Pass `--slug` to `session init` for an explicit one. A request in a script the
ASCII fold cannot romanize — Japanese, Cyrillic — still lands on `discovery`, and the second such
session on `discovery-<hash>`; that is a documented limit, not a bug, and an explicit slug is the way
past it. Sessions already on disk keep the names they were created with; see
[compatibility.md](compatibility.md#what-is-explicitly-not-stable) for what changes about re-running
`discover` on a request first analysed by an older Requivo.

**`discover` claims its session before it reasons**, on both the interactive path and `--once`. Two
consequences, and both are the point:

- Running `discover` again on a request whose session already carries a model is refused
  (`revision_conflict`) **before any API call**, not after the turns you paid for — a fresh discovery
  reasons from the request alone, so it would replace that work rather than refine it. Use
  `requivo answer <slug> "…"` to refine, or a different slug for a genuinely separate discovery.
- **Nothing you pay for is discarded after the session is claimed.** Stopping an interactive
  discovery early — `q`, an empty answer, Ctrl-C — keeps the turns that already ran: the session
  lands at revision 1 with its questions still open, which is exactly what `--once` produces, and the
  command names the `requivo answer <slug> "…"` that continues it. A provider failure part-way
  through the loop does the same, reporting the error alongside what was saved. Only a failure on the
  *first* turn leaves revision 0, because there is nothing to keep — that is the state
  `requivo session init` produces, and there `discover` is still the right retry.

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
it rests on. `stories` and `estimate` were the two exceptions until #519 — printed and never saved —
and `estimate` is the one document where being stale costs money, which is why it graduated
(`decision: the-estimate-graduates`).

`--export-json`/`--github`/`--gitlab` write versioned envelopes outside `ArtifactService` (no
staleness row of their own) that stamp `source_revision` and `slug` — the basis they were rendered
from, never a freshness verdict. That verdict is `requivo status --json`'s `artifacts.epic.stale`;
comparing revision numbers directly is the anti-pattern the staleness model exists to replace. The
full envelope shape, per-version skeleton and a worked consumer flow live in
[integrations.md](integrations.md#the-epic-export-envelope-requivo-epic).

## Local browser interface

| Command | Does |
|---|---|
| `requivo web [--host --port --workspace --no-open --reload]` | Launch the local Web interface (needs the `[web]` extra). Binds to `127.0.0.1` by default. See [web.md](web.md) |

## Local HTTP API (experimental)

| Command | Does |
|---|---|
| `requivo api serve [--host --port --workspace]` | Serve the local REST API (needs the `[api]` extra; `pip install 'requivo[api]'`). Binds to `127.0.0.1:8767` by default, with the OpenAPI docs at `/docs`. **Bound to anything but loopback it refuses to start unless `REQUIVO_API_TOKEN` is set**; with that variable set, every route under `/api/v1` except `/api/v1/health` requires `Authorization: Bearer <token>` (401 `unauthorized` otherwise), whatever the bind. The surface is experimental -- paths and shapes may still change; the design and the freeze conditions are in `docs/decisions/0004-the-http-api-facade.md` |

## Offline / deterministic verbs (no LLM, no key)

| Command | Does |
|---|---|
| `requivo demo` | Replay a bundled run — no key, no network |
| `requivo doctor [--json]` | Environment + install check (see [What `doctor` answers](#what-doctor-answers)) |
| `requivo schema [--framework]` | Print the slot schema (the model vocabulary + driver rule); `--framework` also prints the human framework spec |
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

`doctor` is the verb whose only job is *is anything wrong*, so every check it makes has three
answers, not two: it passed, it failed, or **it could not be made**. The third is the one that used
to be missing, and a check that reports "nothing found" when it could not look is worse than no check
at all.

| `--json` field | Reads |
|---|---|
| `requivo_version` / `python_version` / `os` | The three facts a bug report needs, printed as the first rows of the human view so a paste of them is a bug report |
| `model.name` / `model.source` | The model this install will reason with, and whether it came from an environment override (`env` — `REQUIVO_MODEL`, or the deprecated bare `MODEL`) or the built-in `default` |
| `schema.ok` / `schema.slots` / `schema.error` | The slot schema loaded, and how many slots it defines |
| `context.status` | `ok`, `empty` (the install has no context cards) or `unreadable` (a card directory exists but could not be enumerated — permissions, usually). `context.ok` is true only for `ok` |
| `context_cards` | The card names themselves — the plain list it has always been |
| `sessions.readable` / `sessions.total` / `sessions.error` | Whether the session directory could be listed at all. When it could not, `total` is `null` rather than `0`, because *no sessions* and *we could not look* are different answers and a user told the first concludes their sessions were deleted |
| `sessions.inconsistent` | `{slug: [integrity codes]}` — run `session verify <slug>` on each |
| `sessions.notes` | `{slug: [integrity codes]}` for findings that are **not** defects (#260) — today, an artifact type this build has no generator for, which [compatibility.md](compatibility.md) permits without a `format_version` bump. Reported so a type nobody can see is not a type nobody upgrades for; kept out of `inconsistent` so it moves neither the glyph nor any consumer's verdict |
| `sessions.unresolved_cards` | `{slug: error}` for a session whose saved context cards no longer resolve here |
| `sessions.cards_checked` | False when the card directory itself was unreadable, so `unresolved_cards` being empty means nothing |
| `sessions.non_sessions` | What is under the session root and is **not** a session — see [Design notes](#something-here-that-is-not-a-session). `null`, not `[]`, when the root could not be listed |
| `sessions.unexaminable` | Names under the session root that could **not be examined**, so whether they are sessions is unknown — `name` and `error` per entry. Not folded into `non_sessions`, which states a fact, nor into `total`, which stays what could be confirmed. `null`, not `[]`, when the root could not be listed. See [Design notes](#something-here-that-could-not-be-examined) |
| `locks.readable` / `locks.total` / `locks.error` | Whether `.requivo/locks/` could be listed at all, and how many `<slug>.lock` files it holds (#180) |
| `locks.sessions_checked` / `locks.unmatched` | Which of those slugs currently name no session — candidate residue from a session removed by hand (`rm -rf`, bypassing `session delete`) or by an older Requivo with no delete verb; `session delete` itself unlinks its own lock file as its last step, so an ordinary delete leaves nothing here. `unmatched` is `null`, not `[]`, when the *current session list* itself could not be read, on the same reasoning as `sessions.cards_checked` |
| `locks.unexpected` | Names under `.requivo/locks/` that are neither a `<slug>.lock` file `session_lock` could have written nor a `<slug>.discovering` guard file a first discovery could have left (#391) — a stray file, a directory, a symlink, a stem neither writer could have been given. A reserved-name stem such as `nul` or `con` is recognised by shape alone (#372, #401, #409) whether or not a session by that name currently exists on disk — `locks.unmatched` is the separate question of whether one does. `null`, not `[]`, when the lock root could not be listed at all |
| `locks.unexaminable` | Entries under `.requivo/locks/` whose examination raised — `name` and `error` per entry, on the same terms as `sessions.unexaminable`. `null`, not `[]`, when the lock root could not be listed |
| `output.streams[].state` | `safe` (a character the console cannot encode is escaped visibly, never fatal), `lossy` (it cannot crash but drops or blanks the character with no mark — only reachable by setting `errors=replace`/`ignore` yourself), `will_crash` (a strict handler on a narrow codec, so a glyph would kill the command mid-report) or `unknown` (the stream does not expose a codec, so this check could not look) |

An `empty` context is a broken install rather than a quiet inconvenience: the cards are what impact
is estimated against, and impact is half of `information_value = uncertainty × impact`. Discovery
would still run and still produce a model — it would just ask duller questions, for a reason nothing
on screen would name.

For why a directory under `.requivo/sessions/` can be reported as "not a session" or "could not be
examined" at all, why a stored card name is escaped before it reaches your terminal, and why a lost
context card is reported separately from a session-integrity problem — see
[Design notes](#design-notes).

### What `artifact list --json` answers

```json
{"slug": "leave-approval",
 "artifacts": {"prd": {"revision": 3, "filename": "prd.md",
                       "updated_at": "2026-08-20T14:25:28Z", "stale": false}}}
```

The rows live under `artifacts`, keyed by type, and `stale` is the dependency graph's verdict — not
a comparison of `revision` against the session's current one, which is provenance (see
[dependencies and staleness](requirements-model.md#dependencies-and-staleness)).

`slug` is the name you asked under, not the one stored inside `session.json` — the same reading
`session verify` and `session import` give it, and the safe side of a value that is untrusted on
every read back.

A session with nothing saved answers
`{"slug": …, "artifacts": {}}`, which states which session was asked about; it used to answer `{}`,
which a consumer could not tell from a payload that failed to serialise.

**This payload used to be the bare inner map** (#107) — `{"prd": {…}}`, top level keyed by artifact
type. **Breaking**, same class as #87 and #84: `jq '.artifacts'` where you had `jq '.'`. The rows
are untouched. The reason is #87's, one shape along — a top level made of data cannot gain a field,
because the consumer read is `for t, info in payload.items()` and any key added later is both
ambiguous with a future artifact type and breaks that loop.

### What `model apply` takes

A proposal replaces the model, so it carries the **complete** slot set and a non-empty
`summary.objective`. The three reasoning collections are the exception, and they are tri-state: leave
`decisions`, `challenges` or `opportunities` out and the established ones stand; send `[]` and they are
deleted (and what rested on them goes stale); send a list and it replaces. A refinement normally says
nothing about them. To check a partial projection without applying it, use
`model validate --allow-partial`. See [compatibility.md](compatibility.md#what-a-proposal-means).

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

Three exists because 1 would be a lie in the one case that costs money. `requivo brief <slug>` makes
its provider call, applies the revision and writes the artifact *before* it prints anything — so a
renderer that dies at the final `print` and reports failure invites a re-run that pays for a second
call and stacks a second revision on the first.

The message reads the run's usage ledger rather than assuming: it says a call **has** been billed
only when one was, because several verbs (`doctor`, `status`, `schema`) never call the provider at
all and `discover` prints before it does. Telling you not to re-run a command that cost nothing
would be the same misreport one layer up.

Four describes a **shape of answer rather than a verb**. Two commands reach it today.

`requivo session list` lists every session it can and gives one it could not read its own row:

```
Sessions under /work/.requivo/sessions:
  leave-approval                           rev 3  (anthropic, 2026-08-19T09:04:11Z)
  event-checkin                            could not be read — session format v2 is newer than this Requivo understands (v1) — upgrade requivo.

1 entry could not be read. `requivo session verify <slug>` reports what is wrong in full.
```

The footer counts **entries**, not sessions: one of these rows can be an entry nobody could examine,
and calling that a session is the one claim the third state exists to refuse. The degraded row
**names the session and states nothing it could not read** — no revision, no provider, no timestamp.
A session at **revision 0** is not this state: it has no model yet because nothing has analysed it,
which is a normal row and reads as one.

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
its status. The history behind both of these codes — why 4 exists rather than a fourth code per verb,
and why `doctor` and `verify` look like siblings and are not — is in [Design notes](#design-notes).

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

This is what the Claude Code skills use. A caller that already holds the content should not have to
invent a file for it — the temp files the skills used to write were a shared path (two sessions
overwrote each other), used a filename that is illegal on Windows, and needed `rm` to clean up.

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
import never removes something it cannot interpret. The full before/after story for both this code
and #101's archive-vs-model split is [compatibility.md](compatibility.md#the-import-path-names-the-archive-not-the-model-101)
— it is table-for-table what changed and why, and is not repeated here.

`session export` reads under the session's write lock, so an archive can never combine an old
`session.json` with a newer `model.json`, and it excludes any dot-prefixed entry — a scratch file
from an interrupted write, and a legacy `.lock` left inside a session by an earlier Requivo. The write
lock itself lives at `.requivo/locks/<slug>.lock`, outside every session directory; see
[session-format.md](session-format.md#layout) for why.

### Recovering a torn or inconsistent session

`doctor` and `session verify` are read-only by design — they diagnose and never write. That used to
be where the story stopped: a session where `model.json` no longer matches the revision it claims to
be, or is simply gone, got a code and a message and no next step, and the fact that `revisions/`
holds every model ever applied — a complete history a healthy repair could be built from — lived
nowhere a user reading the output would find it.

`session restore <session> [--revision N]` is that repair. It copies a readable
`revisions/NNNN-model.json` over `model.json`, under the session's write lock, and nothing else:

- **The revision history is untouched.** No new revision is recorded, `current_revision` does not
  move, and no new `revisions/` file is written. This is not `model apply` under a different name —
  it is `model.json` catching up with a history that was already the truth.
- **Defaults to the newest revision this build can still read**, skipping over one that is itself
  corrupt rather than stopping at it, and says which one it picked. `session verify`'s own remedy
  line, printed beside a restorable problem, names the same revision — the two searches are the same
  function.
- **`--revision N` picks one explicitly**, and is refused rather than silently substituted when that
  revision does not exist, is missing on disk, or does not parse. A repair tool that quietly restored
  from something other than what you asked for would be the wrong kind of helpful.
- **Refuses outright** when the session has no applied revision yet (`current_revision` is 0 — there
  is nothing to restore *from*), or when nothing in the requested range can be read at all. Recovery
  in that state is manual JSON surgery, or restoring the session from a backup — this verb does not
  invent a repair where there genuinely is none, the same caution `session import --force`'s own swap
  is built on.

Only the codes `session restore` can actually address get a remedy line from `session verify`:
`invalid_model`, `model_is_not_the_last_revision` and `missing_model` — model.json disagreeing with,
or absent against, a revision history that is otherwise intact. A broken revision log or a corrupt
`session.json` is a different kind of problem, and copying a revision over `model.json` does nothing
about either — naming a fix that would not fix the problem is worse than naming none.

## Sessions from the `out/` layout

Before the versioned session store, discovery wrote to `out/<slug>/`. Nothing has written there since
0.8.0, and since 0.9.8 nothing reads it implicitly either — the automatic fallback and the
migrate-on-first-write are gone, along with the flag CLI (`python src/engine.py …`) that produced that
layout.

One command remains, and it is the only thing that opens an `out/` directory:

```bash
requivo session migrate        # convert every out/<slug>/ session into .requivo/sessions/
```

It copies rather than moves — the originals stay where they are — and the converted model becomes
revision 1, with its artifacts recorded against it. A session still only in `out/` is reported as
missing with that command named in the error, rather than silently working at half capability.

**The receipt has five rows, not two, since #262 and #411.** A legacy session whose `model.json`
will not parse no longer aborts the whole pass — it is named under `errors` with its own message,
and every other legacy session still migrates. And a canonical session already occupying a legacy
slug is split into two different facts rather than one: `skipped_already_present` means the
migration is genuinely done (the session is at revision 1 or later); `interrupted` means a previous
run claimed the slug and crashed before the model was copied in, so the session sits empty at
revision 0 and the legacy data was never migrated. The remedy for `interrupted` is in the message
itself — delete `.requivo/sessions/<slug>` and re-run.

`unreadable` is the fifth row (#411): a legacy directory the process could not even stat into (a
permission bit denying it, most commonly) is reported by name, with the OS error, rather than
aborting the scan that decides what to migrate in the first place — the identical isolation #262
gave the loop *body*, one level up, in the scan that *produces* the loop's rows. It is never counted
as a session and never silently dropped.

The command exits `4` (the same code `session list` and `session verify` use for "the work was done
and part of the answer was unreachable") whenever `errors`, `interrupted` or `unreadable` is
non-empty, so a script reading only the exit code still learns the run was not a clean success.

**The `out/` root itself being unlistable is a different, firmer failure**, distinct from one
unreadable entry inside it: nothing at all could be examined, so there is no partial receipt to
print. That case exits `1` with a clean, one-line refusal (or the `--json` error envelope) naming
the root — "no answer", not "the answer is incomplete."

## Design notes

Everything above is what a flag does. Everything below is *why it works that way* — a bug this
repository hit, what it looked like, and what closed it. None of it is needed to run a command; all
of it is needed to understand why `doctor` and `session verify` are shaped the way they are. This
section is the reference page's own answer to CLAUDE.md's rule for a comment that recounts a past
bug: keep the story next to the behaviour it explains, backed by a test that goes red if the guard
it describes is removed. `docs/decisions/` — where a repository with no live claim on that directory
would put pure archaeology with no such test — is held by another branch as this page was split; see
the note at the end of this section.

### Something here that is not a session

A directory under `.requivo/sessions/` with no `session.json` is invisible to everything that lists
sessions — which, until 0.10.0, was everything. `list_session_slugs` filters on `session.json`;
`doctor` and `session verify` both reason over the slugs it returns; and `check_session` answers about
a directory it is handed, which nobody could hand it a name for. The commonest source is an older
Requivo: `session_lock` used to create the session directory in order to open `.lock` inside it, so
locking a slug with no session left one behind (#22). Those are still on disk.

Nothing in this version can produce one. The refusal #22 added stopped new ones, and the lock no
longer lives inside a session at all — it is at `.requivo/locks/<slug>.lock` — so `session_lock`
cannot create a directory under `sessions/` even in principle. What follows describes what is
**found** on disk, not what Requivo makes.

The symptom is not where the cause is. **The name is taken**, and `create_session`'s rename is the
only claim on a slug — it loses to anything already occupying the name, after which the CLI falls
through to its hash-suffixed candidate. Ask for `leave-approval` and you get `leave-approval-a1b2c3`,
silently, with nothing anywhere explaining why the name you asked for was unavailable. A stray *file*
at a slug name costs exactly the same and is reported the same way.

`doctor` names them, under `sessions.non_sessions` and on a row of its own (#67):

```
  ✅ sessions        0 in this workspace
  🟡 other entries   1 entry under this directory that Requivo does not read
     └─ leave-approval — a directory holding 1 entry: .lock  [name taken]
     [name taken]: a new session asked for that name will not get it. …
```

`doctor` rather than `session verify`, because `verify` is per-session and takes a slug — and the
defining property of one of these is that no listing produces its name, so there is no slug to type.
`session list` still shows only sessions: a listing of sessions must not grow a row for something
that is *established* not to be one.

**It is a report, not a repair.** Requivo does not delete, move or rewrite anything here, and it does
not say what these are. A directory holding only `.lock` is almost certainly a leftover lock, and
almost certainly is not enough: a half-extracted archive and an interrupted copy are the same shape
from the outside, and this project's rule is that the evidence is the directory and only the
directory. So each entry carries what was found — `name`, `kind` (`directory` / `file` / `symlink` /
`other` / `unknown`), `entries` (up to five names) and `entry_count` — and one derived flag,
`slug_shaped`, which is a property of the *name*: whether `create_session`'s rename would collide
with this directory rather than being refused outright, and so whether the entry costs anybody
anything. A name too long to be a slug is `false` there, because `canonical_dir` refuses such a name
outright and loudly rather than substituting silently. A Windows reserved device name (`con`, `nul`,
`lpt1`, ...) is `true`, since #408: the directory already occupies the name, so `create_session`'s
own reserved-name refusal never gets a chance to fire and the rename simply loses instead.
There is no field spelling a conclusion.

A **symlink is not followed**. It would otherwise be reported as whatever it points at, and the
listing beneath it would carry that target's filenames into a report about your workspace.

Three states here too. `entries: null` with an `error` means that directory could not be listed —
never `[]`. On Linux and macOS an empty directory is the one shape that costs nothing at all, because
`rename(2)` replaces an empty destination and the session still gets the name it asked for; on Windows
it does not, so an empty directory is still reported and still marked `[name taken]`. `entries: null`
with no error is a `file` or an `other`: there is nothing to look inside. And the whole key is
`null` when the session root itself could not be listed, where an empty list would read as *we looked
and there is nothing else here*.

Dot-prefixed entries are never reported: a slug cannot start with a dot, so they are `create_session`
staging directories — a session in flight, not something left behind.

### Something here that could not be examined

A third state, and the sentence about what is *established* above is what it turns on (#80). Deciding
whether a name is a session means asking whether `<name>/session.json` is there — and that question
can itself fail. A directory the process cannot stat into (mode `000`, or one owned by another user)
answers neither yes nor no, and that failure used to escape the partition and take **every** entry
with it: `session list` exited 1 with an empty listing and a raw `PermissionError`, and every healthy
session in the workspace was invisible.

Such an entry is now its own answer, because both of the others would be claims nobody established.
Calling it a non-session hides it from `session list` — the invisible entry the section above is
about, one step along. Calling it a session says it *is* one, which is exactly what could not be
checked. So it reaches both surfaces as *we could not tell*:

```
  🟡 sessions        1 in this workspace · 1 entry that could not be examined
     └─ blocked — could not be examined: [Errno 13] Permission denied: …/sessions/blocked/session.json
     Requivo cannot tell whether this is a session, so the count above (1) is what it could confirm,
     not what is there. …
```

The count stays what could be **confirmed**. Absorbing the entry into it would trade a correct number
for a vague one, which is the same trade `other entries` declines above. In `--json` it is
`sessions.unexaminable`, one object per entry with `name` and `error`, and `null` rather than `[]`
when the root could not be listed at all — the same reading as its neighbour.

`session list` gives it a degraded row and exits **4**: the entry is named, every healthy session is
still listed in full, and the row states nothing it could not read. `doctor` keeps its *whole root
unreadable* arm for the case that genuinely is the whole root — `iterdir()` on `.requivo/sessions/`
itself failing. One entry failing is not that, and answering it that way was a claim broader than
what happened.

**A report, not a repair here too.** Requivo reads your workspace; it does not change permissions in
it.

### Context cards a session can no longer find

A session records the context cards it was created with, and those cards live **outside** the
session — in the installed package, or in `REQUIVO_CONTEXT_DIR`. Rename a card, replace an install,
or open the session on another machine, and the saved selection no longer resolves. Since that is now
a refusal rather than a silently empty context, such a session is stopped at its next reasoning turn,
which is a paid call minutes into a conversation.

Both health verbs now say so first, offline: `doctor` lists the sessions under
`sessions.unresolved_cards`, and `session verify <slug>` reports it in a `context_cards` block and
exits non-zero. Recovery is to put the card back, or to point `REQUIVO_CONTEXT_DIR` at wherever it
now lives.

It is deliberately **not** a session-integrity problem. `session verify`'s `problems` list, and
`check_session_dir` behind it, answer whether a session directory tells the truth *about itself* —
and a card is not in the directory. Reporting a lost card there would make the same session coherent
on one machine and broken on another, and would make `session import` (which refuses an archive on
integrity problems) reject a colleague's perfectly good session because you lack one of their cards.
So the two are reported side by side and named differently: `problems` is internal, `context_cards`
is environment. Both count towards the top-level `ok`, because either one means the session cannot
take another turn.

### A card name cannot write a line of the receipt

A session's `context_cards` is caller text that **persists**, and `session import` accepts an archive
without inspecting it — deliberately, because a card lives outside the session directory and its
absence is a fact about your machine, not about the archive. Both health verbs then render those
names into their output.

Until 0.10.0 they rendered them bare, so a name containing a newline did not merely look odd: it
ended the line and started a new one at whatever column it chose. A session could print `doctor`'s
own `sessions` row, byte-identical in shape and column, saying *all clear* directly beneath the row
that was reporting it — and forge `session verify`, the verb whose entire job is to say whether a
session is telling the truth, while `verify` still exited 1 (#40).

A selector token carrying a control character is now **refused** rather than displayed, at the one
function every selector passes through, and reported as `unsafe_selector_token`. The refusal names
the offending value in escaped form, on one line. Two consequences worth knowing:

- `doctor` and `session verify` report such a session as unhealthy and name it. The finding is not
  dropped — it is shown, quoted and escaped, so you can see exactly what is stored.
- `--json` output is unaffected and keeps the bytes verbatim. The escaping is a property of rendering
  to a terminal, not of the finding, and JSON already carries a newline safely.

`session show` prints the stored names without selecting anything with them, so no refusal can run
there; it renders each through the same one-line rule instead. A name that was already safe is
printed byte-for-byte, so ordinary output is unchanged.

That rule now covers **every string `session show` prints**, not just the card names (#70). The card
selection was the field #40 happened to be about; `slug`, `session_id`, `created_at`,
`updated_at`, `provider`, `model_name`, and each artifact's type and filename are read back out of
the same file and were reaching the terminal bare. `session list` was fixed for three of them in #62
and this is the same defect in the other verb — with a sharper edge, because every line `session
show` prints is one Requivo writes itself at a fixed column, so a forged `  revision 0` under a
session that is at revision 12 is indistinguishable from the real thing. `current_revision`, an
artifact's `revision` and its `stale` flag need nothing: they are typed `int` and `bool`, so
`read_meta` refuses a string there before the render runs.

`artifact list` prints two of the same fields — the `artifact_status` key and the `filename` — and
is escaped alongside it. Found by sweeping the class rather than the instance: escaping a stored
value in one of the two verbs that render it leaves the rule meaning *wherever somebody looked*.

`artifact show` prints the artifact's own *body* — a saved markdown document, not metadata — and
closes one more member of this class (#430): a hostile client request that steers the model into an
artifact carrying a raw ESC sequence printed it verbatim. Reusing the one-line rule above would be
wrong here, not merely redundant, because a document's own newlines and tabs are its layout: escaping
them would turn an honest multi-paragraph brief into one long line of visible `\n` escapes.
`display_document` is the document-shaped sibling — the same C0/C1/DEL class, minus `\n` and `\t` —
so a raw ESC is still neutralised while an ordinary document renders unchanged. It runs at print time
only: the saved file on disk and the web download route stay byte-identical, which is what
`core/integrity.py`'s hashing rests on.

**The whole class, closed one release later (#449).** Review of #430 found the identical gap open on
four sibling verbs: `requivo prd`, `criteria`, `epic` and `release` printed each generator's markdown
straight to the terminal in `cli.py` (`_cmd_prd`/`_cmd_criteria`/`_cmd_epic`/`_cmd_release`), through
none of `display_token`/`display_text`/`display_document` — worse in reach than `artifact show`,
since no saved artifact has to exist yet: the hostile reply reaches the terminal on the one paid call
that produced it. All four now route their print through `display_document` too, the same
print-time-only shape as `artifact show`: the file `_wrote` reports and the web download route stay
byte-identical, only what reaches this terminal changes.

`--json` is unaffected on all three verbs, and the reason is narrower than it looks. JSON's grammar
forbids a literal control character below `U+0020` inside a string, so a newline is escaped
regardless of any encoder option. `json.dumps`' `ensure_ascii=True` default is what covers the rest
of the guarded range, `U+007F`–`U+009F` — `NEL`, a line terminator, and `CSI`, an escape introducer.
Both halves are pinned by test, because a test probing only with a newline would be green with that
default turned off.

**What the terminal rule does not cover**, stated because the two are easy to assume identical. The
one-line rule guards C0, DEL and C1 — the class that can move a terminal's cursor or end its line.
`str.splitlines()` breaks on a wider set, including `U+2028` and `U+2029`, which are returned
unchanged. On a terminal that is right: xterm and the VT sequences behind it answer to CR and LF, not
to Unicode `Zl`/`Zp`. It matters if you parse this human-readable output line by line — don't; that
is what `--json` is for, and `--json` escapes those two as well, which makes it the stricter of the
two paths.

### On this section's own home

CLAUDE.md's rule for narrative in this codebase is: back it with a test, or move it to
`docs/decisions/`. Every paragraph above is backed by a test — `test_cli_doctor` exercises the
non-session state and `test_unexaminable_entries` the could-not-examine state, and the
control-character escaping is pinned by
`test_session_show_json_escapes_a_control_character_before_it_reaches_a_line` and its siblings — so
none of it is the archaeology that rule sends to `docs/decisions/`. It moved here, to the foot of
this page, rather than there, because `docs/decisions/` is a different branch's file this tick; if
that changes, this section is the candidate to relocate, not to duplicate.
