# Session format

> Where and how a session is stored. For what the model contains, see
> [requirements-model.md](requirements-model.md). For what is guaranteed not to break, see
> [compatibility.md](compatibility.md) — this layout is a **published contract**, at
> `format_version` 1.

A session is a directory under your **workspace** (the current directory, or `--workspace` /
`REQUIVO_WORKSPACE`). It is local, versioned, and shared by every interface — the CLI, the Claude Code
plugin and the Web app all read and write the same layout.

## Layout

```text
.requivo/
├── .gitignore              `*` — written once, on creation; see "Sessions and git" below
├── sessions/
│   └── <slug>/
│       ├── session.json        metadata + provenance + artifact status
│       ├── request.md          the originating request
│       ├── model.json          the current model — the durable product
│       ├── revisions/
│       │   └── 0001-model.json  one frozen file per applied revision
│       └── artifacts/           generated views — one file per type, named in compatibility.md
└── locks/
    └── <slug>.lock             the write lock (empty; safe to delete when nothing is running)
```

- **`.gitignore`** holds a single `*`, so git ignores the whole store — including the ignore file
  itself, the self-ignoring pattern `uv` writes into `.venv/`. It is written **once**, by whichever
  call first creates `.requivo/`, and never rewritten. See "Sessions and git" below for why, and for
  how to opt out.
