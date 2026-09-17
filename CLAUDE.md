# CLAUDE.md

Guidance for Claude Code in this repository: the rules and the map. The story behind every rule is
on the tracker, under the issue its guarding test cites (`decision: the-tree-records-the-rule`).

## What this is

**Requivo — a requirements engine.** It turns a vague client request into a structured solution
model ready for dev. It is not a chatbot: the product is the model (typed slots, four pillars, the
driver `information_value = uncertainty × impact`) and the engine that fills it until it is precise
enough to build from. Documents are views of that model. Everything in the repo is in English; the
engine's questions mirror the request's language and every buildable artifact anchors English
(`docs/requirements-model.md`, "The language of the outputs").

## Run and test

```bash
cp .env.example .env                    # ANTHROPIC_API_KEY; REQUIVO_MODEL defaults to claude-sonnet-5
uv run requivo demo                     # replays a saved run: no key, no network
uv run requivo run "We'd like a leave approval system."   # the whole conversation, one verb
uv run requivo status <slug>            # readiness, offline; `docs <slug>` lists the seven documents
uv run python -m pytest tests/ -q       # the whole suite: no API calls, no network, no build step
uv run ruff check src tests scripts     # lint, as CI runs it
```

`docs/cli.md` is the verb reference; `docs/getting-started.md` the classic venv install. Sessions
are written under the caller's workspace at `.requivo/sessions/<slug>/`, never inside the install.

## Architecture

Reasoning is one LLM call per turn, and its intelligence lives in prompt assets rather than Python.
The layers form a strict DAG: `core/` (deterministic; never a provider, never argv or stdout) →
`providers/` (the only LLM callers) → `services/` (the one apply path, the one orchestration) →
`render/`, `cli.py`, `deterministic/`, `web/` (the only layers touching argv, stdout or HTTP). Every
interface is a thin layer over the same services: there is never a second apply, generation or
staleness rule. `docs/architecture.md` is the full description.

```
requivo/
  paths.py streams.py usage.py http.py   framework-free: roots, stdout encoding, spend ledger, error→HTTP status
  assets/          shipped in the wheel: prompts/, perimeters/<id>/ (model_schema.json, elicitation.md), context/, demo/
  core/            contracts (StrictModel; PersistedEngineOutput for what is read off disk), analysis, context,
                   persistence/, validation, errors, dependencies (the DAG), integrity, adapters, perimeters
  providers/       base.py = the ReasoningProvider protocol; anthropic/ = client, pricing, completion, generators
  services/        sessions (SessionService), artifacts, repository (SessionRepository), discovery (DiscoveryService)
  render/          data → str, no side effects
  cli.py           the journey verbs in the order a user meets them; status, demo and impact never build a client
  deterministic/   the plumbing verbs (doctor, schema, context, session, model, artifact), bound through register(sub)
  web/             FastAPI + Jinja2 + HTMX over the services; viewmodels/labels.py is the user-facing vocabulary
plugins/claude-code/   the Claude Code plugin, not in the wheel; its skills mirror a pinned CLI commit
```

## Invariants

Each line is a rule and the test that goes red without it; the cost of breaking it is in the issue
that test cites.

1. Staleness is the dependency graph, never the revision number:
   `test_related_slot_change_marks_artifact_stale`.
2. A generation carries the revision it read, as `expected_revision` and `source_revision`:
   `test_an_artifact_generated_from_a_superseded_revision_is_born_stale`.
3. Refuse, don't truncate; refuse, don't filter:
   `test_an_oversized_request_is_refused_before_any_provider_call`.
4. What an LLM fills is `StrictModel`; completeness is checked at the discovery boundary only:
   `test_contracts_reject_a_field_the_schema_does_not_define`.
5. Reasoning items recompute their id from their own text, never trusting a supplied one:
   `test_reasoning_items_carry_a_stable_content_derived_id`.
6. Provenance is real or absent: `test_a_revision_records_the_prompt_it_was_reasoned_against`.
7. `core/` never imports a provider and never touches the process:
   `test_core_never_imports_a_provider`, `test_core_never_touches_the_process`.
8. The session format and the `--json` outputs are public (`docs/compatibility.md`), and a
   diagnostic is at least as permissive as the loader:
   `test_a_session_written_by_an_older_requivo_still_loads`.
9. A compound mutation runs under `repo.lock(slug)`, held outside the session directory:
   `test_racing_applies_conflict_cleanly_instead_of_crashing`.
10. A proposal is not a model: slots replace, reasoning lists are tri-state, and `resolve(current)`
    is the only place they collapse: `test_reasoning_merely_omitted_by_a_turn_is_preserved`.
11. Creating a session is one atomic rename onto its slug, never a preceding existence check:
    `test_racing_creations_of_one_session_all_agree_on_it`.
12. A provider call reasons from one `snapshot()`:
    `test_a_snapshot_cannot_report_one_revision_and_another_revisions_model`.
13. A first discovery only lands on revision 0, claimed before the paid call:
    `test_both_discover_entry_points_refuse_a_refined_session_before_paying`.
14. The services are the integrity boundary, and a persisted card name is untrusted input:
    `test_the_service_refuses_a_context_card_that_does_not_exist`.
15. A listing survives its own members; the CLI's third state is `EXIT_DEGRADED`:
    `test_one_unreadable_session_no_longer_takes_the_listing_down`.
16. Text is UTF-8 on both sides, and a renderer cannot kill the process:
    `test_every_text_read_declares_its_encoding`.
17. A guard's verdict never depends on transient filesystem state:
    `test_a_session_path_is_not_resolved_before_it_exists`.
18. `_atomic_write` retries a denied rename, briefly and only that:
    `test_atomic_write_survives_a_transient_permission_error`.

## Rules of the tree

- **The tree records the rule; the tracker records the story.** One line at a call site: the rule,
  the cost, the test that goes red. Five lines at most in a test docstring, citing the issue. A
  citation names a test function or a test file, on one line. A decision record is for a decision a
  reader could reopen, never for an incident (`docs/decisions/README.md`).
- **Every size has a ceiling in `tests/lean_budget.toml`**, this file included. Ceilings only go
  down; a number in prose that no test can falsify comes out.
- **A guard that does not exercise runtime code needs two real instances, named by issue.** Extend
  an existing tier before opening a file. The persistence diagnostics tier (`core/persistence/scan.py`
  and `doctor`'s non-session and lock scans) is frozen: no new report-only diagnostic without a
  reproduced field instance and an action a user can take.
- **Behaviour is tuned in the assets and measured through the golden harness, never judged from one
  run** (`docs/evaluations.md`). The shared prompt head stays byte-identical across templates or the
  cache is lost; `build_system_prompt` refuses a template that does not open with it.
- **The Web speaks a translation** (`web/viewmodels/labels.py`, `viewmodels/status.py`): relabel and
  select, never recompute, and nothing stored changes.
- **Adding a generator, a slot or a perimeter touches a fixed set of registration points**, listed
  in `docs/extending.md`; a jit-context rule restates the list when one of those files is touched.
  Any change to a prompt or a schema is followed by a golden re-capture.

## Where things are written down

`docs/README.md` indexes the manuals. `docs/decisions/` holds the arguments no test can go red for.
`CONTRIBUTING.md` maps which guard trips on which change and where the fix goes.
