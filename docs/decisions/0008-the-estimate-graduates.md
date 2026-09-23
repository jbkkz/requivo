# The estimate graduates to a saved artifact type

**Slug:** `the-estimate-graduates`

## Context

`requivo estimate` answers the pain the product names first — committing to scope and price on a
half-understood request — and existed only in terminal scrollback. CLAUDE.md called `stories` and
`estimate` "deliberately terminal-only". Measured across the registries a saveable type touches,
that was true of neither: `stories` had a filename and a Web label but no writer; `estimate` sat in
the staleness graph (`render_impact` printed it in a blast radius) but had no filename, so no path
could persist it. Two types, three states — the half-registration #270 documents. The answer gated
the API freeze (`decision: the-http-api-facade`) and the MCP/n8n resource set (#426).

## Decision

**The estimate becomes a saved artifact type, and `stories` is finished in the same change.** An
estimate is the one artifact where being stale costs money, and the staleness mechanism could not
reach it with nothing on disk. Retreating instead would remove `stories` from public registries;
retrofitting after the freeze would change a promised route; and no browsable surface showed the
most on-promise output (#232).

**Save both, against one revision.** The estimate is reasoned against a stories draft from the same
snapshot (#135); saving it alone would record a `source_revision` naming half its basis (invariant
6). `generate()` reasons the stories, saves them, then saves the estimate beside them. No prompt
changed, so no golden re-capture. Landed as #519.

**The `/analyses/{stories,estimate}` API routes are retired, not aliased** (#519): both types are
reached through `POST /sessions/{slug}/artifacts/{type}` like every other artifact, and a second
route whose only difference is not persisting is what this decision ends. `reason()`/`reason_from()`
stay as the unsaved seam for a caller holding its own snapshot.

## What breaking it cost

The measured half-registration: `requivo impact` naming an artifact no session could hold, and a
Web label for a document nothing could write. **Revisit** only if a saved estimate is read as a
durable commitment — and then make its staleness louder rather than un-save it.

## Alternatives rejected

- **Keep both terminal-only, deliberately** — leaves the open repo's most on-promise output
  unshareable and outside the staleness graph while the hosted product persists it: the open-core
  line in the wrong place; and it needs `stories` removed from two public registries to be true.
- **Graduate the estimate alone** — a half-true provenance, and the same drift one type along.
- **Save the estimate as a rendering of the stories** — they consume different slots and go stale
  for different reasons (`_ARTIFACT_SLOTS_RAW` records each).