- **model.json** is the product; every artifact is regenerated from it.
- **revisions/** freezes each applied model, so history is inspectable and `requivo impact` can reason
  from a past point.
- Every write is atomic (temp file + rename), so an interruption can't leave a half-written model. The
  temp file is unique per writer, so concurrent writers cannot collide on it.
- **`locks/<slug>.lock`** is an empty file held with an OS-level lock for the duration of a write.
  The kernel releases it when the process ends, so a crash never leaves a session locked. Against a
  live holder that is slow or stuck, acquiring waits up to 30 seconds, then raises a clear "locked by
  another process, retry" (#265). A lock file claims no slug.

  It sits **beside** `sessions/`, not inside the session it guards: an OS lock is a claim on an inode,
  writers write by pathname, and `session import --force` renames the session directory — a lock
  inside it would stop guarding what writers write to (#113), and Windows refuses to rename a
  directory holding an open handle. A `.lock` left inside a session by an earlier Requivo is read by
  nothing; deleting it is safe.

  `session delete` (#238) removes the directory and then its lock file. A session removed by hand, or
  by an older Requivo, leaves one empty lock file behind that claims nothing. `requivo doctor` counts
  lock files and names slugs with no session (#180), without calling them orphans — the two scans run a
  moment apart.

## Sessions and git

`.requivo/` is written into your **workspace** — the directory you run from, unless `--workspace` or
`REQUIVO_WORKSPACE` says otherwise. For the Claude Code plugin that is your project repository by
construction, and `request.md` holds the originating request **verbatim**: for most users that is a
client's own words, and often material they are under an obligation not to publish.

So Requivo writes `.requivo/.gitignore` containing `*` the first time it creates the store: a
routine `git add .` picks up nothing, and your own `.gitignore` is never edited. **Written once, never
restored**: delete or edit it and your choice stands — committing sessions is reasonable for a team
whose requests are not confidential.

**To share one session rather than all of them**, use the archive verbs — they work whatever the
ignore file says:

```bash
requivo session export <slug> -o <slug>.zip
requivo session import <slug>.zip
```

A colleague importing that archive into a workspace with no `.requivo/` yet gets the ignore file too,
for the same reason: the request text inside is someone's client's, not theirs.

## Revisions and provenance

Each applied revision records **who produced it**, in a `revisions` log in `session.json`:

| Field | What it answers |
|---|---|
| `provider`, `model_name` | which engine reasoned |
| `prompt_version` | `sha256:…` of the exact system prompt — prompt file + schema + the context cards actually selected |
| `surface` | which interface asked (`cli-discover`, `web-answer`, `cli-brief`, a Claude Code turn, `session-rescope`…) |
| `previous_revision`, `created_at` | where it sits in the history |
| `model_hash` | content identity of the model that was written |
| `usage_input_tokens`, `usage_output_tokens`, `usage_cache_read_tokens`, `usage_cache_write_tokens` | what the provider call(s) behind this revision actually spent |
| `usage_rate_per_mtok`, `usage_priced_as_of` | the `(input, output)` USD-per-million-token rate those calls were billed at, and the date of the table it came from |

Provenance belongs to the revision, because more than one surface moves a model over its life; the
prompt hash is there because behaviour is tuned by editing prompts and cards.

**The `usage_*` fields (#292) are absent — never zero — on most revisions**: a deterministic apply
(`model apply`, a Claude Code turn, `session import`) or a revision older than #292 carries none. A
provider-backed apply stamps the rate it was billed at, so a later rate-table edit cannot change an
old revision's cost. `requivo status` sums them into a "session cost so far" line, and says nothing
when none carry them.

Updates go through the single validated apply path and support an **optimistic-locking** precondition:
`requivo model apply <slug> proposal.json --expected-revision N` fails cleanly with a
`revision_conflict` if the session has moved on, instead of silently overwriting a concurrent change.
Provider-backed operations set it for you — a generation or an answers turn holds the revision it read,
so a change that lands while the provider is reasoning is a clean conflict rather than a lost update.

A whole update runs under the session's write lock, so the precondition and the writes it authorises
are held together: two processes on one session serialise, and the loser gets `revision_conflict`,
never a half-applied session.

**Not every revision changes the model.** `requivo session rescope <slug> --context <cards>`
(see [context-cards.md](context-cards.md#re-scoping-an-existing-sessions-cards)) records a new
revision whose `model_hash` is identical to the one it succeeds — the model carries forward
unchanged, and `surface: "session-rescope"` tells them apart. `context_cards` lives on `session.json`,
read fresh each turn, so a re-scope changes only what the *next* turn reasons against; at revision 0 it
mints no revision at all.

## Artifacts and freshness

`session.json` tracks each generated artifact: its file, when it was written, the **source revision**
it was generated from, and a `stale` flag. The type → filename map is in
[compatibility.md](compatibility.md#artifact-filenames--stable-and-part-of-the-session-format). The
estimate is reasoned against the user stories, so `requivo estimate` saves both from one snapshot
against one source revision (#519): the two `artifact_status` rows carry the same `revision`.

The source revision is *provenance*, not a verdict. An artifact is stale when something it rests on
actually changed — computed from the dependency graph — not because the session has moved past its
source revision. An old artifact whose inputs never moved is still fresh, and every surface reports the
flag rather than comparing numbers.

Two kinds of dependency feed that judgment:

- **Slots** — the facts an artifact consumes, per artifact (`ARTIFACT_SLOTS`). The saved assessment is
  the one that rests on all of them: it is a judgment over the whole model.
- **The reasoning layer** — the design decisions, challenges, opportunities, excluded options (#599)
  and decision thresholds (#604). Every generator is prompted with the complete model, reasoning
  included, so a rewritten decision can change a PRD with no slot touched. A model whose slots are
  identical but whose judgment moved is a different model.

Reasoning that a turn simply *omits* is not a removal — a refinement turn answers a question rather
than re-deriving the brief, so its reply routinely carries no decisions at all. That is resolved when
the proposal is validated, not when it is diffed: the five collections are tri-state in a proposal
(absent = keep, `[]` = delete, a list = replace), and `ModelProposal.resolve` collapses them against
the model being refined. The diff itself is symmetric, so an explicit deletion *is* reported and does
mark what rested on it stale.

Freshness is also computed when an artifact is saved **against an older revision** — `requivo artifact
save … --revision N`. Reasoning and saving are not the same moment: a provider call takes minutes, and
Claude Code may save a document it wrote several turns ago. The honest answer is knowable, so it is
given: the source revision is diffed against the current model, and the artifact is recorded stale on
the spot if its dependencies moved. `artifact save --json` returns the `stale` it recorded.

**`--revision` is required**: which revision the content was reasoned from is the one fact only the
caller holds, and reading an omission as "the current one" would record a plausible `stale: false`
nobody claimed (#57). Leaving it off is refused (`unstated_source_revision`) and nothing is written. A
source revision that cannot be *read* is refused as `unreadable_source_revision` (#82): provenance
that cannot be verified is not recorded.

## Stable identifiers

Design decisions, challenges, opportunities, exclusions and decision thresholds each carry an `id`
(`dec_…`, `chl_…`, `opp_…`, `exc_…`, `thr_…`) derived from their own content and recomputed on every
validation. It is the same value across revisions, surfaces and machines for as long as the statement
is unchanged, so a decision can be referred back to without quoting its text. A supplied id is never
trusted — it is always recomputed. A reworded statement gets a new id.

## Perimeter

`session.json` carries `perimeter` — which decision structure this session reasons in (`software`,
`go-to-market`, …), frozen the moment the session is created alongside the request and the card
selection (invariant 11). It never moves afterward: every turn's system prompt has to stay
byte-identical for the prompt cache to hold (#258), and a session's own slot vocabulary — what a
model, a `Question.slot` and every DAG edge (`derived_from`, `contests`, `rests_on`) may name — comes
entirely from it.

**A session with no `perimeter` at all** — the shape every session had before #608 — reads as the
**software** perimeter, the only one that existed. That is a default, not a guess: there was only
ever one vocabulary, so there is nothing to infer.

**A session naming a perimeter this install does not have is refused, by name** — the one inversion
of the "unknown vocabulary is tolerated" rule. An extra key or unknown artifact type is preserved and
never interpreted; a perimeter is interpreted, and the wrong one would validate, gate readiness and
trace a blast radius against the wrong vocabulary, confidently. `unknown_perimeter` is raised by the
loader (`migrate_session`) and reported the same way by `doctor` and `session verify`.

Which perimeters an install has is itself observable: `requivo doctor` (`--json`'s `perimeters` key)
lists them, and `requivo schema --perimeter <id>` prints any one of their schemas.

## Slugs

A slug names the session directory, so it is validated in the Core: strict kebab-case
(`^[a-z0-9]+(?:-[a-z0-9]+)*$`), no path separators or dot segments. An explicit `--slug ../../escaped`
is rejected before any path is built.

A slug that is a Windows reserved device name is refused too, on every platform, case-insensitively —
`con`, `prn`, `aux`, `nul`, `com1`-`com9`, `lpt1`-`lpt9`. Windows cannot create a file or directory with
one of those names, so refusing it everywhere keeps an archive portable to every platform (#221). The same
set is refused as the stem of an artifact filename (the part before the first dot — `con.md` and
`con.tar.gz` are both reserved).

## Verifying a session

A session is several files that have to agree: the revision count in `session.json`, the revision file
per revision, the current model that should equal the last of them, each artifact pointing back at a
revision that exists. Each file can be perfectly valid while the relationships between them are not —
an archive that lost its `revisions/`, a hand-edited `session.json`, a `model.json` swapped out from
under the hash its revision recorded.

```bash
requivo session verify <slug>          # exits non-zero, and says which claim is false
requivo session verify <slug> --json   # {"ok": false, "problems": [{"code": …, "message": …}],
                                       #  "notes": [], "context_cards": {"checked": true,
                                       #  "problem": null}}
```

The same check gates `session import` (an archive is held to exactly the standard a live session is)
and appears in `requivo doctor`, which names any session in the workspace that no longer adds up.

**What an interrupted apply leaves.** An apply is three writes — `revisions/NNNN-model.json`, then
`model.json`, then `session.json` — ordered so the first gap is harmless: nothing reads the frozen
file yet, `verify` reports `orphan_revision_file`, and the next apply reuses the number. Dying after
`model.json` is replaced is a real inconsistency — `model_is_not_the_last_revision`, or
`model_without_revision` on a first apply — repaired by re-applying or `session restore`.

A recorded artifact `filename` is untrusted: one that is not a plain file inside `artifacts/` is
reported as `unsafe_artifact_filename` and **not checked for existence**, since the answer would
reveal whether an arbitrary path exists. `import` refuses such an archive.

**An artifact type this build has no generator for is a `note`, not a problem** (#260): a new type
needs no `format_version` bump, so it counts toward neither `ok` nor the exit code, and does not stop
`session import`; its filename, file and revision are still checked. A key not *shaped* like a type
(lowercase, at most 64 characters) is refused as `unsafe_artifact_type`.

`context_cards` is reported beside `problems`, not inside it: the cards live outside the session, so
a missing one is a claim about the install (see
[cli.md](cli.md#context-cards-a-session-can-no-longer-find)). Both count toward `ok`.

## Sessions from the `out/` layout

Before this layout, sessions lived in `out/<slug>/`. Nothing has written there since 0.8.0, and since
0.9.8 nothing reads it implicitly: `requivo session migrate` converts them into `.requivo/sessions/`
(copying, not moving), and a session found only in `out/` is reported as missing with that command
named in the error.
