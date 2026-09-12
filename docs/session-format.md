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
- **`locks/<slug>.lock`** is an empty file held with an OS-level lock for the duration of a write. The
  kernel releases it when the process ends, so a crash cannot leave a session permanently locked —
  there is no stale-lock state to clean up, and no timeout to wait out on a *crashed* holder.

  A **live** holder is a different story, and both platforms now answer it the same way (#265).
  Acquiring the lock against a holder that is merely slow — or genuinely stuck (a suspended
  process, a debugger paused on a breakpoint, an NFS-mounted workspace) — waits up to 30 seconds
  and then raises a clear "locked by another process, retry in a moment" rather than hanging. Every
  write normally holds this lock for milliseconds, so this bound is never felt in ordinary use.

  It sits **beside** `sessions/` rather than inside the session it guards, and that is load-bearing
  rather than tidy. An OS lock is a claim on an *inode*; every writer under it resolves the session
  directory and writes by *pathname*. `session import --force` renames that directory, so the two
  stopped describing the same thing: a writer mid-write went on writing into the freshly imported
  session, and a third process opening the lock found a different file and acquired it. Keeping the
  lock outside is what lets the import hold it — a directory containing an open handle is precisely
  what Windows refuses to rename.

  A lock file claims no slug on its own: a session's name is claimed by the `sessions/<slug>`
  directory alone, and a file here never reserves one.

  **A session written by an earlier Requivo may still carry a `.lock` inside it.** Nothing
  opens it, `session export` skips it, and `session verify` ignores it. Deleting it is safe;
  leaving it costs nothing.

  **`session delete` (#238) removes both halves — `sessions/<slug>/` and its lock file — in one
  operation, unlinking the lock last, after its own lock is released.** A session removed by hand
  (`rm -rf`, bypassing that verb) or by an older Requivo with no delete verb at all still leaves the
  lock file behind: what stays is one empty file under `locks/` that claims no slug and is read by
  nothing. Delete it or leave it.

  **`requivo doctor` names this now (#180), and still stops short of concluding what it is.** Its
  `locks` check reports the total lock-file count and, of those, which slugs currently name no
  session — the ordinary shape this residue takes, from a hand-deleted session or an older Requivo —
  beside anything under this root that is not a `<slug>.lock` file `session_lock` could have written. It
  never prints the word "orphan": the lock scan and the scan of current sessions it is checked
  against run a moment apart, and a session created or removed in that gap reads exactly the same
  way for a tick without being residue at all — the same rule that keeps `doctor` from concluding
  what a non-session entry under `sessions/` is.

## Sessions and git

`.requivo/` is written into your **workspace** — the directory you run from, unless `--workspace` or
`REQUIVO_WORKSPACE` says otherwise. For the Claude Code plugin that is your project repository by
construction, and `request.md` holds the originating request **verbatim**: for most users that is a
client's own words, and often material they are under an obligation not to publish.

So Requivo writes `.requivo/.gitignore` containing `*` the first time it creates the store. A routine
`git add .` picks up nothing, and nothing has to be added to *your* `.gitignore` — a file Requivo has
no business editing.

**Written once, and never restored.** The trigger is the store directory not existing yet, not the
ignore file being missing. If you delete it, sessions become ordinary tracked files and stay that way;
if you edit it, your edit survives. Both are the same branch, and both are deliberate: committing
sessions is a reasonable choice for a team whose requests are not confidential, and Requivo should not
quietly overrule it on the next write.

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

Provenance belongs to the revision, not the session, because a model is moved by more than one surface
over its life. The prompt hash is there because behaviour is tuned by editing prompts and context
cards: "which model produced this" answers half the question, and the other half is what changed
between two runs that look identical in the log.

**The `usage_*` fields are additive, from #292, and absent — never zero — on most revisions.** A
deterministic apply (`model apply` from a hand-authored proposal, a Claude Code turn, which spends no
API tokens, `session import`) carries none of them; a revision written before #292 shipped carries
none either. Only a provider-backed apply made while a `requivo.usage` ledger was open (every CLI
command, every Web request) stamps them, and it stamps the rate it was actually billed at rather than
one looked up again later — a later edit to the vendor rate table must not retroactively change what
an old revision is reported to have cost. `requivo status` sums every revision that carries them into
a cumulative "session cost so far" line and says nothing when none do, the same three-state shape
`render_usage()` uses for a single run's own tokens/cost/latency.

Updates go through the single validated apply path and support an **optimistic-locking** precondition:
`requivo model apply <slug> proposal.json --expected-revision N` fails cleanly with a
`revision_conflict` if the session has moved on, instead of silently overwriting a concurrent change.
Provider-backed operations set it for you — a generation or an answers turn holds the revision it read,
so a change that lands while the provider is reasoning is a clean conflict rather than a lost update.

A whole update — read the metadata, check the precondition, write the model, freeze the revision,
rewrite the flags — runs under the session's write lock. The precondition and the writes it authorises
have to be held together: checked and then acted on with a gap in between, two writers can both pass
the same check and the second silently overwrites the first. Two Requivo processes on one session
therefore serialise; the loser gets `revision_conflict`, which is a real answer, and never a
half-applied session.

**Not every revision changes the model.** `requivo session rescope <slug> --context <cards>`
(see [context-cards.md](context-cards.md#re-scoping-an-existing-sessions-cards)) records a new
revision whose `model_hash` is identical to the one it succeeds — the model carries forward
unchanged, and `surface: "session-rescope"` is what tells the two apart on inspection. `context_cards`
itself lives on `session.json`, not per revision: it is the session's live setting, read fresh at the
start of every turn, so a re-scope changes what the *next* turn reasons against and nothing else.
Before any model exists (revision 0) there is nothing yet whose provenance the old selection could
describe, so re-scoping a session with no model does not mint a revision at all — it only updates
`context_cards`.

## Artifacts and freshness

`session.json` tracks each generated artifact: its file, when it was written, the **source revision**
it was generated from, and a `stale` flag. The type → filename map is in
[compatibility.md](compatibility.md#artifact-filenames--stable-and-part-of-the-session-format); every
generator has a file since #519, when `stories` and `estimate` stopped being terminal-only.

One artifact rests on another as well as on the model: the estimate is reasoned against the user
stories, so `requivo estimate` saves both, from one snapshot, against one source revision — the
`stories.md` beside an `estimate.md` is the draft that estimate was read from, and the two
`artifact_status` rows carry the same `revision`. Saving the estimate alone would have recorded half
its basis.

The source revision is *provenance*, not a verdict. An artifact is stale when something it rests on
actually changed — computed from the dependency graph — not because the session has moved past its
source revision. An old artifact whose inputs never moved is still fresh, and every surface reports the
flag rather than comparing numbers.

Two kinds of dependency feed that judgment:

- **Slots** — the facts an artifact consumes, per artifact (`ARTIFACT_SLOTS`). The saved assessment is
  the one that rests on all of them: it is a judgment over the whole model.
- **The reasoning layer** — the design decisions, challenges and opportunities. Every generator is
  prompted with the complete model, reasoning included, so a rewritten decision can change a PRD with
  no slot touched. A model whose slots are identical but whose judgment moved is a different model.

Reasoning that a turn simply *omits* is not a removal — a refinement turn answers a question rather
than re-deriving the brief, so its reply routinely carries no decisions at all. That is resolved when
the proposal is validated, not when it is diffed: the three collections are tri-state in a proposal
(absent = keep, `[]` = delete, a list = replace), and `ModelProposal.resolve` collapses them against
the model being refined. The diff itself is symmetric, so an explicit deletion *is* reported and does
mark what rested on it stale.

Freshness is also computed when an artifact is saved **against an older revision** — `requivo artifact
save … --revision N`. Reasoning and saving are not the same moment: a provider call takes minutes, and
Claude Code may save a document it wrote several turns ago. The honest answer is knowable, so it is
given: the source revision is diffed against the current model, and the artifact is recorded stale on
the spot if its dependencies moved. `artifact save --json` returns the `stale` it recorded.

**`--revision` is required, and that is the point.** Which revision the content was reasoned from is
the one fact only the caller holds; the store can see the session's *current* revision, which is a
different thing. Omitting the flag used to be read as "the current one", and the freshness question
was then answered against a revision nobody had claimed to read — necessarily `stale: false`, because
a source revision that *is* the current one cannot have moved. The recorded number was real and
plausible, so nothing downstream could detect it. Leaving it off is now refused
(`unstated_source_revision`), with the message naming the flag and the revisions the session has;
nothing is written by a refused save. The third state here is not a flag value — it is that the
record does not get made.

A source revision that cannot be *read* — a `revisions/NNNN-model.json` that is missing, truncated,
mis-encoded or unreadable — is refused the same way, under `unreadable_source_revision`. Provenance
that cannot be verified is not recorded, because `stale: false` is a claim about the artifact rather
than the absence of one. Two codes for two facts, sharing one `details` shape by choice;
`docs/compatibility.md` carries the reasoning.

It answered `invalid_session` until #82; the changelog carries the release. #57 gave the *unstated*
arm a code of its own and left this one on the family base, which made the pair distinguishable in
exactly one direction: a consumer branching on the unstated arm worked, and one branching on this arm
caught every other malformed-session fact along with it. #82 closed the other direction.

## Stable identifiers

Design decisions, challenges and opportunities each carry an `id` (`dec_…`, `chl_…`, `opp_…`) derived
from their own content and recomputed on every validation. It is the same value across revisions,
surfaces and machines for as long as the statement is unchanged, so a decision can be referred back to
without quoting its text. A supplied id is never trusted — it is always recomputed, so neither a model
nor a hand-edited session file can invent an identity. A reworded statement gets a new id; nothing in
the data says the rewording preserved the intent.

## Slugs

A slug names the session directory, so it is validated in the Core: strict kebab-case
(`^[a-z0-9]+(?:-[a-z0-9]+)*$`), no path separators or dot segments. An explicit `--slug ../../escaped`
is rejected before any path is built.

A slug that is a Windows reserved device name is refused too, on every platform, case-insensitively —
`con`, `prn`, `aux`, `nul`, `com1`-`com9`, `lpt1`-`lpt9`. Windows cannot create a file or directory with
one of those names, so a session slugged `con` created on macOS or Linux would export fine and then be
unopenable by `session import` on a colleague's Windows machine. Refusing it everywhere, rather than
only on Windows, is what keeps an archive portable to every platform it claims to be (#221). The same
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

**What an interrupted apply leaves.** Applying a revision is three writes and no transaction — the
frozen `revisions/NNNN-model.json`, then `model.json`, then `session.json` — so a machine that dies
between two of them leaves a real state, and the order is chosen so that the first of those gaps is
harmless. Only the frozen file is on disk, nothing reads it yet, and every read path goes on serving
the revision `session.json` records; `verify` reports `orphan_revision_file` and the next apply mints
the same revision number again, overwriting it. The revision number is never spent by a write that
did not finish. Die in the *second* gap, with `model.json` already replaced, and the session really
is inconsistent: `verify` says `model_is_not_the_last_revision` — the current model and the history
describe different states — or `model_without_revision` when the interrupted apply was the session's
first, since there is then no recorded revision for the model to disagree with. Re-applying is again
the repair.

A recorded artifact's `filename` is treated as untrusted while this runs. It is an unconstrained
string in the format, and a session may arrive from an archive or a hand edit, so it goes through the
same bare-filename guard every artifact write uses; a name that is not a plain file inside
`artifacts/` is reported as `unsafe_artifact_filename` and, deliberately, **is not checked for
existence**. Testing it would answer whether an arbitrary path on the machine exists — no content,
but the presence or absence of `missing_artifact_file` in the reply is itself the answer. `import`
refuses such an archive.

**An artifact type this build has no generator for is a `note`, not a problem** (#260). It appears
under `notes` rather than `problems`, counts towards neither `ok` nor the exit code, and does not stop
`session import` — because [compatibility.md](compatibility.md) lists a new artifact type among the
changes that need no `format_version` bump, so a session written by a newer Requivo is exercising the
format rather than breaking it. Everything else about that row is checked exactly as before: the
filename guard above, the file's existence, and the revision it claims. A key that is not *shaped*
like an artifact type — a plain lowercase name such as `risk-register`, at most 64 characters — is a
different answer and stays a refusal, as `unsafe_artifact_type`.

`context_cards` is a second, separate question the same command answers: do the context cards this
session was created with still resolve on this machine? It is reported beside `problems` rather than
inside it, because those cards live *outside* the session directory — an integrity problem is a claim
about the session, and a missing card is a claim about the install. Keeping them apart is what lets
`session import` accept a colleague's archive that names a card you do not have, while
`session verify` still refuses to call that session usable here. Both count towards `ok`. See
[cli.md](cli.md#context-cards-a-session-can-no-longer-find).

## Sessions from the `out/` layout

Before this layout, sessions lived in `out/<slug>/`. Nothing has written there since 0.8.0, and since
0.9.8 nothing reads it implicitly: `requivo session migrate` converts them into `.requivo/sessions/`
(copying, not moving), and a session found only in `out/` is reported as missing with that command
named in the error.
