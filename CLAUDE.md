# CLAUDE.md

Guidance for Claude Code working in this repository. Read the invariants section before changing
anything in `core/` or `services/` — several of them look like details and are not.

## What this is

**Requivo — a requirements engine.** It turns a vague client request into a *structured solution
model* ready for dev. It is **not a chatbot**: the chat is only an interface. The product is the
**model** (a set of typed slots) and the engine that progressively fills it until it is precise enough
to build from. Everything in the repo — code, comments, docs, prompts and context cards — is in English.
The engine's **output** is split, and deliberately so: the questions and the understanding it renders
each turn mirror the language of the client's request, while every buildable artifact — the decision
brief, PRD, stories, criteria, epic, release notes — anchors English, because those feed dev teams
and trackers. The policy, and the prompt sentences that enforce it, are in
`docs/requirements-model.md` under "The language of the outputs".

## Run and test

```bash
cp .env.example .env                    # ANTHROPIC_API_KEY; REQUIVO_MODEL defaults to claude-sonnet-5
uv run requivo demo                     # replays a saved run — no key, no network, no arguments
uv run requivo discover "We'd like a leave approval system."   # → .requivo/sessions/<slug>/
uv run requivo status <slug>            # understanding checklist + readiness (offline)
uv run requivo prd <slug>               # regenerate any artifact from the saved model
```

