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
and trackers. The saved decision brief is bilingual and says so: `brief_markdown` is the only writer
that also receives an `EngineOutput`, and its four projected sections are the model's own words,
which are on the mirroring side. The policy, its two named open edges (`estimate`, and the brief's
projected half) and the prompt sentences that enforce it are in `docs/requirements-model.md` under
"The language of the outputs".

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
Python — and that call lives in a **provider**, never in the core. The layers form a strict DAG —
`core/` (deterministic; never prints, never reads argv, never calls or imports a provider) →
`providers/` (the only LLM callers; `base.py` is the `ReasoningProvider` protocol, `anthropic/` the
one implementation, and what a run *cost* lives outside it in `requivo.usage`) → `services/` (the
application seam: `SessionService.update_model` is the single validated apply path,
`DiscoveryService` the single provider-backed orchestration, storage and reasoning both injected) →
`render/`, `cli.py`, `deterministic/` and `web/` (the only layers touching argv/stdout/HTTP).
`docs/architecture.md` is the full description, including the snapshot/`expected_revision` split
and why the services, not the interfaces, are the integrity boundary; the layer map below is what
this file adds to it.

Every interface — the terminal CLI, the Claude Code plugin, Requivo Web — is a thin layer over the same
services. There is never a second implementation of an apply, a generation, or a staleness rule.
That was stated in three places and enforced in none until #77, and "a surface" meant `cli.py`
alone for a release after that (#167, `render/` importing pricing from `providers.anthropic`).
`tests/test_boundaries.py` guards both ends of the arrow now — `core/` may not import a provider,
and a surface may reach only the provider names an allowlist keyed by **(file, name)** names as
*surface* concerns, each with its reason — and the storage half (#76: a surface reaching past
`SessionRepository` to `core.persistence`) the same way, keyed by **(file, function)** and asserted
in both directions. The fix for a leak like #167 is to move the neutral concept out of
`providers/`, not to write it an allowlist entry; the fifteen surviving direct store calls are each
*about* a path and justified at the call site. The three narratives are in that file's docstring.

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
    dependencies.py  the dependency DAG: propagate / diff_models / diff_reasoning / thinner_evidence
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
bug that looked like correct behaviour. Each entry is the rule, the cost of breaking it, and the
test that goes red when the guard is removed; the narrative behind each lives in that test's
docstring, which is where *Where a bug narrative lives* (below) says it belongs — #286 applied the
rule to this list on 2026-09-12.

1. **Staleness is the dependency graph, never the revision number.** An artifact is stale when
   something it rests on changed — the source revision is *provenance*. Two edge sets feed it:
   `ARTIFACT_SLOTS` and `REASONING_CONSUMERS` (every generator, since each is prompted with the full
   model). Report `ArtifactStatus.stale`; never compare revisions. Cost: a rewritten decision
   changing the PRD with no slot touched, and the PRD staying marked fresh. Guarded by
   `test_related_slot_change_marks_artifact_stale` (whose docstring names the other two) and
   `test_reasoning_that_changes_without_a_slot_moving_still_invalidates`.
2. **A generation carries the revision it read.** Capture `current_revision` before the (minutes-long)
   call; pass it as `expected_revision` on any apply and `source_revision` on the artifact write.
   Never record `stale=False` because the caller didn't say otherwise. Cost: a stale document
   reported fresh under a perfectly plausible revision number. Guarded by
   `test_an_artifact_generated_from_a_superseded_revision_is_born_stale` and
   `test_an_omitted_source_revision_is_refused_rather_than_read_as_now`.
3. **Refuse, don't truncate; refuse, don't filter.** Half a request reads exactly like a whole one, and
   dropping an unknown context card leaves an empty selection, which means *every* card. Guarded by
   `test_an_oversized_request_is_refused_before_any_provider_call` and
   `test_the_service_refuses_a_context_card_that_does_not_exist`.
4. **Boundary contracts are strict.** Everything an LLM fills inherits `StrictModel` (`extra="forbid"`);
   an invented field fails loudly and rides the retry loop. *Completeness* (the full required slot
   set, a non-empty objective) lives at the discovery boundary instead, because a partial
   `EngineOutput` is a legitimate internal object. Cost: a drifted prompt reading as a clean success.
   Guarded by `test_contracts_reject_a_field_the_schema_does_not_define` and
   `test_output_allows_a_partial_but_known_model`.
5. **Reasoning items have content-derived ids.** `DesignDecision`, `Challenge` and `Opportunity` recompute
   `id` from their own text on every validation; never trust a supplied one. Cost: an echoed stale
   id letting two different decisions share a handle. Guarded by
   `test_reasoning_items_carry_a_stable_content_derived_id`.
6. **Provenance is real or absent.** Each revision records provider, model, surface and a hash of the
   exact prompt; don't add a provenance field you do not populate. Cost: a column that is always
   filled gets trusted, so a revision log nothing can reproduce reads as one that can. Guarded by
   `test_a_revision_records_the_prompt_it_was_reasoned_against` and
   `test_a_deterministic_apply_carries_no_usage_provenance`.
7. **Core stays provider-free, and talks to its caller rather than to the process.** It may read and
   write files (*IO-free*, which this used to say, was false as written); it may not import a
   provider or the SDK, nor touch argv, the standard streams, the environment or process exit —
   `logging` is fine. Cost: #77, the CLI's `discover` loop reasoning two calls of its own. Guarded by
   `tests/test_boundaries.py` — `test_core_never_imports_a_provider`,
   `test_core_never_touches_the_process`,
   `test_the_surfaces_reach_the_provider_only_through_the_named_surface_concerns` — which walks
   `core/` recursively, resolves relative imports, and fails when its scan set is empty
   (`test_the_guard_refuses_a_scan_it_could_not_make`).
8. **The session format and the `--json` outputs are public.** `.requivo/sessions/` is the interface
   between every surface, at `format_version` 1: adding a field is free; renaming a *populated* one
   needs a bump, a migration in `migrate_session()` and `docs/compatibility.md` in the same change.
   Two contracts, on purpose: `StrictModel` for what an LLM fills (invariant 4),
   `PersistedEngineOutput` for what is read off disk, which also preserves what it cannot name
   (invariant 10). **A diagnostic must be at least as permissive as the loader**, about every
   vocabulary the format lets a later version extend — keys (#14) and artifact types (#260) alike;
   tolerating is still not trusting (`_ARTIFACT_TYPE_RE`, `unsafe_artifact_type`). Cost: `doctor`
   reporting a defect in a session the loader opens without complaint. Guarded by
   `test_a_session_written_by_an_older_requivo_still_loads` (backward; its docstring names the
   forward half), `test_the_persisted_contract_is_permissive_all_the_way_down` (with its
   constraint-copying sibling beside it) and
   `test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect`.
9. **A precondition is held across the writes it authorises.** Every compound mutation runs under
   `repo.lock(slug)`, taken by the service; the lock is re-entrant per thread, OS-held, and lives at
   `.requivo/locks/<slug>.lock` — *outside* the session, because a lock inside the thing it guards
   guards nothing the moment that thing can be renamed (#113). Cost: two writers passing one check.
   Guarded by `test_racing_applies_conflict_cleanly_instead_of_crashing`,
   `test_a_forced_import_serialises_against_a_concurrent_writer` and
   `test_the_lock_still_guards_a_session_that_exists`.
10. **A proposal is not a model, and silence is not deletion.** A `ModelProposal`'s slots are complete
    (an apply *replaces*); `decisions`/`challenges`/`opportunities` are tri-state — absent means "not
    speaking to it", `[]` means "delete" — and a key this version cannot name is a fourth thing it
    cannot speak to. `resolve(current)` is the *only* place the states collapse. Cost: an ordinary
    answer turn deleting every decision the assessment had produced, silently. Guarded by
    `test_reasoning_merely_omitted_by_a_turn_is_preserved`,
    `test_reasoning_explicitly_emptied_is_a_deletion_that_invalidates` and
    `test_an_unknown_key_survives_a_refinement_turn_and_not_only_a_re_save`.
11. **Creating a session is one atomic claim on its slug.** `create_session` assembles in a staging
    directory and renames into place; the rename wins or raises `SessionExistsError` — never a
    preceding existence check. Identity is the request **and** its card selection. `create_session`
    is the only producer of a session directory; what else sits under the root is reported by
    `doctor` (a report, not a repair). Cost: concurrent creations overwriting each other's identity,
    and a lock-made directory losing the rename to a session nobody created. Guarded by
    `test_racing_creations_of_one_session_all_agree_on_it`,
    `test_a_lock_on_a_slug_with_no_session_leaves_no_trace` and
    `test_doctor_names_what_is_under_the_session_root_and_is_not_a_session`.
12. **A provider call reasons from one snapshot.** `SessionService.snapshot(slug)` reads revision, model,
    request and cards under the lock; every provider-backed operation takes one, and a two-call
    analysis takes one for both (#135). Cost: revision N filed with the model of N+1, undetectably.
    Guarded by `test_a_snapshot_cannot_report_one_revision_and_another_revisions_model` and
    `test_the_estimate_verb_reads_stories_and_estimate_from_one_snapshot`.
13. **A first discovery only lands on revision 0.** Discovery never sees the current model, so
    `_require_revision_zero`/`claim_session` is taken *before* the paid call, by every entry point —
    a rule that lives in an interface is not enforced, and neither is one the documenting path takes
    and the used path does not (#133). Cost: nine paid calls thrown away by a correct refusal in the
    wrong place. Guarded by
    `test_both_discover_entry_points_refuse_a_refined_session_before_paying` (the assertion is the
    call count), `test_a_repeat_discovery_is_refused_before_the_provider_is_paid` and, for what a
    stop keeps, `test_stopping_early_keeps_the_turns_it_paid_for`.
14. **The service layer is the integrity boundary, not the interfaces.** Cards are resolved in
    `create_session`; `DiscoveryService`'s artifact service defaults to the *session service's*
    repository; and a persisted `context_cards` is untrusted input every time it is read back, held
    by `normalize_tokens` where the value is interpreted (#40). Cost: a bad name silently widening
    the context; a stored card name forging a line of `doctor`. Guarded by
    `test_the_service_refuses_a_context_card_that_does_not_exist`,
    `test_the_artifact_service_defaults_to_the_session_service_s_storage` and
    `test_doctor_cannot_be_made_to_print_a_row_a_session_wrote`.
15. **A listing survives its own members.** Any aggregate view catches per-item failure and degrades
    that row, never the page — through `SessionService.list_entries()`, *above* the rows (#7, #62),
    and a scan predicate with three answers below them (#80). A degraded row states no fact it could
    not read; on the CLI the third state is `EXIT_DEGRADED` (4), a shape of answer rather than a verb.
    Cost: one un-analysed session hiding every other. Guarded by
    `test_one_unreadable_session_no_longer_takes_the_listing_down` (and its web sibling
    `tests/web/test_degraded_listing.py`),
    `test_the_partition_answers_in_three_states_and_the_third_is_neither_neighbour` and
    `test_the_three_outcomes_have_three_exit_codes`.
16. **Text is UTF-8 on both sides, and a renderer cannot kill the process.** Every text read and write
    names `encoding="utf-8"`; `streams.py` reconfigures stdout/stderr once, from every entry point
    that prints, with `errors="backslashreplace"` — never `replace`; the `UnicodeEncodeError` arm
    exits `EXIT_RENDER_FAILED` (3) and says the work is done. Cost: mojibake that still validates,
    `integrity.py` accusing the user of an edit nobody made, and a traceback after the paid work
    landed. Guarded by `tests/test_encoding.py` — `test_every_text_read_declares_its_encoding`,
    `test_a_glyph_that_cannot_be_encoded_exits_three_rather_than_a_traceback` — and, for the two
    harness scripts (#164), `test_a_harness_script_survives_a_console_that_cannot_encode_its_output`.
17. **A guard's verdict must not depend on transient filesystem state.** Nor on where it runs.
    `is_contained` resolves only a path that is actually there (`exists() or is_symlink()`), with
    `os.path.realpath` rather than `Path.resolve()`, and refuses a symlink that resolves to its own
    location — the resolver saying *I could not look*. Cost: `InvalidSlugError` about a valid slug
    because another thread was creating a session; a dangling symlink read as contained on one CI
    leg of thirteen. Guarded by `test_a_session_path_is_not_resolved_before_it_exists` and
    `test_a_dangling_symlink_is_refused_where_the_platform_cannot_resolve_it`.
18. **`_atomic_write` retries a denied rename, briefly and only that.** `PermissionError` only, bounded by
    `_REPLACE_ATTEMPTS`/`_REPLACE_BACKOFF_S`, then the original is re-raised — the one place in the
    store where retrying is right, because the cause is external (a scanner holding the destination)
    and the operation idempotent (#3). Cost: a completed write lost to an antivirus scanner, or a
    genuine permissions error turned into a hang. Guarded by
    `test_atomic_write_survives_a_transient_permission_error` and
    `test_atomic_write_still_gives_up_on_a_permanent_permission_error`.

## Where a bug narrative lives

Every invariant above exists because a plausible assumption produced a bug that looked like correct
behaviour, and the surrounding code says so at length. That density is deliberate: the person about
to simplify a subtlety away is *in the editor*, not in a docs folder, so a pointer they will not
follow is strictly worse than the paragraph it replaced. An external review proposed moving all of
it to decision records; that remedy is rejected and its diagnosis is not (#75). The rule instead:

> **A comment paragraph that recounts a past bug must be backed by a test that goes red when the
> guard is removed.**
>
> - **If it is** — the paragraph belongs in that test, and the call site keeps one line: the
>   invariant, the cost of breaking it, and **the name of the test that enforces it**.
> - **If it is not** — it is either a missing test, which gets written, or genuine archaeology,
>   which goes to `docs/decisions/`.

It is a rule and not a preference because it is mechanically decidable: *is there a test that goes
red?* has one answer per paragraph. It converts density into coverage — nothing is deleted, and what
survives sits on a support that cannot be read in diagonal. A comment can be skimmed; a red test
cannot.

**The reference is a name, never a path** — a test function, a test module, or a decision record's
slug. Paths in this repository move (the package was renamed, `deterministic.py` became a package, a
2147-line test module became seven files, all inside a fortnight); a name survives every one of
those. **And it must be greppable**, so an identifier is never split across a line wrap.
`tests/test_narrative_references.py` guards only what is mechanical: every reference resolves, none
is wrapped. Whether a paragraph *should* carry one is a judgement stated here for a person to apply.

**The same test applies to a count** (#134): a number in prose that no test can falsify buys one
release and then lies. A count that earns its place gets a test (`tests/test_version_sites.py`);
one nobody acts on comes out.

**What this is not.** Not a licence to remove a reason attached to a guard, or a MUST-FIRE note:
those *are* the invariant rather than the story around it, and they stay at the line. Not applicable
to `docs/`, which is narrative's right home. It *was* held not to apply to this file or to the
invariant list; #286 applied it to both on 2026-09-12, and the invariants above are the result.

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
3. The `system` prompt is sent as **two text blocks** (`_system_blocks`). The first is the shared
   leading block every template opens with — `SHARED_PROMPT_HEAD`, the schema + context cards, ~9k
   tokens with the bundled cards, byte-identical across the eight operations — and it carries a
   `cache_control: ephemeral` breakpoint on **every** call, so the second operation of a sitting reads
   it at 0.1x instead of re-sending it at full price (#258; a five-op pipeline sends ~25k system tokens
   instead of ~56k). The second is the op-specific remainder, and it carries a breakpoint **only when
   its caller will send it again** (`_complete(..., reuse_system=)`): that pays across the calls of
   *one* operation — a golden capture's K runs, `converse()`'s turns — and never across operations. A
   write costs 1.25x input and a read 0.1x, so a one-call verb that cached its remainder was paying a
   flat ~25% surcharge on it (#9), and a lone one-call verb with no second op inside the 5-minute TTL
   now pays that premium once on the shared block (~2.3k token-equivalents, accepted). The other
   accepted cost: a one-call verb that hits the JSON **retry** loop re-sends the identical remainder
   uncached, paying 2.0x on it where it used to pay 1.35x — the better bet only while a retry is rarer
   than ~1 call in 4, which it is. Keep the prompt byte-identical per call or the cache is lost where
   it does pay, and keep the leading block byte-identical across templates or it is lost everywhere
   (`build_system_prompt` refuses a template that does not open with it). `_complete()` records per-call usage into a session-scoped
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
invariant 10), `Brief` ↔ `brief.md`, `Stories` ↔ `stories.md`, `EstimateDraft` ↔ `estimate.md`,
`PRD` ↔ `prd.md`, `AcceptanceCriteria` ↔ `criteria.md`, `Epic` ↔ `epic.md`, `ReleaseNotes` ↔
`release.md`. Slot ids live in `framework/model_schema.json`, which also carries each slot's
`pillar` and `label` (read back by the renderer via `slot_meta()`). The enumeration above is still
kept by hand — nothing checks those eight names against the registries — so read it as a map, not a
guarantee. The *agreement* is guarded: `tests/test_prompt_contracts.py` validates each prompt's
Output format example offline against the contract the generator's own `_complete(...)` call parses
replies with (so a generator pointed at a different contract goes red too), because
the drift was invisible and paid — a model obeying a stale example produces a reply `extra="forbid"`
refuses, `_complete()` retries twice and raises `EngineError`, so one operation cost up to three
calls while the offline suite stayed green (#266). It deliberately does **not** check an *optional*
contract field the prompt was never told about. Adding a generator adds a row to it automatically;
changing an example still owes a golden capture.

The slot vocabulary is enforced in two layers, with `schema_slot_ids()` as the single source:
*vocabulary* — both contracts reject unknown slot ids in the model, in each `Question`'s target slot
and in every DAG edge (`derived_from`, `contests`), and `questions` is capped at 6; *completeness* —
`completeness_gap()` is the single definition (the full required slot set, plus a non-empty
objective), read by both boundaries that enforce it: the discovery `validate` hook, which needs a
`ValueError` to ride the retry loop, and `validate_proposal`, which needs a structured
`RequivoError`. They used to state it separately, and drifted. As defence in depth,
`readiness_blockers()` reasons over the *schema's* required slots rather than the ones returned, and
`diff_models()` walks the union of old/new keys so a removed slot registers as a change.

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
change between two versions (value/confidence/impact — completeness alone is noise), and
`thinner_evidence()` the decisions derived while a slot they rest on was `empty`/`inferred` and is
`explicit` now — *worth re-reading*, never *contradicted* (#493; the service finds the derivation
revision, the pure comparison only takes the two models).

Each generator is the same shape — **prompt + contract + generator fn + writer** — and every interface
reaches them through `DiscoveryService.generate()`, which owns the revision lock, the provenance and
the artifact write. `stories` and `estimate` used to reach the terminal through
`DiscoveryService.reason()` and neither was written to a file — **"deliberately terminal-only",
which this line once said, was true of neither** (#426). Measured across the seven registries a
saveable type touches, `stories` was in `ARTIFACT_FILENAMES` and `ARTIFACT_LABELS` with no
`_WRITERS` entry, and `estimate` was in the staleness graph (`_ARTIFACT_SLOTS_RAW`, `ARTIFACT_FILES`)
and in neither of the first two: two types, three states, half-registered each.
`decision: the-estimate-graduates` settled it and #519 landed it: both are saved now, and the
estimate is the one two-call branch of `generate()` — it reasons the stories from the same snapshot,
saves them, and saves itself beside them against the same revision, because an estimate is reasoned
*against* those stories and a `source_revision` naming only the model would be half its provenance
(invariant 6). `reason()` stays for a caller that wants a contract and no write.

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

`docs/evaluations.md` is the manual: the two request shapes (single-pass, and interactive via
`answer.<slot>:` layers driving `DiscoveryService.draft_turn` for `GOLDEN_TURNS` turns), consensus
over K runs, strong vs weak moves, the assessment and interactive lenses, the per-shape cost, and
baseline freshness. `fixtures/golden/requests.md` is the fixed request set; the committed
`<slug>.runs.json` is the baseline, the working tree the candidate. Edit an asset → `golden_run` →
`golden_diff` → commit the new baseline if the change was intended. What this file adds is the set
of rules that have each already been broken once, and the test that holds each:

- **A capture identical to HEAD reports "not re-captured", never "no change"**, and `unknown`
  baseline freshness never renders as `current` — a false all-clear is the one failure a regression
  lens must not have.
- **Every lens runs, and the verdict is the union of the ones that ran.** The slot section's *no
  change above the noise floor* line used to end the whole request, which made the assessment lens
  unreachable in exactly the case it exists for (#162). A lens that could not look says so and moves
  no verdict — including a capture that dropped an assessment the baseline had, which is `!` and
  still nothing measured, because `--brief` is a per-invocation flag no capture remembers.
  `test_the_assessment_lens_runs_when_the_slot_consensus_held_still` and
  `test_a_capture_that_dropped_the_assessment_says_so_without_manufacturing_a_signal`.
- **Grouping challenges by headline wording was tried and abandoned** — the engine rephrases at the
  concept level, so two wordings of one challenge share no words. They are grouped by the slots they
  contest.
- **The slot tiers are a projection; the questions and challenges are the product.** `--questions`
  is usually what settles whether a change was an improvement or merely a movement.
- **A SHALLOW capture names the answer-sheet layers no run reached**, only below `MEASURABLE_DEPTH`
  (`AnswerSheet.remaining()`, wired back as `unreached_layers()` after being removed as dead in
  #137; #163).
- **No total for the set is written down, here or in the script.** A total in prose was right the
  day it was written and silently wrong the day the next request landed (#290); `planned_calls()`
  derives the ceiling from what an invocation actually selected, and a bare `golden_run.py` skips
  interactive requests (K × `GOLDEN_TURNS` each) rather than only recommending it (#276).
  `test_the_announced_call_count_moves_with_the_request_set`.
- **A committed baseline can silently predate a real change**: `baseline_commits_since` compares
  each baseline's last commit against commits touching `WATCHED_PATHS`, funded by #405 and #410 —
  scoped to the assets and `generators.py`'s on-wire assembly, and it says which paths it checked.

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
