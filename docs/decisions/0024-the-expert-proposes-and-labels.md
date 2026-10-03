# The expert proposes and labels

**Slug:** `the-expert-proposes-and-labels`

## Context

The plugin's honesty rules said "never fabricate" and the generation skills read it as "never add":
the PRD was "a view of the model — not new invention", every requirement "grounded in the model,
not added from outside it", and an `inferred` value could only ever be an assumption. Fabricating
the requester's word and contributing expertise were one rule. #744 measured the cost: on the same
request, with the same answers relayed, a plain Claude wrote a brief and a PRD a PM would rather
have — a concrete design for the core feature, data-protection facts that changed the scope (who is
controller, which data is special-category), versioned rules with a re-check flag, a table of
riskiest assumptions with kill signals, options set aside. Same model; Requivo's rules had turned it
into a slot-renderer.

## Decision

**The reasoner is an expert who proposes and labels, not an engine that only renders what it was
told.** Decided by the maintainer on 2026-10-03.

- **Fabrication stays forbidden.** What the requester said, decided or observed is never invented.
- **Expertise is expected.** The reasoner contributes proposed defaults (a concrete design choice
  where the model leaves room) and domain knowledge (regulatory, technical, market facts). Each is
  `inferred`, with evidence `proposed: <rationale>` or `domain: <fact>`. An appeal to what "most
  teams do" is not a fact and stays out.
- **A document may propose**, tagged `[proposed]` with a one-line rationale. Unknowns stay visible,
  open decisions stay open, and a proposal never overrides a stated value.
- **Documents stay views of the model.** Every proposal a document introduces is applied to the
  model first, through `model apply`, so it lands in #731's "what I will assume unless you object"
  list and can be vetoed.
- **Documents tag claims by source**: requester, evidence, repo, proposed, domain, assumed. The tag
  is derived from the slot's confidence and evidence; the model keeps one confidence per slot
  (`decision: confidence-stays-one-axis`), and claim-level provenance is #747's separate design.

**Status.** The keyless plugin carries this (#746, #749). The API path's generator prompts under
`assets/prompts/` still carry the old rule ("no unsourced industry knowledge"); moving them is a
separate change that owes a golden re-capture. Until then the plugin's skills lead the prompts they
cite (`decision: plugin-skills-mirror-a-pinned-cli-commit`).

## What breaking it cost

#744: a brief without the decision sections the baseline led with, and a PRD that could not state
what a senior PM would propose — the largest scope driver in the product (how the core feature
works) was never asked or proposed, and the data-protection scope stayed at "GDPR obligations".

## Alternatives rejected

- **Keep "never invent", tune the questions** — the run asked well (#744's "useful" list); the loss
  was in what the documents were allowed to say, which no question fixes.
- **Proposals in the documents only** — a PRD would then state what the model does not hold, and
  "documents are views of the model" would stop being true: no veto, no staleness, no blast radius.
- **A per-claim provenance field now** — the honest end state, but a model and format change;
  derived tags carry the doctrine without it (#747).
- **Unlabelled expertise** — the old failure in the other direction: a reader cannot tell the
  requester's word from the reasoner's, which is what the honesty rules existed to prevent.
