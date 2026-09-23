# The decision brief is an English document that quotes

**Slug:** `the-decision-briefs-quoted-half`

## Context

`docs/requirements-model.md` puts the saved artifacts on the English side of the output-language
policy. Most writers receive only the contract the provider filled; `brief_markdown` alone also
receives the `EngineOutput`, and four of its sections are a **projection** of it — the objective,
*Current understanding*, *What is confirmed* and *Important assumptions* — read off the model and so
in the request's language, while the problem, solution, decisions, challenges and risks are the
provider's English. A French request saves a brief that is English with four French sections. #481
corrected the page to say so; the choice it left open is this one (#491).

## Decision

**The brief stays on the English anchor side, and its projected sections are quotations** — the
client's own words about their own problem, which are not improved by translation. The saved brief
is read by the build side, the same audience as the PRD and the epic; the PM taking questions back
to the client is served by the turn output, which mirrors. No code changes: the behaviour is pinned
by `test_the_decision_brief_projects_the_models_own_words_rather_than_restating_them` and
`test_the_decision_briefs_english_anchor_covers_the_judgment_the_provider_wrote`. A reader expecting
a wholly English brief will meet French in four sections; that is the decision, not a defect.

## What breaking it cost

Nothing yet. Before #481 the page asserted an English anchor the code never had — one release of a
document describing behaviour the tree did not have. **Revisit** if the saved brief's primary reader
turns out to be the client or the PM rather than the build side.

## Alternatives rejected

- **Move the brief to the mirroring side.** It would make the first document in the build chain the
  one that is not in the anchor language.
- **Translate the projection.** Asks the provider to restate facts it was given — *ask the provider
  for judgment; read the facts off the model* — costs a call, and puts the section a reader checks
  against the model at the end of a paraphrase.
