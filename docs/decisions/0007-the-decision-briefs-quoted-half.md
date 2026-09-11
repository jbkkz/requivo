# The decision brief is an English document that quotes

**Slug:** `the-decision-briefs-quoted-half`

## Context

`docs/requirements-model.md` puts the six saved artifacts on the English side of the output-language
policy, against the questions and the understanding, which mirror the client's request. For five of
them that is the whole story: `prd_markdown`, `criteria_markdown`, `epic_markdown` and
`release_markdown` each receive **only** the contract the provider filled, so the language sentence
in their prompt is the only thing deciding what they say.

`brief_markdown` is the exception, and the exception is structural rather than incidental. It is the
only writer that also receives an `EngineOutput`, and four of its sections are a **projection** of
that model rather than prose the provider was asked to write:

| section | source | which side |
| --- | --- | --- |
| `**Objective:**` | `out.summary.objective` | mirrors the request |
| *Current understanding* | `out.summary.scope` | mirrors the request |
| *What is confirmed* | `_stated(out, explicit)` → each slot's `value` | mirrors the request |
| *Important assumptions* | `_stated(out, inferred)` + `out.summary.assumptions` | mirrors the request |
| `**Problem:** / **Solution:**`, decisions, challenges, risks | the `Brief` the provider wrote | English |

So a French request saves a `solution-assessment.md` whose judgment is English and whose four
projected sections are French. #481 corrected the page to describe this rather than to keep
asserting an anchor the code did not have; the decision that correction deliberately did not take is
the one recorded here (#491).

## Decision

**The decision brief stays on the English anchor side, and the projected sections are quotations.**

An English document that quotes its source in the source's own language is an ordinary thing, not a
compromise: the four projected sections are *the client's own words about their own problem*, read
off the model's evidence, and a quotation is the one kind of text that is not improved by being
rendered into the document's language.

What decides which side the artifact sits on is who reads a **saved** `solution-assessment.md`. It
is the build side and the trail behind a commitment — the same audience as the PRD, the stories, the
criteria and the epic, which is exactly why the anchor exists. The PM taking questions back to the
client is served by the *turn* output, which mirrors and always has; they do not need the saved file
to mirror as well, and the saved file has a second reader who needs it not to.

Consequences, stated so nobody has to re-derive them:

- **Nothing changes in the code.** This is the behaviour that ships. The two tests that pin it —
  `test_the_decision_brief_projects_the_models_own_words_rather_than_restating_them` and
  `test_the_decision_briefs_english_anchor_covers_the_judgment_the_provider_wrote` — stay where they
  are, and their existence is what makes the cost of ever reversing this visible.
- **The artifact is honestly bilingual and the page says so.** `docs/requirements-model.md`'s "The
  language of the outputs" section names `brief_markdown`'s projected half as one of the policy's
  two open edges. It is no longer an open edge; it is a decided one, and the page now says which.
- **The residue, named rather than implied:** a reader who expected a wholly English saved brief for
  a French request will still meet French in four sections. That is the decision, not a defect.

## What breaking it cost

Nothing yet, and this record says so rather than inventing an incident. What it *had* cost before
#481 was one release of the opposite failure: the page asserted an English anchor across all six
artifacts while the code had never had one for the brief, so the document a reader trusted described
a behaviour the tree did not have.

The trigger for revisiting is a fact about readers, not about code: **if the saved brief's primary
reader turns out to be the client or the PM rather than the build side**, the artifact is a
conversation document and belongs on the mirroring side. That is a product observation, and it is
the one thing that should reopen this.

## Alternatives rejected

- **Move the brief to the mirroring side entirely.** Coherent, and it answers a real objection: the
  current assignment rests partly on an architectural consequence — the brief's reasoning is folded
  into the model and every later generator reads that model — standing in for a product decision.
  Rejected because it splits the saved-artifact set on an axis that is not the reader's. The English
  anchor exists so that what feeds dev teams and trackers reads in one language; a brief on the
  mirroring side would make the *first* document in that chain the one that does not.
- **Translate the projection.** This is the option that looks like the fix and is not. It means
  asking the provider to restate facts it was already given, which is precisely what `CLAUDE.md`
  refuses at the line where `brief_markdown` is half deterministic: *ask the provider for judgment;
  read the facts off the model* — a restatement can drift from the model it restates, a projection
  cannot. It also costs a call, and it would put the one section a reader checks the model against
  at the far end of a paraphrase.
