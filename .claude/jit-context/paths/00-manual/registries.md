---
title: "Adding a generator touches every registration point"
description: "The registries a new artifact type must reach, and the test that fails when it reaches some (#270, #587)."
match: ^src/requivo/(core/dependencies\.py|services/discovery\.py|providers/anthropic/generators\.py|render/markdown\.py|web/viewmodels/labels\.py|cli\.py)$
---

A new artifact type lands in **all** of these or it is never flagged stale, never listed, or never
reachable (`test_the_real_artifact_registries_agree_on_their_key_sets`):

- prompt asset + contract, whose Output format example validates against the contract;
- `providers/anthropic/generators.py`: the function, in `_GENERATORS` and `_OP_PROMPTS`;
- `render/markdown.py`: the writer; `services/discovery.py`: its `_WRITERS` entry;
- `core/dependencies.py`: `_ARTIFACT_SLOTS_RAW` (what the staleness graph reads) and, if saveable,
  `ARTIFACT_FILENAMES`;
- `web/viewmodels/labels.py`: `ARTIFACT_LABELS`; `cli.py`: the subcommand.

Staleness is the dependency graph, never the revision number. `docs/extending.md` has the rest.
