# The estimate graduates to a saved artifact type

**Slug:** `the-estimate-graduates`

## Context

`requivo estimate` is the output aimed most directly at the pain this product names first —
committing to scope and price on a half-understood request — and it is the one output that exists
nowhere but a terminal scrollback. `CLAUDE.md` records the reason as a decision: *`stories` and
`estimate` are deliberately terminal-only analyses with no file*.

**Measured, that sentence is true of neither of them.** The eight registration points a saveable
type touches disagree, and they disagree differently for each:

| registry | `stories` | `estimate` |
| --- | --- | --- |
| `_GENERATORS` (a provider function) | ✅ | ✅ |
| `_ARTIFACT_SLOTS_RAW` (what it consumes — the staleness graph) | ✅ | ✅ |
| `ARTIFACT_FILES` (what `_resolve_stale` re-flags on every apply) | ✅ | ✅ |
| `ARTIFACT_FILENAMES` (it has a filename, so `artifact save` accepts it) | ✅ | ❌ |
| `ARTIFACT_LABELS` (the Web has a name for it) | ✅ | ❌ |
| `_WRITERS` (something can render it to that file) | ❌ | ❌ |
| a `--save` path on the CLI verb | ❌ | ❌ |

So `stories` is addressable as a saved artifact by an external writer, has a label the Web would
render, and nothing in this repository can produce the file. `estimate` is known to the dependency
graph — `render_impact` already prints it in the blast radius, as *"estimate (regenerate on
demand)"* — and cannot be persisted by any path. Neither is "terminal-only by design". Both are
**half-registered**, which is the exact drift #270 documents, sitting in the tree already.

This decision gates the v1 API freeze (`decision: the-http-api-facade`, §6) and shapes the resource
set the MCP facade and the n8n flows project (#426).

## Decision

**The estimate graduates to a saveable artifact type, and `stories` is finished in the same change.**

The decisive argument is the product's own central mechanism. Artifacts are views, they go stale
when the model moves, and the dependency graph knows what rests on what. **An estimate is the one
artifact where being stale costs money** — a number quoted against an understanding that has since
changed is worse than no number — and today the mechanism built to protect exactly that cannot
reach it, because there is nothing on disk to flag. The graph already has an opinion about what
invalidates an estimate; only the file is missing.

Three supporting reasons, none of them sufficient alone:

- **The half-registration is the drift, not the terminal-only-ness.** Whichever answer were taken,
  seven registries holding three different opinions about two types is a defect. Closing it by
  *completing* the vocabulary costs one sweep; closing it by retreating would mean removing
  `stories` from `ARTIFACT_FILENAMES` and `ARTIFACT_LABELS`, which is a breaking change to a public
  payload for the sake of a sentence.
- **Retrofitting later is more expensive than doing it now**, and the API freeze is the deadline.
  A new artifact type is additive to the session format (invariant 8, #260) and additive to the
  resource model *before* a freeze; after one it is a promised route that has to change meaning.
- **The most on-promise artifact is invisible on every browsable surface**: no web page, no label,
  no plugin skill, and no example ships one (#232).

**The wrinkle this decision has to settle, because saving it wrong is worse than not saving it.**
The estimate is not a function of the model alone: it is reasoned *against a stories draft* that the
same invocation produced and never persisted (#135 — one snapshot, two calls). Saving the estimate
by itself would record a `source_revision` that names half its basis and stay silent about the other
half, which is invariant 6's rule broken in the one place provenance is load-bearing. So the
graduation saves **both**: `stories` gets its writer, and the estimate is saved with the stories it
was reasoned against. That is also what settles the asymmetry rather than leaving it as a second
unrecorded state.

Implementation is a separate change and follows `CLAUDE.md`'s own adding-a-generator checklist. It
needs no prompt edit and therefore **no golden re-capture** — the two prompts already exist and are
unchanged.

## What breaking it cost

Not a past incident but a present, measured one: the table above. Two types, seven registries, three
states, and `CLAUDE.md` describing all of it as a single deliberate design. The cost already paid is
that `requivo impact` names an artifact in its blast radius that no session can hold, and that the
Web carries a label for a document nothing can write.

**The trigger for revisiting** is narrow and worth naming: if saving an estimate turns out to invite
readers to treat a point-in-time judgment as a durable commitment, the answer is not to un-save it
but to make the staleness louder — which is the mechanism this decision exists to bring to bear.

## Alternatives rejected

- **Keep both terminal-only, deliberately, and record that.** The honest version of the status quo:
  `/analyses/estimate` returns the contract without persistence, the hosted product persists it in
  its own database (it is a declared-seam Pydantic contract), and the question stops being
  re-derived. Rejected because it leaves the product's most on-promise output unshareable and
  outside the staleness graph in the open repository, while the closed one gets both — an open-core
  boundary drawn in the wrong place. It would also require *removing* `stories` from two public
  registries to become true.
- **Graduate the estimate and leave `stories` as it is.** Rejected because it makes the estimate's
  provenance a half-truth, per the wrinkle above, and because it would leave the same
  half-registration one type along, which is the defect this decision is about.
- **Save the estimate as a rendering of the stories rather than as its own type.** Rejected: they
  consume different slots and go stale for different reasons, which is a fact `_ARTIFACT_SLOTS_RAW`
  already records separately for each.

## Addendum (2026-09-12, #519): the `/analyses/{stories,estimate}` routes are retired

`decision: the-http-api-facade` §1 planned `POST /sessions/{slug}/analyses/stories` and
`.../analyses/estimate` over `DiscoveryService.reason` — "terminal-only analysis, nothing persisted;
POST because it pays" — as slice 3 of #425, and #426 named their fate as part of this question. With
#519 landed, both types are in `GENERATABLE`, so both are reached through the artifacts generate
route (`POST /sessions/{slug}/artifacts/{type}`) like every other artifact, with the same save, the
same `source_revision` and the same staleness row. A separate `analyses` resource would then be a
second route to the same generation whose only difference is *not* persisting — which is the state
this decision exists to end, and the wrong side of the "one apply, one generation, one staleness
rule" line for a frozen API to promise. **Retired, not aliased**: an alias is a route the freeze
would have to keep. The remaining slices of #425 read this here rather than in 0004, which is left
as written; the two `analyses` rows in its §1 table are superseded by this paragraph.

`DiscoveryService.reason()` / `reason_from()` stay as the unsaved seam for a caller that holds its
own snapshot; nothing in the CLI or the Web calls them any more.