Classic install (equivalent; drop `uv run` once the venv is active):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools           # a fresh venv often ships pip < 21.3, too old for editable installs
pip install -e ".[dev]"                 # deps + the `requivo` command + pytest
.venv/bin/python -m pytest tests/ -q    # the whole suite: no API calls, no network, no build step
.venv/bin/ruff check src tests scripts  # lint (CI runs the same)
```

`requivo` is the command. Verbs: `discover`,
`answer`, `demo`, `status`, `impact`, `brief`, `prd`, `stories`, `estimate`, `criteria`,
`epic` (`--export-json/--github/--gitlab` — three flags of one kind, each writing an export file;
`epic` deliberately has no stdout `--json`, see #83), `release`, `web`, plus the offline ones in `deterministic/`
(`doctor`, `schema`, `context`, `session` incl. `verify`, `model`, `artifact`). `impact` is a pure query over the
dependency DAG — no API call. Without an install, `python scripts/requivo_cli.py <cmd>` is equivalent
(the launcher lives under `scripts/`, not at the repo root, where it would shadow the package).

Two worked examples live under `examples/`: `leave-approval/` (a one-line request → model → brief →
PRD) and `event-checkin-reconciliation/` (a messy multi-feature client email → an assessment that
refuses its conflation → epic + criteria). `requivo demo` replays the second from disk.

## Architecture

Reasoning is a **single LLM call per turn** whose intelligence lives in assembled prompt data, not in
Python — and that call lives in a **provider**, never in the core. The layers form a strict DAG:

- **`core/`** — the deterministic engine. Never prints, never reads argv, **never calls an LLM, never
  imports a provider** (guarded by `tests/test_boundaries.py`). It validates, versions, and reasons
  over the model; it never *produces* one.
- **`providers/`** — the only place an LLM is called. `base.py` is the `ReasoningProvider` protocol,
  `errors.py` its failure type; the `anthropic/` package (behind the optional `requivo[anthropic]`
  extra) implements it. The Claude Code surface is a *second* provider that lives outside Python:
  Claude reasons, the deterministic CLI applies. What a run *cost* is not a provider concept and
  lives outside this package in `requivo.usage` (#167).
- **`services/`** — the application seam, and the only place the two meet. `SessionService.update_model`
  is the single validated apply path (validate → diff → propagate → revision → stale-flag);
  `DiscoveryService` is the single provider-backed orchestration (reason → apply → save), including
  the un-persisted `draft_turn` an interactive surface loops over — a surface owns the loop, never a
  client.
  Both storage (`SessionRepository`) and reasoning (`ReasoningProvider`) are injected, so the
  orchestration is backing-agnostic — a Postgres repository reuses it verbatim.
- **`render/`** turns data into strings; **`cli.py`**, **`deterministic/`** and **`web/`** are the
  only layers that touch argv/stdout/HTTP.

Every interface — the terminal CLI, the Claude Code plugin, Requivo Web — is a thin layer over the same
services. There is never a second implementation of an apply, a generation, or a staleness rule.

That was stated in three places and enforced in none, which is how the CLI's interactive `discover`
loop came to reason two provider calls of its own and use the service only for the write (#77).
`tests/test_boundaries.py` guards it now, from both ends of the same arrow: `core/` may not import a
provider, and a surface may reach only the provider names an allowlist there names as *surface*
concerns, each with its reason.

**"A surface" meant `cli.py` alone for a release, and that was the hole (#167).** `render/` was in
neither scan set, so the layer with the *weakest* claim to a provider import was the only one with
no guard — and `render/terminal.py` imported `PRICING_AS_OF` and `UsageLedger` from
`providers.anthropic` straight through the hardening effort that produced the allowlist. `render/`
is in the scan set now, the allowlist is keyed by **(file, name)** rather than by name alone, and it
reaches nothing: what it needed was never Anthropic's. The fix for a leak like that is to move the
neutral concept out of `providers/`, not to write it an allowlist entry.

The storage half — a surface reaching past `SessionRepository` to `core.persistence` — was #76, and
it is guarded now too, over `cli.py`, `deterministic/` and `web/`. Twenty-seven direct calls became
fifteen; what remains is every call for which no backing-neutral form exists, because it is *about*
a path: `canonical_dir` telling a caller where a session landed, `artifact_path` validating a
filename read off disk, `migrate_legacy` converting one filesystem layout into another,
`validate_slug` refusing a name before any session exists to ask a repository about. A CLI that
talks about files is entitled to know about files; the target was never zero direct calls, only zero
unjustified ones, and the reason each survivor is justified is now written both at the call site and
in the guard's allowlist. That allowlist is keyed by **(file, function)** and asserted in both
directions, so a new call goes red under the name of the file that made it, and an entry whose call
site is gone goes red as unchecked prose.

They are equal in capability and **not** equal in weight. **Web is the product experience, Claude Code
is an integration, the CLI is infrastructure.** That ordering is a product decision, held in the
README, `docs/`, and the relative prominence of each surface's documentation — never in the code, where
all three reach the same services.

```
requivo/
  paths.py         ASSETS (read-only) + workspace_root()/session_root()/lock_root() + output_root() (retired out/)
  streams.py       stdout/stderr encoding — one chokepoint, called once by cli.app() (see invariant 16)
  assets/          bundled data shipped in the wheel: prompts/ framework/ context/ demo/
  core/            the deterministic engine — no LLM, no provider, no argv/stdout
    contracts.py     Pydantic contracts (StrictModel base) + stable ids + slot vocabulary, and the
                     permissive PersistedEngineOutput mirror everything reads off disk through
    analysis.py      readiness / soft slots / blockers    context.py   card + prompt assembly (no LLM)
    persistence.py   session store: .requivo layout, revisions, migrate_legacy, atomic writes
    validation.py    validate_proposal → structured errors  errors.py  RequivoError (+ .to_dict())
    dependencies.py  the dependency DAG: propagate / diff_models / diff_reasoning
    integrity.py     does a session directory tell the truth about itself? evidence is the
                     directory and only the directory: nothing outside becomes a verdict (a lost
                     context card is an environment finding), and nothing inside aims a filesystem
                     call outside (a recorded artifact filename is untrusted input)
    adapters.py      epic_export + GitHub/GitLab tracker plans
  usage.py         the provider-neutral API-spend ledger — records carry the rate they were billed at
  http.py          RequivoError code → HTTP status classification (#422); framework-free like
                   paths.py/streams.py/usage.py above, so it is importable with no extra installed —
                   web/app.py imports only http_status_for (as its one remaining private alias,
                   _status_for); STATUS_BY_CODE/UNCLASSIFIED_STATUS live here and nowhere else
  providers/       the only LLM callers
    base.py          ReasoningProvider protocol   errors.py  EngineError (no SDK, no vendor)
    anthropic/       the one implementation, split by cohesion (#74)
      client.py        the SDK handle + the optional-import guard + the model id
      pricing.py       the dated rate tables + `price_call`, which stamps a rate onto a record
      completion.py    _complete: the retry loop, JSON extraction, truncation check, usage recording
      generators.py    the discovery turn, the seven generators, _GENERATORS / _OP_PROMPTS
      provider.py      AnthropicProvider
  services/        the shared seam
    sessions.py      SessionService (create / update_model / diff / status)
    artifacts.py     ArtifactService (save with source revision / list / mark_stale)
    repository.py    SessionRepository protocol + FileSessionRepository (Postgres-swappable)
    discovery.py     DiscoveryService — reason → apply → save (+ draft_turn), shared by CLI + Web
  render/          views (data → str/stdout, no side effects)
  cli.py           the `requivo` CLI: the *journey* verbs, in the order a user meets them (demo →
                   discover → refine → generate → web) — most call the provider, and three do not
                   (status, demo, impact: pure reads over a model or the bundled example, no client
                   ever built). The axis here is the journey, not the API call; deterministic/ below
                   is the other axis, and the two cut across each other rather than nesting (#296)
  deterministic/   the *plumbing* verbs — session/model/artifact CRUD and install diagnostics, one
                   module per axis of *that* split (#73); every verb here happens to be no-LLM, which
                   follows from being plumbing rather than being the split's own boundary.
                   `register(sub)` is the single seam `cli.py` binds through and it names its four
                   halves, so a module that stops registering is an ImportError rather than a quietly
                   shorter `--help`
    __init__.py      the module docstring + `register(sub)` = register_doctor/_sessions/_model/_artifacts
    _shared.py       what more than one verb module needs — input, `print_json`, `EXIT_DEGRADED` —
                     and the membership rule that keeps it from becoming a second deterministic.py
    doctor.py        doctor / schema / context: the verbs that answer for the install, not a session;
                     owns card health + the two remedy hints, which `session verify` imports
    sessions.py      session init / list / show / migrate / export / verify / import
    model.py         model show / validate / apply / diff       artifacts.py  artifact save / list / show
  web/             Requivo Web — FastAPI + Jinja2 + HTMX over the services (the `[web]` extra)
    app.py           create_app()   security.py  cross-site guard   routes/  viewmodels/  templates/
    example.py       the bundled sample as a real session — the keyless activation path (#226); it
                     owns the policy (second click, provenance, how a sample is recognised) so the
                     route stays a redirect and a second surface could reuse it
    viewmodels/labels.py  the user-facing vocabulary, in one table (see "Two vocabularies" below)
plugins/claude-code/   the Claude Code plugin (skills + manifest) — NOT shipped in the wheel
```

Assets (`prompts/`, `framework/`, `context/`, the demo payload) live **inside the package** at
`src/requivo/assets/`, so they ship in the wheel and a `pip install` works outside a clone. Sessions
are written to `.requivo/sessions/<slug>/` under the caller's **workspace** (cwd, or
`--workspace`/`REQUIVO_WORKSPACE`), never inside the install. The retired `./out` root is opened by
nothing but `requivo session migrate`.

## Invariants

These are the rules a change must not quietly break. Each one exists because breaking it produced a
bug that looked like correct behaviour.

1. **Staleness is the dependency graph, never the revision number.** An artifact is stale when
   something it rests on changed — not because the session moved past its source revision. The source
   revision is *provenance*. Report `ArtifactStatus.stale`; never infer staleness by comparing
   revisions. Two edge sets feed it: the slots an artifact consumes (`ARTIFACT_SLOTS`) and the
   reasoning layer (`REASONING_CONSUMERS` — every generator, since each is prompted with the full
   model, so `diff_reasoning` invalidates on its own). Reasoning a turn merely *omits* is not a
   removal — but that is resolved *before* the diff, by `ModelProposal.resolve`, not inside it (see
   invariant 10). By the time two models reach `diff_models`/`diff_reasoning` both are complete, so
   the diff is symmetric: an empty collection facing a populated one is a real deletion.
2. **A generation carries the revision it read.** Provider calls take seconds to minutes and the
   session can move underneath them. Capture `current_revision` before the call; pass it as
   `expected_revision` on any apply and as `source_revision` on the artifact write. Saving against an
   older revision stays legal — `ArtifactService.save` then computes freshness against the current
   model rather than assuming it. Never record `stale=False` because the caller didn't say otherwise.
3. **Refuse, don't truncate; refuse, don't filter.** Over-long input is rejected, not cut — half a
   request reads exactly like a whole one. An unknown context card is an error, not something to drop:
   dropping it leaves an empty selection, and an empty selection means *every* card.
4. **Boundary contracts are strict.** Everything an LLM fills inherits `StrictModel` (`extra="forbid"`).
   A field the model invented must fail loudly and ride the retry loop, not be silently discarded.
   *Completeness* rules (the full required slot set, a non-empty objective) live at the discovery
   boundary instead, because a partial `EngineOutput` is a legitimate internal object.
5. **Reasoning items have content-derived ids.** `DesignDecision`, `Challenge` and `Opportunity` carry
   an `id` recomputed from their own text on every validation. Never trust a supplied one.
6. **Provenance is real or absent.** Each revision records provider, model, surface and a hash of the
   exact prompt it was reasoned against. Don't add a provenance field you do not populate.
7. **Core stays provider-free, and talks to its caller rather than to the process.** It may read and
   write files — `persistence.py`, `context.py`, `contracts.py` and `analysis.py` all do, by design;
   *IO-free*, which this invariant used to say, was false as written and a guard against the literal
   wording would have to fail on correct code. What core may not do is import a provider or the
   Anthropic SDK, and may not touch **argv, the standard streams, the environment, or process exit** —
   `cli.py`/`deterministic/` own argv and stdout, `paths.py` owns the environment. `logging` is
   fine; it is the library-correct way of *not* printing. `tests/test_boundaries.py` enforces both
   halves, walks `core/` recursively, resolves relative imports, and **fails when its scan set is
   empty** — a glob over a directory that no longer exists returns `[]`, and `assert not []` is an
   all-clear nobody earned. Each rule in that file carries the reason it is there, so the next person
   argues with a line instead of deleting the file.
8. **The session format and the `--json` outputs are public.** `.requivo/sessions/` is the interface
   between every surface, at `format_version` 1. Adding a field is free; renaming or repurposing a
   *populated* one needs a version bump and a migration in `migrate_session()`. A frozen 0.8.2
   `session.json` in `tests/test_sessions.py` pins the backward-compatibility half of that promise, and
   `docs/compatibility.md` is the written contract — update it in the same change, not later. Forward
   compatibility is the other half: persisted models are `extra="allow"`, so a field from a *newer*
   Requivo survives a round-trip through an older one. Retiring a key is explicit, in `_RETIRED_KEYS`.
   That held for `session.json` and, until #14, was simply false for `model.json`, which was read
   through the *provider* contract and therefore refused an unknown key outright — so the promise
   most likely to be believed was the one nothing enforced. The two directions are two contracts
   now: `StrictModel` for what an LLM fills (invariant 4), `PersistedEngineOutput` for what is read
   off disk. They disagree on purpose; the block at the foot of `contracts.py` says why, and
   `test_the_persisted_contract_is_permissive_all_the_way_down` fails if a nested contract gains a
   strict-only twin, with `…_copies_every_constraint_it_restates` beside it for the other half of a
   field's contract — a mirror **must** restate `Field(...)` (pydantic drops the parent's `FieldInfo`
   on re-annotation, which would silently make the field required), so shared limits live in a
   constant like `MAX_QUESTIONS` and the graph guard is what stops them drifting.
   **Whatever reads a persisted model must read it permissively — including `integrity.py`**, or
   `doctor` reports a defect in a session the loader opens without complaint.
   And a permissive read is only half: **whatever writes one must preserve what it could not name.**
   Reading alone left `resolve()` dropping the key on the next turn — the visible refusal traded for
   a silent loss, which is the worse of the two. See invariant 10.

   **The same rule reaches past fields, and the second place it bit was an artifact *type*** (#260).
   This page lists "a new artifact type" among the changes needing no bump, and `check_session_dir`
   made an `artifact_status` key it had no generator for an integrity *problem* — so `session verify`
   exited non-zero, `doctor` called the session inconsistent and `session import` refused a
   colleague's archive, over a session `read_meta` opened without complaint. Identical shape to the
   `model.json` case above, one file along, and it would have fired on the first new generator ever
   shipped. Such a type is a **note** now: reported, blocking nothing.
   `test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect` is the guard.
   The generalisation worth carrying: **a diagnostic must be at least as permissive as the loader**,
   about every vocabulary the format lets a later version extend, not only about keys.
   Tolerating is still not trusting (invariant 14) — the type has to *look* like one
   (`_ARTIFACT_TYPE_RE`, `MAX_ARTIFACT_TYPE_LENGTH`) or it is refused as `unsafe_artifact_type`,
   because accepting an unknown key is accepting what `session import` used to hold shut, and every
   other check on that entry still runs.
9. **A precondition is held across the writes it authorises.** `save_revision` checks
   `expected_revision` and then writes more than once; without `session_lock` around both, two writers
   pass the same check and the second overwrites the first — the check reads as protection while
   providing none. Every compound mutation runs under `repo.lock(slug)`, taken by the service so the
   whole sequence is one unit. The lock is re-entrant per thread, and OS-held, so a crash releases it.
   Any new multi-step write goes inside it; any scratch file gets a unique name.

   **A lock is a claim on an inode; the writes under it are by pathname, and a rename separates the
   two.** The lock lived at `<slug>/.lock`, inside the directory `session import --force` renames.
   So a writer inside `save_revision` went on writing into the *freshly imported* session, and a
   third process opening the lock found a different inode and acquired it — the check reading as
   protection while providing none, one layer below where this invariant first found it. Holding the
   lock across the swap was not available either: an open handle inside a directory is exactly what
   Windows refuses to rename (#112, four legs, every time). The lock is at
   `.requivo/locks/<slug>.lock` now — outside every session — so the swap is an ordinary compound
   write and `os.replace` sees no handle. The general form: **a lock that lives inside the thing it
   guards guards nothing the moment that thing can be renamed.** Pinned by
   `test_a_forced_import_serialises_against_a_concurrent_writer`; the cost, an older Requivo in the
   same workspace locking a different file and not serialising against a newer one, is in
   `docs/compatibility.md` rather than here, because that page is where every other "two versions,
   one workspace" promise lives (#113).
10. **A proposal is not a model, and silence is not deletion.** What a surface sends is a
    `ModelProposal`: the slots are complete (an apply *replaces*, so a partial one is refused, never
    merged), but `decisions`/`challenges`/`opportunities` are tri-state — absent means "not speaking
    to it", `[]` means "delete". `resolve(current)` collapses the three states against the model being
    refined, and it is the *only* place that happens: `validate_proposal(…, current=…)` for every
    apply, and the provider's `run(…, carry_from=…)` for a turn it reasons itself. Read as an
    `EngineOutput` instead, an ordinary refinement turn — which `engine.md` never asks to re-state the
    brief — deleted every decision the assessment had produced, silently.
    A key this version cannot *name* is a fourth thing the proposal cannot speak to, and `resolve()`
    carries it for the same reason (#14): a `ModelProposal` is `extra="forbid"`, so its silence about
    a field a newer Requivo added is not a decision to delete it. That is why the reasoning
    collections carry `SerializeAsAny` — a carried-forward item is a permissive instance under a
    strict annotation, and pydantic serializes by the annotation — and why `resolve()` returns a
    `PersistedEngineOutput` when the model it refines holds a top-level key it could not name.
11. **Creating a session is one atomic claim on its slug.** `create_session` assembles the session in a
    staging directory and renames it into place; the rename either wins the slug or raises
    `SessionExistsError`. Never decide with a preceding existence check — two concurrent creations both
    pass it, and the second overwrites the first's identity, provider and context cards. Identity is
    the request **and** its context-card selection: same request, different cards is a different
    discovery, because the cards are what the impact estimates are read against.

    **A claim is only decidable while nothing else can make a directory of that name**, so
    `create_session` is the only producer of one. `session_lock` used to be a second: it created the
    session directory in order to put `.lock` inside it, so locking a slug with no session left a
    directory that `list_session_slugs` cannot see and the rename cannot win — a refusal naming a
    session nobody had created. It refuses such a slug now rather than materialising one, which is
    also why a *failed* lock has to leave the store as it found it (#22). The refusal stays as
    policy, and the structural hazard behind it is gone: since #113 the lock file is at
    `.requivo/locks/<slug>.lock`, so `session_lock` cannot produce a directory under the session
    root even in principle.

    **That stopped new ones and found none of the ones already on disk**, which nothing could see:
    `list_session_slugs` filters on `session.json`, and `doctor` and `session verify` both reason
    over the slugs it returns, so the only symptom was the next `create_session` on that name losing
    its rename and landing under `<slug>-<hash>` with nothing saying why. `scan_session_root`'s
    second part is the other half of that one predicate, beside `list_session_slugs`; it answers
    both from **one** listing, for the caller that asks both, because two scans are two instants and a
    `session.json` landing between them puts a name in neither answer. `doctor` reports it — a
    **report, not a repair**, describing what is there and never concluding what it is, because a
    half-extracted archive and a leftover lock are the same shape, and invariant 14's rule is that
    the evidence is the directory and only the directory. A symlink is named as one and not followed;
    `slug_shaped` goes through `validate_slug`, since validity is the pattern *and* the length (#67).
12. **A provider call reasons from one snapshot.** `SessionService.snapshot(slug)` reads the revision,
    the model, the request and the cards under the session lock; `run_discovery`, `answer`, `generate`
    and `reason` all take one. Reading the revision and the model separately yields revision N with the
    model of N+1 when a write lands between them — the artifact is then generated from one model and
    filed against another, undetectably, because the recorded number is plausible. The lock is released
    before the call (which takes minutes); `expected_revision` handles what happens *after*, the
    snapshot handles what the call started *from*.

    **An *analysis* that is two calls is still one snapshot** (#135). `estimate` reasons the stories
    and then the estimate *against* those stories, and it took a snapshot each — "two reads, two
    instants", this invariant's own sentence, one layer up from the case it was written about. Nothing
    is written there, so no provenance can become a lie; what drifts is the answer, which shows both
    halves side by side and names no revision. `reason_from` takes the caller's snapshot for exactly
    this, and the snapshot carries its own slug so the two cannot disagree. Pinned by
    `test_the_estimate_verb_reads_stories_and_estimate_from_one_snapshot`.
13. **A first discovery only lands on revision 0.** Discovery reasons from the request alone — it never
    sees the current model — so running it on a refined session discards that work rather than
    improving it, with the optimistic lock satisfied throughout (it reads revision N and writes against
    N). `_require_revision_zero` is the gate, taken *before* the paid call, by every entry point. A
    rule that lives in an interface (the Web hides the button after revision 0) is not enforced.

    **"Before" and "every entry point" were aspirational for a release, and on the path a person
    actually uses** (#133). `--once` claimed the session inside `start()` and refused for free; the
    CLI's interactive loop reached the gate only through `finalize_discovery`, after up to nine paid
    calls — eight turns plus the assessment — all of them thrown away by a refusal that was correct
    and in the wrong place. This is the same shape as the sentence above it: a rule the documenting
    path takes and the used path does not is not enforced either. `claim_session` is public for that
    reason — a surface that owns its own loop has to be able to take the gate itself.

    The cost used to be that an abandoned interactive discovery left the request captured at
    revision 0 — better than nothing, and still a discarded paid turn every time. Since #202 a stop
    keeps what it bought: the loop hands back the model it had drafted, `_cmd_discover` applies it,
    and the session lands where `--once` already landed, at revision 1 with its questions open. So
    the gate now *fires* on a re-run of that request, which is the accepted cost in its place, and
    the refusal is a signpost rather than a dead end because it already names both ways on — refine
    with `requivo answer`, or discover under another slug. That half is pinned by
    `test_stopping_early_keeps_the_turns_it_paid_for`; the gate's own position is pinned by
    `test_both_discover_entry_points_refuse_a_refined_session_before_paying`, whose assertion is the
    *call count*, since a test asserting only the refusal was green on the defect. Two claims, two
    tests, and citing the second for the first was #320: it passes against the un-fixed code, so the
    sentence had a reference that resolved and guarded nothing — which `test_narrative_references.py`
    cannot see, because it checks that a name exists and not that it fires.
14. **The service layer is the integrity boundary, not the interfaces.** Context cards are resolved in
    `create_session`, not trusted from the caller, because the CLI and the Web being careful is not a
    guarantee — an external consumer can call the service directly. For the same reason `DiscoveryService`'s
    artifact service defaults to the *session service's* repository: on files a split backing is
    invisible and every call succeeds.

    **And creation is not the only door.** `session import` writes a `session.json` this project never
    resolved — deliberately, because refusing a colleague's archive for want of one of their cards
    would be wrong, and `integrity.py` is right not to turn a card living outside the session
    directory into a verdict. So the resolution above is a guarantee about *creation*, never about the
    value on disk: a persisted `context_cards` is untrusted input every time it is read back. What
    holds the second door is a guard where the value is *interpreted* — `normalize_tokens`, which
    every card selector passes through — not the resolver. Read as covering both, this invariant
    promised something it does not, and a stored card name spent a release able to forge a line at
    column 0 of `doctor`'s own output (#40).
15. **A listing survives its own members.** `session_list` renders every session on the home page, and
    a session at revision 0 has no model — `status()` raises for it. Letting that propagate turned one
    un-analysed session into a 404 for the *whole* list, hiding every other session behind it. Any
    aggregate view catches per-item failure and degrades that row, never the page.

    **The guard belongs above the rows, not around them.** `SessionService.list_entries()` is the
    degrading read — `list_sessions()` is a single comprehension over `read_meta`, so an unreadable
    `session.json` raises before any row exists to degrade. Both aggregates now use it: the home page
    (#7) and `requivo session list` (#62), which had the identical duty and no guard at all for a
    release. Where a row makes further reads of its own, as the web row does with `request_text` and
    `status()`, it carries a bare `except Exception` too; the CLI row reads nothing past the metadata
    and deliberately carries none, because a guard that provably cannot fire is worse than none.

    **And there is a layer below the rows, which is where it broke next** (#80). `list_entries` can
    only degrade a row it *has*; the scan that decides what the rows *are* is underneath it, and it
    could raise too. `_scan_session_root` partitioned the session root with
    `(p / "session.json").exists()`, and `Path.exists()` re-raises EACCES — so one directory the
    process could not stat into aborted the partition for every entry, and `session list` exited 1
    with an empty stdout and a traceback. The fix is not a fourth `except` around the aggregate: it
    is that **the predicate has three answers**, because it can fail. An entry it could not decide
    about goes in neither of the other two buckets — `others` hides it from every listing, which is
    #67's defect one function along, and `slugs` claims it is a session, which is the one thing the
    failed probe did not establish. It reaches `list_entries` as a third source of rows and renders
    as a degraded one. The general form: **a guard above the rows is only as good as the scan that
    produced them**, and a partition whose predicate can raise has three outcomes whether or not its
    return type says so.

    **Three outcomes, and the third is the point.** A degraded row names its session and states no
    fact it could not read — no timestamp, no revision, no question count. *Could not be read* and
    *not analysed yet* must render differently. On the CLI that third state also has an exit code of
    its own, `EXIT_DEGRADED` (4), because 0 says nothing is wrong and 1 says there is no answer.

    **That code names a shape of answer, not a verb** (#86). It was `EXIT_DEGRADED_LISTING` and read
    as belonging to one command; `session verify` then reached the same state from the other side —
    it could not read a session's product context, which is *not an answer* — and exited 1 beside a
    session that really is inconsistent. Both are 4 now. A new code per verb rebuilds the collapse 4
    exists to undo. Where a verb can produce both at once, the firm negative wins: a session that is
    inconsistent **and** whose cards were unreadable exits 1, because a complete answer outranks a
    partial one.
16. **Text is UTF-8 on both sides, and a renderer cannot kill the process.** Every text read and write
    names `encoding="utf-8"` — the default is the *locale's* codec, so a file this project wrote as
    UTF-8 decodes as cp1252 on Windows and the round-trip corrupts while still validating: mojibake in
    the PRD, and `integrity.py` rehashing the mis-decode to accuse the user of editing a file nobody
    touched. It was 28 reads and one write, not reads alone.
    `tests/test_encoding.py` is the guard, because 29 call-site fixes leave the 30th; it walks
    `src/`, `scripts/` and `tests/`. The `tests/` half is deliberately asymmetric and the asymmetry is
    the lesson: **every read** must declare its codec, because the hazard is in the file being read and
    no literal in the source reveals it — that is how the first Windows leg went red on
    `test_demo_payload_matches_the_browsable_example`, reading a bundled asset the product itself
    reads correctly. A **write** is checked only when its content literal is non-ASCII, because there
    the literal *is* the hazard and is fully visible. Two sites read with the locale default on
    purpose and are exempted by name with a reason, pinned by a test that fails when an exemption
    stops naming a real function. A file the **user** names is the one exception worth knowing: it is
    still read as
    UTF-8, but refused by `read_user_text` with a structured error rather than a traceback.
    Output is the mirror image, and the ordering is the whole point: `streams.py` reconfigures stdout
    and stderr once, from every entry point that prints, with `errors="backslashreplace"` — never
    `replace`, because a reader cannot tell a substituted character from one that was never there.
    *Every entry point* was `cli.app()` alone for a release, and the two scripts that **measure** the
    product printed sixteen glyphs outside it (#164): `golden_run.py` spends real API calls and
    writes its baseline before it reports on it, so a cp1252 console turned a completed capture into
    a traceback standing where a result should be. `golden_lib.configure_output()` is the one call
    both take, and it carries the third state rather than an exit code of its own — a stream
    `configure_stream` could not reach is named on stderr, because that is exactly the stream that
    can still crash. `test_a_harness_script_survives_a_console_that_cannot_encode_its_output` and
    `test_a_strict_console_kills_a_harness_script_that_does_not_configure_its_streams` are the pair.
    A glyph must not be able to kill a process **after** the mutation it was reporting has landed;
    the `UnicodeEncodeError` arm in `app()` exits `EXIT_RENDER_FAILED` (3) and says the work is done
    rather than letting a traceback imply it is not (#11, #29). The harness scripts deliberately have
    no such arm: `golden_diff` neither calls nor writes anything, so a guard there could never fire,
    and `golden_run` has already written each request's baseline by the time a summary could die.
17. **A guard's verdict must not depend on transient filesystem state.** `_child_of` decided whether
    `root/slug` escaped the session root by comparing `d.resolve()` with `root.resolve()` — two
    independent resolutions, of paths where one is derived from the other, each reflecting the tree at
    the instant it ran. Another thread creating a directory in that window made them disagree, so
    `canonical_dir("s")` raised `InvalidSlugError` — *you gave me a bad slug* — about a valid slug,
    because somebody else was creating a session. Reproducible on POSIX with a symlinked parent, and
    observed on the first Windows CI leg as four of twelve concurrent creators crashing. Two siblings
    had the identical shape and were found by sweeping the class rather than the instance:
    `artifact_path`, and `integrity.py`'s artifact containment check — where a spurious disagreement
    reported `unsafe_artifact_filename` about a perfectly bare name, i.e. the verb that answers *is
    this session intact* accusing the user again. All three now resolve **only a path that is actually
    there**, which is the only
    case that can fail: `validate_slug`/`validate_filename` already make a separator or a dot segment
    unrepresentable, so the sole escape is a symlink at the target, and an absent path is not one —
    `exists() or is_symlink()`, never `exists()` alone, because `exists()` follows the link and a
    dangling symlink out of the root is precisely the case to catch. The three are now **one**
    function, `core/persistence.py`'s `is_contained`, because each had to be corrected separately
    for this and then again for its sequel, below.

    **Nor may it depend on where it runs.** The sequel: the decision was made with `Path.resolve()`,
    which on Windows under CPython 3.9 asks `nt._getfinalpathname` — a call that has to open the path
    and therefore fails on a link whose target is missing, after which the non-strict branch re-joins
    the unresolvable tail to the parent it could resolve. A dangling symlink then resolves to its own
    location and reads as contained, so the guard was off on that one leg of thirteen while the other
    twelve were green. Two things hold it now, and the second is the one that matters: `_resolve` is
    `os.path.realpath`, never `Path.resolve()` — realpath reads the reparse point itself, and its
    `strict=` keyword is 3.10+ and must not be reached for — and `is_contained` **refuses a symlink
    whose resolution comes back equal to its own location**, because a symlink never legitimately
    resolves to where it sits, so that equality is the resolver saying *I could not look*. Refusing
    there is the third state, and it is what takes the guarantee off the platform entirely.
    `_blind_to_dangling_links` in `tests/test_integrity.py` gives every leg the 3.9 semantics so the
    class is caught on Linux; it patches `store._resolve` and **not** `Path.resolve`, which is where
    it was first installed — a simulation aimed at a function the code no longer calls is a test that
    passes for a reason unrelated to its name. The general form has two halves now: a check that can
    answer differently for the same argument depending on **when** it runs is not a check, and
    neither is one whose answer depends on **whether the platform happened to be able to look**
    (#3, #11).
18. **`_atomic_write` retries a denied rename, briefly and only that.** On Windows `rename` is
    `MoveFileEx`, which fails with `PermissionError(13)` whenever anything holds a handle to the
    destination — an antivirus scanner or the Search Indexer, opening a file microseconds after it is
    written, neither of which this process can serialise against. Losing a completed write to a scanner
    is not acceptable for the durable product, so the replace is retried — 8 attempts, 280 ms of
    backoff in total, both bounded by `_REPLACE_ATTEMPTS`/`_REPLACE_BACKOFF_S`. Narrow on purpose:
    `PermissionError` only, then the original is re-raised, so a genuinely unwritable destination
    still fails loudly and fast. This is the one place in the
    store where retrying is right rather than a way of hiding something — the operation is idempotent
    and the cause is external (#3).

## Where a bug narrative lives

Every invariant above exists because a plausible assumption produced a bug that looked like correct
behaviour, and the surrounding code says so at length. That density is deliberate and it is not
archaeology: the person about to simplify a subtlety away is *in the editor*, not in a docs folder,
so a pointer they will not follow is strictly worse than the paragraph it replaced.

An external review proposed moving all of it to decision records. That remedy is rejected and its
diagnosis is not (#75). The rule instead:

> **A comment paragraph that recounts a past bug must be backed by a test that goes red when the
> guard is removed.**
>
> - **If it is** — the paragraph belongs in that test, and the call site keeps one line: the
>   invariant, the cost of breaking it, and **the name of the test that enforces it**.
> - **If it is not** — it is either a missing test, which gets written, or genuine archaeology,
>   which goes to `docs/decisions/`.

It is a rule and not a preference because it is mechanically decidable. *Reduce the archaeology* has
no stopping condition and two readers will disagree about it forever; *is there a test that goes
red?* has one answer per paragraph. And it converts density into coverage: nothing is deleted, every
long comment either becomes a test or moves, and what survives sits on a support that cannot be read
in diagonal. A comment can be skimmed; a red test cannot. `tests/test_boundaries.py` and
`tests/test_encoding.py` had already found this answer without stating it.

**The reference is a name, never a path** — a test function, a test module, or a decision record's
slug. Paths in this repository move: the package was renamed once (`product_copilot` → `requivo`),
`deterministic.py` became a package, and a 2147-line CLI test module became seven files, all inside a
fortnight. A name survives every one of those and a path survives none — and the guard below made
that point on this very paragraph, which first cited that module by its dead filename. **And it must be greppable**,
because selecting it and grepping is the only way anyone uses one — so an identifier is never split
across a line wrap. Two of the sixteen references then in the tree were, and were unfindable while
naming tests that really existed.

`tests/test_narrative_references.py` is the guard, and it checks only what is mechanical: that every
reference resolves, and that none is broken by a wrap. Whether a given paragraph *should* carry a
reference is a judgement, stated here for a person to apply — a test that enforced it would be
guessing at intent.

**The same test applies to a count** (#134). This file said the suite was *324 tests* while it
collected 687, and correcting the number buys one release: every lane that adds a test invalidates
it again and nothing goes red when it does. So a count in prose has to answer *what does a reader do
with this?* — and if the answer is nothing, it comes out rather than getting a guard. The exact size
of the suite is not a fact anyone acts on; *no API calls, no network, no build step* is. A count that
does earn its place gets a test, the way `tests/test_version_sites.py` exists because an unguarded
README badge sat fifteen releases stale.

**What this is not.** Not a licence to remove a reason attached to a guard, or a MUST-FIRE note:
those *are* the invariant rather than the story around it, and they stay at the line. Not applicable
to this file, to `docs/`, or to the invariant list, which are already narrative's right home.

**A new source-scanning guard *tier* is not free, and #288 is where that got measured rather than
assumed.** Three of the existing tiers (`test_boundaries.py`, `test_encoding.py`,
`test_narrative_references.py`) had each grown their own `scan`/`_parse`/empty-root-refusal from
nothing, which is how the same ten-line function came to exist almost identically three times before
anyone shared it (`tests/_scan.py` is that fix). The bar for the *next* one is the same the codebase
already applies to itself elsewhere: a plausible first instance does not justify a new guard tier on
its own, any more than one context card colliding with its neighbour justified automatic relevance
routing — see the golden harness's "Known limit" note. **Two real instances of the drift a new tier
would have caught**, named by issue number, is what funds a third scanning implementation; short of
that, extend an existing tier's scan set (as #355 did for `providers/`) rather than starting a
fourth.

**#287 widens that bar from "a scanning tier" to every prose/CI/script guard that does not exercise
shipped runtime code, because the same trajectory shows up one level up.** The issue's own filing
quoted a 2026-08-29 count that had already drifted the day it was written — `test_agent_layer.py`
tripled that same day, in the commit that stopped the tracked `.claude/settings.json` configuring
every contributor — so the figures below are re-measured directly against this repository rather than
carried forward from the issue: `test_narrative_references.py` (441), `test_version_sites.py` (506),
`test_workflow_untrusted_output.py` (673), `test_workflow_permissions.py` (207),
`test_agent_layer.py` (552), `test_vocabulary_boundary.py` (113), `test_dependency_floor.py` (153),
`test_plugin.py` (353), `test_plugin_cli_drift.py` (1,153) and `test_golden_{lib,readout,capture}.py`
(967) — 5,118 lines, ~20% of the 26,186-line suite — guard the repo's own self-description: comment
references, version strings, CI YAML, `.claude/` inertness, asset wording, plugin-doc drift, a harness
script. `test_boundaries.py` (1,167) and `test_encoding.py` (1,080) — another 2,247 lines, ~9% — guard
source *form* (an import, an encoding declaration) rather than behaviour. Each one is incident-backed
and individually defensible, same as every scanning tier above. The risk is the trajectory, not the
estate: this culture adds a guard per incident, and an incident in prose is cheap to have, while
nothing automated fails when the engine's questions or artifacts get materially worse —
`docs/product-validation.md`'s own verdict, "well tested and under-validated," which #169 exists to
fix and this file does not. Re-measure before citing this line again — a suite this actively guarded
moves fast enough that even a same-day count can already be wrong.

**The meta-guard estate is at budget.** A new prose/CI/script guard needs the same two-named-instance
bar #288 already applies to a scanning tier, one level up: name two real instances of the drift it
would have caught, by issue number, or it is a taste rather than a budget line. Folding into an
existing meta-guard file is preferred over opening a new one — most of the files above already hold
more than one concern, and a new file is a new standing cost every future run pays, kept or not.

**The next testing investment is #169, not another guard.** Running the product-validation protocol
and recording its findings as the baseline is a judgment about the product that no meta-guard can
stand in for, and it is the gap this repository has actually been missing.

## The persistence diagnostics tier is frozen

A parallel drift, in the store rather than in the test suite: the report-only diagnostics in
`core/persistence.py` and `deterministic/doctor.py` —
`NonSessionEntry`/`UnexaminableEntry`/`_describe_non_session`/`scan_session_root`/`list_*` (~260
lines) and the lock-residue scan (`scan_lock_root`, `_lock_health`, ~110 lines together) — have grown
faster than the states they report actually occur. Each addition is well built and each mints public
`--json` surface that `docs/compatibility.md` then makes expensive to remove, so the tier only ever
ratchets outward. Meanwhile the residue that genuinely accumulates — a dot-prefixed scratch file left
behind by any compound write hard-killed mid-write, from `create_session`'s staging directories and
`_swap_in`'s `.replaced` backups to `session restore`'s own `.model.json.<pid>.restore.tmp` (#210,
landing beside this paragraph) — is reported by nothing, so the tier's coverage was never even
aligned with what actually piles up, and every new writer keeps adding another unreported instance of
the identical shape (#287).

**No new report-only diagnostic lands in this tier without a reproduced field instance of the state it
reports, and it must name what a user should do about it** — a residue with no action is not reported.
This is a decision, applied once and retroactively rather than as a deletion: the shipped lock-residue
check stays (its `--json` is already public, and removing it would be the breaking change, not keeping
it — see [compatibility.md](docs/compatibility.md)), and the tier stops **here**. If a diagnostics
change is ever wanted again, the stale dot-prefixed staging/backup class above is the one actually
worth adding — it is the one this paragraph can point at a real, reproduced instance of, which is
exactly the bar every other addition to this tier must now clear too.

## Two vocabularies, one meaning

The engine's vocabulary is precise: slots, evidence, coverage, artifacts, staleness, revisions. It is
the right one for `core/`, for `--json`, for `docs/` — and the wrong one for a first screen, because it
asks a reader to learn the model before they can use the product.

So the Web speaks a translation of it, defined once in `web/viewmodels/labels.py` and in
`viewmodels/status.py` (*what we know* / *what we are assuming* / *open question* / *needs updating* /
*are we ready?* / *decision brief*). Two rules keep this from becoming a second model:

- **Translation only, never computation.** A view model relabels and *selects* (which five questions
  lead the page); it never re-derives readiness, coverage or a blast radius. `impact_view` reshapes an
  `UpdateResult`; it does not recompute one, and it must never ask the provider — a generated list of
  documents needing an update is a plausible guess where a computed one is an answer.
- **Nothing stored changes.** `brief` is still `brief` on disk, in the CLI verb, in the contract and in
  `session.json`; only the caption reads "Decision brief". Renaming a persisted key to change a label
  would cost a format bump for a word.

The primary screen shows what a reader must act on; everything else lives behind *Traceability
details*, complete and one click away. Hiding is presentational — the counts are always stated, so it
is never possible to mistake a short list for the whole list.

## The runner

1. `build_prompt(name, only)` loads a prompt file and substitutes `{{SCHEMA}}` (the slot definitions)
   and `{{CONTEXT}}` (`load_context()`, which concatenates every `context/*.md` except `_`-prefixed
   ones). `prompt_version()` hashes exactly this string — that hash is what lands in the revision log.
2. Every reply must be **JSON only**. `_complete()` is the shared call: it concatenates the response's
   text blocks, strips a fence or slices `{ … }`, and validates against a Pydantic contract. On
   malformed or non-conformant JSON it retries (2× by default) with a corrective nudge in a *local*
   message copy, so the caller's history stays clean. An optional `validate` hook rides the same loop
   for semantic checks. Transport failures and truncated replies surface as a clean `EngineError` —
   never a traceback. The output ceiling is `MAX_OUTPUT_TOKENS` (16k; the call is non-streaming, and
   the SDK risks HTTP timeouts above that). Truncation is checked **parse-first**: a reply flagged
   `max_tokens` whose JSON is nonetheless complete still succeeds.
3. The `system` prompt carries a `cache_control: ephemeral` breakpoint **only when its caller will
   send it again** (`_complete(..., reuse_system=)`). It pays across the calls of *one* operation — a
   golden capture's K runs, `converse()`'s turns — and cannot pay across operations, since
   `build_prompt` substitutes the shared schema+context into a per-op template that places them near
   its *end*: the shared bulk is a suffix, caching is a prefix match, so no breakpoint placement lets a
   second operation hit a warm entry. A write costs 1.25x input and a read 0.1x, so a one-call verb
   that cached was paying a flat ~25% surcharge (#9). The accepted cost: a one-call verb that hits the
   JSON **retry** loop re-sends the identical prompt and is no longer cached, paying 2.0x where it used
   to pay 1.35x — the better bet only while a retry is rarer than ~1 call in 4, which it is. Keep the
   prompt byte-identical per call or the cache is lost where it does pay. `_complete()` records per-call usage into a session-scoped
   `UsageLedger` (`requivo.usage`, provider-neutral); `render_usage()` prints it (tokens are exact,
   cost is a labelled estimate). The rate table with its expiry-aware launch pricing stays in
   `providers/anthropic/pricing.py`, and `price_call` stamps the rate **onto the record as the call
   is filed** — so the ledger holds arithmetic rather than a price table, and an estimate spanning a
   price change is right on both sides of it. Every exit of `_complete()` records before it raises:
   a failed call is still billed (`test_a_failed_call_is_still_recorded_on_every_exit`).

**Consequence for changes:** behaviour is tuned by editing the Markdown/JSON assets, not the Python.

## The output contract (keep in sync)

Each stage has a Pydantic contract that must agree with its prompt's "Output format" block:
`ModelProposal` ↔ `engine.md` (the reply is a *proposal*; `EngineOutput` is what it resolves into —
see invariant 10), `Brief` ↔ `brief.md`, `Stories` ↔ `stories.md`, `EstimateDraft` ↔
`estimate.md`, `PRD` ↔ `prd.md`, `AcceptanceCriteria` ↔ `criteria.md`, `Epic` ↔ `epic.md`,
`ReleaseNotes` ↔ `release.md`. Slot ids live in `framework/model_schema.json`, which also carries each
slot's `pillar` and `label` (read back by the renderer via `slot_meta()`).

The *agreement* between each prompt and its contract is no longer kept by hand — the enumeration
above still is, and nothing checks those eight names against the registries, so read it as a map
rather than as a guarantee. `tests/test_prompt_contracts.py` parses each prompt's Output format
example offline and validates it against the contract that operation actually parses replies
with — read out of the generator's own `_complete(...)` call rather than from a table here, so a
generator pointed at a different contract goes red too. It exists because the drift was invisible and
paid: a model obeying a stale example produces a reply `extra="forbid"` refuses, `_complete()` retries
twice with a corrective nudge and then raises `EngineError`, so one operation costs up to three calls
every time it runs while the offline suite stays green (#266). What it deliberately does **not**
check is an *optional* contract field the prompt was never told about — the three derived `id` fields
are legitimately absent from `brief.md`, and a coverage rule would force a golden-harness capture for
every future optional field. Adding a generator therefore adds a row to this guard automatically;
changing an example still owes a golden capture.

The slot vocabulary is enforced in two layers, with `schema_slot_ids()` as the single source:

- *Vocabulary* — both contracts always reject unknown slot ids: in the model, in the slot each
  `Question` targets, and in every DAG edge (`derived_from`, `contests`). `questions` is capped at 6.
- *Completeness* — `completeness_gap()` is the single definition (the full required slot set, plus a
  non-empty objective), read by both boundaries that enforce it: the discovery `validate` hook, which
  needs a `ValueError` to ride the retry loop, and `validate_proposal`, which needs a structured
  `RequivoError`. They used to state it separately, and drifted. As defence in depth, `readiness_blockers()` reasons over the
  *schema's* required slots rather than the ones returned, and `diff_models()` walks the union of
  old/new keys so a removed slot registers as a change.

## The two core concepts

- **Slots (the atomic unit).** Every requirement lives in a slot: `completeness` (0–100), `confidence`
  (explicit|inferred|empty), `impact` (low|medium|high), `value`, `evidence`. Slots group into four
  navigation pillars (Why / What / How / Validate) defined in `framework/elicitation.md`. Every output
  is a render of the same filled model: the bars are per-pillar completeness, the questions are its
  gaps, the assessment is a consultant's read of it.
- **The driver: `information_value = uncertainty × impact`.** The engine does **not** ask because a
  slot is empty — it asks where information value is high. Empty-but-low-impact slots are left alone;
  filled-but-risky slots get probed. Impact is estimated **from the product context**, so the engine is
  only as sharp as the `context/*.md` cards it is given. This is the central design idea; preserve it
  when editing prompts.

## The model is the product; artifacts are views

Discovery persists the model to `.requivo/sessions/<slug>/model.json` — the durable product; each apply
also freezes a copy under `revisions/`. Everything else is a **generator**: a pure function
`model → artifact`, run again from the saved model without redoing discovery.

Because artifacts are views, they go **stale** when the model moves, and the model knows what rests on
what. `core/dependencies.py` holds the graph: a `DesignDecision` records the slots it was
`derived_from`; a `Challenge` records the slots it `contests`; `ARTIFACT_SLOTS` records which slots each
artifact consumes. The assessment maps to `*` — it is a judgment over the whole model, so any material
change invalidates the saved copy. `propagate()` gives the blast radius, `diff_models()` the material
change between two versions (value/confidence/impact — completeness alone is noise).

Each generator is the same shape — **prompt + contract + generator fn + writer** — and every interface
reaches them through `DiscoveryService.generate()`, which owns the revision lock, the provenance and
the artifact write. `stories` and `estimate` are deliberately terminal-only analyses with no file
(`DiscoveryService.reason()`).

**Adding a generator touches every registration point below, or a type lands in some tables and not
others** — the exact drift #270 found: a type present in `ARTIFACT_FILENAMES`/`_GENERATORS`/`_WRITERS`
but missing from `_ARTIFACT_SLOTS_RAW` was never flagged stale, because `services/artifacts.py`'s
`_stale_since` reads both of its checks off that one map, which is invariant 1's exact failure shape.
This checklist used to name only three of the eight registration points and place one of them in the
wrong file; `tests/test_dependencies.py`'s key-agreement test
(`test_the_real_artifact_registries_agree_on_their_key_sets`) now fails when a new type reaches some
of the tables below and not the rest, so a future omission is caught rather than only documented:

- a prompt asset + contract
- a function in `providers/anthropic/generators.py`, registered in `_GENERATORS` and `_OP_PROMPTS`
  (which stay one table each)
- a writer *function* in `render/markdown.py` — the writer *registration table*, `_WRITERS`, lives in
  `services/discovery.py`, not in `render/markdown.py`
- an entry in `core/dependencies.py`'s `_ARTIFACT_SLOTS_RAW` (which slots the artifact consumes — the
  one the staleness graph actually reads at save time, so a type missing here is never flagged stale
  regardless of what else knows about it) and, if the type is saveable, `ARTIFACT_FILENAMES` (its
  filename) **and** `ARTIFACT_FILES` — found missing from this checklist and from the guard's own
  first cut, in review of this same change: `services/sessions.py`'s `_resolve_stale`, which runs on
  *every* apply rather than only at save time, iterates `for t in ARTIFACT_FILES` to decide which
  already-saved artifacts to eagerly re-flag, so a type present everywhere else and absent from this
  one table is never auto-flagged stale by that path even though the save-time path still catches it
- a label in `web/viewmodels/labels.py`'s `ARTIFACT_LABELS`, so the Web has something to call it
- a subcommand in `cli.py`

Any generator whose text is user-facing carries the **Voice** rule: no slot ids, percentages or
confidence labels in prose.

`brief_markdown` is deliberately half deterministic. Its *What is confirmed* and *Important
assumptions* sections are projections of the model (`_stated()` reads each topic's evidence), not
prose the provider was asked to write — a restatement of facts can drift from the model it restates,
and a projection cannot. Ask the provider for judgment; read the facts off the model.

A generator can have **more than one writer** on the same contract — a second view, no extra call.
`Epic` has `epic_markdown()` (human) and `epic_export_json()` (a tool-neutral versioned envelope).
**Tracker adapters** are pure transforms over that neutral export, not over the internal `Epic`, which
keeps the core tool-agnostic: `to_github()` degrades honestly (GitHub has no native epic or dependency
— a tracking issue plus task list, `depends_on` stated in bodies, a `requivo-epic:<slug>` idempotency
label), `to_gitlab()` maps `depends_on` to native issue links. The authenticated push is deliberately
out of repo — an n8n flow consumes the plan. Adding Jira = another pure `to_<tracker>()`.

## The golden harness (measuring a prompt or context-card change)

Behaviour is tuned by editing assets, and the engine is non-deterministic with no sampling controls on
the model family in use — so "did this edit help?" cannot be answered from one run.

```bash
python scripts/golden_run.py [<slug>…] [--brief]   # re-capture the K-run baseline (K=3, GOLDEN_K)
python scripts/golden_diff.py [<slug>…]            # what moved, above the noise floor
python scripts/golden_diff.py <slug> --questions   # the questions & challenges themselves, old vs new
```

A bare `golden_run.py` (no slug, no flag) captures every single-pass request and skips every
interactive one by default, naming each skip and the exact command to capture it alone -- see
Cost, below, for why. Name the slug explicitly, or pass `--all`, to capture an interactive request
as part of a run anyway (#276).

**Two shapes of request.** Most are **single-pass** — one discovery call, K times. A request carrying
`answer.<slot>:` lines in `requests.md` is **interactive**: it drives `DiscoveryService.draft_turn`,
the loop behind `requivo discover`, for up to `GOLDEN_TURNS` turns per run (5), answering off those
lines. Each line is one *layer* — the next thing that client has to say when the engine comes back to
that slot — handed out in order and then exhausted, which is what keeps a conversation moving instead
of looping on one reply. It exists because the interactive loop is grounded on the carried model alone
from turn 3, where the loop it replaced re-sent the transcript, and turns 1 and 2 are byte-identical
between the two — so a single-pass capture, and a two-turn one, measure nothing about it (#77, #137).

`fixtures/golden/requests.md` is the fixed request set — one request per problem *form*. Each is
captured K times into `fixtures/golden/<slug>.runs.json`; the committed version is the baseline, the
working tree is the candidate. Workflow: edit an asset → `golden_run` → `golden_diff` → commit the new
baseline if the change was intended. Why it is built this way (`scripts/golden_lib.py`):

- **Consensus over K runs, not one capture.** A slot dimension is only a usable reference if all K runs
  agree; the per-request noise floor is printed on a fresh capture so you know how much signal it can
  carry.
- **Strong vs weak moves.** Strong = unanimous before *and* after; weak = a bare majority, which at K=3
  is one run flipping. Act on strong; watch weak only in aggregate. Without this the lens reports
  jitter as signal.
- **A capture identical to HEAD reports "not re-captured", never "no change"** — a false all-clear is
  the one failure mode a regression lens must not have.
- **Every lens runs, and the run's verdict is the union of the ones that ran** — the strongest signal
  any of them found. They are independent measurements of one capture, not votes on one question, so
  a null result from one is never evidence against a finding from another; the slot section's *no
  change above the noise floor* line decides only whether **that section** is a dash. It used to end
  the whole request, which made the assessment lens unreachable in exactly the case it exists for —
  a `brief.md` edit that moves the verdict or the challenges without moving a slot — and `--brief`
  doubles that request's calls, so whoever paid for the capture was the one told there was nothing to
  see (#162). A lens that could not look says so on its own line and **contributes nothing to the
  verdict** — including the case where the baseline has an assessment and the new capture does not,
  which is marked `!` because committing it would drop a lens, and still measured nothing. That last
  one was graded strong for one review round, by analogy with the turn lens, and the analogy fails:
  interactivity is declared in `requests.md` and reproduced on every capture, while `--brief` is a
  per-invocation flag no capture remembers and every single-pass baseline in `fixtures/golden/`
  carries one — so the grading turned the documented no-`--brief` workflow into six strong signals
  over a run where nothing moved, which is the noise the file exists to suppress, manufactured by the
  lens meant to catch it. `test_the_assessment_lens_runs_when_the_slot_consensus_held_still` and
  `test_a_capture_that_dropped_the_assessment_says_so_without_manufacturing_a_signal` are the guards.
- **The assessment lens** (`--brief`, doubles that request's calls) watches the deliverable: the
  complexity verdict and the challenges, grouped by the slots they contest. Grouping challenges by
  headline wording was tried and abandoned — the engine rephrases at the concept level, so two
  wordings of one challenge share no words and matching read that as one lost plus one gained.
- **The slot tiers are a projection; the questions and challenges are the product.** `--questions` is
  usually what settles whether a change was an improvement or merely a movement.
- **The interactive lens** watches turn 3 and beyond on an interactive request: questions **re-asked**
  after the client already answered them, early confirmations the model **lost** by the end, and
  completeness that **regressed** across a deep turn. It carries its own third state — a single-pass
  baseline reports *not measured* rather than an empty finding set, and a run that converged before
  five turns is flagged as shallow rather than counted as clean. On a SHALLOW capture it also names
  which answer-sheet layers no run ever reached (`AnswerSheet.remaining()`, wired back in as
  `unreached_layers()` after being removed as dead in #137) — a layer counts only when *every* run
  left it unused, so the sheet is implicated only when it, and not the engine, is why the capture
  stayed shallow. Reported only below `MEASURABLE_DEPTH`: a healthy capture is deliberately authored
  deeper than five turns, and its leftover layers are by design, not a finding (#163).

Cost: K calls per **single-pass** request, doubled under `--brief`. An interactive
request is K × `GOLDEN_TURNS` on its own (15 at the defaults), so capture it alone rather than as part
of a full-set run — the bare invocation skips interactive requests for that reason. Re-capture the
targeted request first, the full set only before committing a baseline. -- a bare `golden_run.py`
enforces this by skipping interactive requests rather than only recommending it in prose (#276);
`--all` or an explicit slug opts back in.

**No total for the set is written down, here or in the script.** It used to be — *"a full six-request
cycle is 18"*, in this file and in `golden_run.py`'s docstring, arithmetically right on the day both
were written and silently wrong the day a seventh request landed in `requests.md`, with nothing going
red in between (#290). `planned_calls()` derives the ceiling from the requests a given invocation
actually parsed and selected, and `main()` prints it before the first call, so the figure a reader
budgets against is the live one. Pinned by
`test_the_announced_call_count_moves_with_the_request_set`.

**A committed baseline can silently predate a real change, and `golden_diff` now says so before any
lens output.** `baseline_commits_since` (`golden_lib.py`) compares each baseline's own last commit in
HEAD against commits since touching `WATCHED_PATHS` — the prompt/context/framework assets and
`generators.py`'s on-wire user-message assembly. Funded by two reproduced instances (#405, #410):
three asset commits landed between two committed baselines and went unnoticed for a month, and
`ba526f6` dropped `indent=2` from the JSON `generators.py` sends as the user message for every
`--brief` capture, with nothing in the tree able to see it — `prompt_version()` only hashes the
system prompt, and `tests/test_golden_baselines.py` only compares `request`/`answers`. Three states,
printed as the first line of every request's readout: `current`, `stale` (named, with the capture
date and each commit since), and `unknown` (git unavailable, a shallow clone, or no commit history
for the baseline) — `unknown` must never render as `current`, the same rule this file already states
for a byte-identical capture, applied here to a commit count instead. The scope is deliberately
narrower than everything that can move what a capture measures — `core/context.py`'s own assembly
logic and `completion.py`'s retry/parsing are real gaps with no reproduced instance yet — and the
printed line names exactly which paths it checked rather than reading as coverage it does not have.

**Known limit (partially mitigated):** `load_context()` concatenates every card by default, so each new
card dilutes its neighbours. Measured once, strongly: adding `financial-reporting` cost `doc-reapproval`
its sharpest question (3/3 runs → 1/3, displaced by that card's audit-trail emphasis).
`requivo discover --context <cards>` lets a session opt into a subset, held constant across its turns
so the cached prefix survives — but there is still no *automatic* relevance routing, which a third such
instance would justify.

## Extending

- **New context card:** copy `src/requivo/assets/context/_template.md` to `…/context/<name>.md` and
  fill it; it is picked up automatically (non-`_` prefix). For a pip install with no checkout, drop
  cards in `user_context_dir()` (`REQUIVO_CONTEXT_DIR`, default `~/.config/requivo/context`) —
  `_card_paths()` in `core/context.py` merges bundled + user cards by stem, user winning on a clash.
  Better cards → better impact estimates → better questions. Measure through the golden harness: a card
  helps its target request and can quietly cost a neighbour.
- **`config_vs_custom`** is the one `optional: true` slot — the platform edge (hardcoded / configurable
  / per-client / reusable-for-all). On for configurable multi-client platforms, off for one-shot apps.
- **Adding a slot** touches more files than any other kind of change, and it used to be possible to
  miss the one file that silently mattered most (#269). Three are mandatory:
  1. **`framework/model_schema.json`** — the slot itself (`id`, `pillar`, `impact_default`,
     `label`, `probe`; `optional: true` only for a platform-edge slot with no dedicated artifact
     field, `config_vs_custom` today). This is the single source — `schema_slot_ids()` reads it, and
     everything else either derives from it or has to be kept consistent with it by hand.
  2. **`framework/elicitation.md`** — the pillar table and the human-readable spec. **Unguarded**:
     nothing fails if this drifts from the schema, so check it by eye every time. Unguarded on
     purpose and not by omission — #278 asked for a guard and was answered with a measurement:
     `decision: elicitation-schema-hand-kept`.
  3. **`core/dependencies.py`'s `_ARTIFACT_SLOTS_RAW`** — add the slot to every artifact set it
     materially shapes, or, if it genuinely feeds no *specific* artifact (only the assessment's
     judgment over the whole model, via `brief`'s `*`), name it with a reason in
     `tests/test_dependencies.py`'s `_SLOTS_WITH_NO_SPECIFIC_ARTIFACT`. **Guarded since #269**:
     `test_every_required_slot_is_consumed_by_a_specific_artifact_or_is_exempted` fails on a required
     slot that is in neither. Before that test existed nothing caught this — a new slot silently
     marked nothing stale for prd/stories/estimate/criteria/epic/release, only the assessment,
     invariant 1's exact failure shape landing on the most routine change the schema will ever see.

  Six more, conditional on whether the slot should surface in a *specific* buildable artifact rather
  than only shape the assessment's judgment through `*`: a slot-named field on the relevant contract
  in `core/contracts.py` (e.g. `PRD`'s `workflow`/`business_rules`/`permissions`/`integrations`/
  `edge_cases`); the matching writer in `render/markdown.py`; guidance in the relevant prompt(s)
  (`prd.md`, `stories.md`, `estimate.md`, `brief.md`, …) telling the model what the slot means for
  that artifact; and any context-card activation line (`assets/context/*.md`) that names slot ids.

  **Whatever you touch, a full golden re-capture follows** — `{{SCHEMA}}` is substituted into every
  prompt, so adding a slot moves every prompt's hash. See "The golden harness" below.
- **Docs live in `docs/`**, one file per subject (`architecture`, `cli`, `web`, `session-format`,
  `providers`, `context-cards`, `requirements-model`, `evaluations`, `product-validation`, `roadmap`),
  plus `docs/decisions/` for the arguments no test can go red for — see *Where a bug narrative lives*
  above, and `docs/README.md`, which indexes all of it.
  The README is an orientation, not a manual — put depth in `docs/`. `product-validation.md` is the
  manual protocol for "is this better than a strong prompt?"; keep it out of the golden harness, which
  answers a narrow mechanical question and would lend a false precision to a judgment.
