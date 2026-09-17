# Extending Requivo

> The checklists for the changes that touch several registration points at once. Each one is also a
> jit-context path rule under `.claude/jit-context/paths/00-manual/`, injected when one of the files
> it names is edited; this page is the same list for a reader without the hook.

## Adding a generator

Every generator is the same shape: prompt + contract + generator function + writer, reached through
`DiscoveryService.generate()`, which owns the revision lock, the provenance and the artifact write. A
new type touches all of these, or it lands in some tables and not others (#270, #587):

- a prompt asset under `assets/prompts/` and a contract in `core/contracts.py`, whose "Output
  format" example must validate against that contract (`tests/test_prompt_contracts.py`);
- a function in `providers/anthropic/generators.py`, registered in `_GENERATORS` and `_OP_PROMPTS`;
- a writer function in `render/markdown.py`, registered in `services/discovery.py`'s `_WRITERS`;
- an entry in `core/dependencies.py`'s `_ARTIFACT_SLOTS_RAW` (the slots the artifact consumes; the
  staleness graph reads this and nothing else) and, if saveable, `ARTIFACT_FILENAMES`;
- a label in `web/viewmodels/labels.py`'s `ARTIFACT_LABELS`;
- a subcommand in `cli.py`.

`test_the_real_artifact_registries_agree_on_their_key_sets` fails when a type reaches some tables and
not the rest. A generator whose text is user-facing carries the Voice rule: no slot ids, percentages
or confidence labels in prose. A generator can have several writers on one contract (`Epic` has a
Markdown writer and a JSON export; tracker adapters are pure transforms over that export).

## Adding a slot

A slot belongs to one perimeter's vocabulary (`assets/perimeters/<id>/`, #608). Three steps are
mandatory, all scoped to that perimeter:

1. `model_schema.json`: the slot itself (`id`, `pillar`, `impact_default`, `label`, `probe`;
   `optional: true` only for a platform-edge slot with no artifact field, `config_vs_custom` today).
   `schema_slot_ids(perimeter)` reads it; everything else derives from it.
2. `elicitation.md`: the pillar table and the spec. Unguarded on purpose
   (`decision: elicitation-schema-hand-kept`), so check it by eye.
3. `core/dependencies.py`'s `_ARTIFACT_SLOTS_RAW`: add the slot to every artifact set of that
   perimeter it materially shapes, or name it with a reason in `tests/test_dependencies.py`'s
   `_SLOTS_WITH_NO_SPECIFIC_ARTIFACT`.
   `test_every_required_slot_is_consumed_by_a_specific_artifact_or_is_exempted` fails otherwise.

Conditional, when the slot should surface in a specific artifact rather than only shape the
assessment: a field on the contract in `core/contracts.py`, its writer in `render/markdown.py`,
guidance in the relevant prompts, and any context-card activation line that names slot ids.

`{{SCHEMA}}` is substituted into every prompt, so adding a slot moves every prompt's hash: a full
golden re-capture follows (`docs/evaluations.md`).

## The output contract

Each stage's Pydantic contract must agree with its prompt's "Output format" block: `ModelProposal` ↔
`engine.md` (a proposal; `EngineOutput` is what it resolves into), `Brief` ↔ `brief.md`, `Stories` ↔
`stories.md`, `EstimateDraft` ↔ `estimate.md`, `PRD` ↔ `prd.md`, `AcceptanceCriteria` ↔
`criteria.md`, `Epic` ↔ `epic.md`, `ReleaseNotes` ↔ `release.md`. The agreement is guarded offline
(`tests/test_prompt_contracts.py`); the list above is kept by hand.

Slot vocabulary is enforced twice from one source, `schema_slot_ids()`: both contracts reject unknown
slot ids in the model, in a question's target and in every DAG edge; `completeness_gap()` is the one
definition of completeness, read by the discovery `validate` hook and by `validate_proposal`.

## Adding a context card

Copy `assets/context/_template.md` to `assets/context/<name>.md`; any non-`_` file is picked up.
For an install without a checkout, drop cards in `REQUIVO_CONTEXT_DIR` (default
`~/.config/requivo/context`); user cards win over bundled ones on a stem clash. Every card is
concatenated into every prompt by default, so a card that helps its target request can cost a
neighbour: measure through the golden harness. `docs/context-cards.md` covers scoping a session.
