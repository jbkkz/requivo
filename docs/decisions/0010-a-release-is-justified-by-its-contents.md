# A release is justified by its contents

**Slug:** `a-release-is-justified-by-its-contents`

## Context

v1.0.0 → v2.0.0 → v3.0.0 in thirteen days, each major **correctly** forced by
`docs/compatibility.md`'s own direction-aware breaking rule. A fourth, `4.0.0`, was proposed one day
after the third on twenty fragments, and cut as a minor with the derivation overridden. Then the
maintainer loop's `merged_prs: 10` threshold fired ~21 hours after v3.1.0, on a morning of five
merges, and was surfaced rather than acted on. The observation arrived three times — from the
tags, from the version script, and from the automation — and each time it was the same one: every
individual release was right under the rules, and nothing anywhere said what a release *is for*
(#440).

Two facts about this project decide the shape of the answer, and the record states them so the
policy is not mistaken for a general one:

- **It is maintained when its author has time, not continuously.** A cadence written down is a
  promise the project cannot keep, and a promise it does not want to make: the maintainer's own
  words on the issue are *"there has to be meaning, and a rhythm of every 48 hours or 10 PRs has
  none."*
- **Its one downstream consumer of consequence pins exactly.** `docs/compatibility.md` already
  recommends `requivo==X.Y.Z` and explains why a range ceiling *reads as prudence and works as
  starvation*. So the cost of a major is not a broken install — it is a chore, and the currency in
  which churn is paid is the reader's trust that the number means something.

## Decision

**A release is cut because of what is in it. No count of merged pull requests and no elapsed time
is a reason to tag, and neither is written anywhere as one.** Concretely:

1. **What justifies a release** — any one of these, and nothing else:
   - a **user-visible capability** landing: a verb, a surface, an artifact type, a skill;
   - a **fix somebody is waiting on**, so that the release is the delivery of that fix;
   - a **blocking-class finding** — in the maintainer loop's finding-ranking table, `destroys`,
     `discloses`, `executes`, `forges`, `ships-local-state`, either containment row — which ships
     **immediately** and alone if need be. This is the one trigger the loop keeps, and it is
     content-shaped.

   A release that is none of these is not cut. Merged work waits on the next one that is, which is
   the batching the issue's option (a) describes — arrived at from the other direction.

2. **The count and time triggers are removed from `.oss.json`**, not raised. `release.triggers`
   carried `merged_prs: 10` and `soak_hours: 48`; both encoded exactly the rhythm rejected above,
   and while they stood the loop was under a standing instruction to propose the thing the
   maintainer had said not to do. An absent trigger reads as *not met, none declared* in
   `release_trigger.py` — a third state, not a zero — so the loop's release phase now fires only on
   the blocking class. Tag-and-publish authority stays with the loop (`authority: loop`) for
   precisely that class: a security fix must not wait on the maintainer being at the keyboard.

3. **What a major means: correct code stops working.** `docs/compatibility.md`'s pricing rule is
   unchanged — a break to anything on that page costs a major — and this record settles the
   question that rule leaves open, which is what *break* means at the fragment. A fragment is
   graded `breaking` **only when there is correct usage that stops working**. An observable that
   moved on a path no correct code was on — an exit code that now refuses an invocation which used
   to operate on the wrong session, a call that used to be paid for and discarded — is
   `compatible`, with the moved observable named in the reason so a reader still learns it moved.
   This is the issue's own diagnosis (*two grades carrying three facts, the nameless one rounded
   upward, a major each time*) answered without a third grade: the fragment vocabulary is owned by
   the vendored assembler and is not this repository's to extend, and the honest answer was already
   expressible once `breaking` was defined by its consequence rather than by its observability.
   Tested against the two fragments that produced the `4.0.0` proposal, both grade `compatible`
   under this definition, and the release they were in was correctly a minor.

4. **Breaking changes batch, and a major is cut when the batch has a reason to ship**, by rule 1.
   A breaking change is never held back from a release that is happening; it is never the reason
   one happens. The exception is rule 1's third bullet, which was the issue's own exception and
   which, under rule 3, is usually not breaking anyway.

5. **The integrator pin, in one sentence, quotable:** *pin exactly, `requivo==X.Y.Z`, and bump it
   as a routine chore gated by your own tests and the repository conformance suite.* That sentence
   already lived in `docs/compatibility.md`; this record makes it the policy's stated consequence
   rather than a paragraph a reader has to find.

## What breaking it cost

Not a hypothetical. Three majors in thirteen days is the measurement, and the first downstream
casualty already exists: a consumer's `<2.0.0` pin went stale within a day of 2.0.0 and nothing on
either side went red (`docs/cloud-boundary.md` §2). The cost was not a broken build — it was a
ceiling that read as prudence and starved the consumer of every fix after it, silently. And the
cost of the count trigger was a proposal to release on a morning's merges, which a human had to
decline and record.

The trigger for revisiting this record is a second maintainer with a different availability, or a
consumer that cannot pin exactly. Neither exists.

## Alternatives rejected

- **Fast majors as the declared policy** (the issue's option (b)). Honest, and rejected because it
  makes the version number a changelog index rather than a signal: under it, `requivo==7.0.0` in
  October would be correct and would tell a reader nothing about whether anything they use moved.
  The exact-pin recommendation would still hold — it holds under any policy — but the number would
  carry no meaning of its own, which is the property the maintainer named as the one that matters.
- **A monthly cadence.** Content-shaped enough to be tempting, and rejected because it is still a
  clock: a month with nothing user-visible in it would produce a release that means nothing, and a
  month with a security fix on day two would wait. Rule 1 already gives the second case its answer
  and the first case has none it needs.
- **Raising the count and time thresholds instead of removing them.** Rejected because a higher
  number is the same policy with a longer fuse, and picking one would be inventing the cadence this
  record exists to refuse.
- **A third fragment grade** (`observable`, `breaking: no-migration`). The issue's own proposal, and
  the right diagnosis. Rejected as a mechanism because the grade vocabulary is the vendored
  assembler's, with one owner that is not this repository — and because rule 3 reaches the same
  outcome by definition rather than by a new value the version script would also have to learn.
- **Moving release authority to `maintainer`.** Considered because the policy is a human judgment.
  Rejected because the one trigger that remains is the one where speed matters most, and the
  judgment it needs — *is this blocking-class?* — is the ranking table's, already made at review.
