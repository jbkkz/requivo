# Adopting the DCO

**Slug:** `adopting-the-dco`

## Context

`CONTRIBUTING.md` said a CLA or DCO *"may be introduced before accepting large external
contributions; if that happens it will be documented here first."* The relicense from MIT to
Apache-2.0 was possible precisely because sole authorship was provable from history — and the first
two external contributions arrived the same week that wording was written (#439).

Every additional contributor raises the cost of ever changing licensing posture again, and the
question is cheapest to answer while the contributor count is two. Waiting for the *large* external
contribution the sentence names means introducing the requirement at the moment it is most
expensive: on a pull request somebody has already written.

## Decision

**Adopt the Developer Certificate of Origin, now, at version 1.1
(<https://developercertificate.org>).** Every commit in a pull request carries a `Signed-off-by:`
trailer whose email matches the commit's own author; `.github/workflows/dco.yml` and
`scripts/check_dco.py` are the check, and `CONTRIBUTING.md` states what is being certified.

A **CLA is refused, in the same breath and for a stated reason**: the hosted product consumes
`requivo` as an ordinary Apache-2.0 dependency, so there is no proprietary relicensing right over
contributions that this project needs and does not already have. A CLA would ask contributors to
grant something nobody is going to use, and it is the heavier of the two instruments by a wide
margin — a signing ceremony against a per-commit trailer.

What this deliberately does **not** do:

- **It does not become a required check on day one.** A context that has never produced a run, made
  required, leaves every open pull request blocked on something that will never arrive — the exact
  hazard `decision: appending-a-required-check` is about, one check along. It runs on pull requests
  from the day it lands and can be added to branch protection once it has.
- **It does not reach backwards.** Commits already on `main` are not signed and are not going to
  be; the certificate is about what arrives from here.
- **It does not check the pull request body, the squash message, or a comment.** The DCO is a
  per-commit assertion by the person who wrote the commit. A sign-off anywhere else certifies
  nothing about the commits under it.

**The `#343` note, recorded so the question stops being reopened.** That pull request was opened
under the MIT-era `CONTRIBUTING.md` wording and merged hours after the relicense; the contribution
is a few dependency-floor lines, likely *de minimis*, and MIT-inbound is redistributable inside
Apache-2.0 in any case. This paragraph is the maintainer note that closes it at zero cost. It is
**not a legal conclusion** and is flagged for professional review if the hosted entity ever
commissions one.

## What breaking it cost

Nothing yet, and this record says so rather than inventing an incident — the honest weight here is
that the cost is asymmetric in time rather than already paid. What it protects against is concrete:
a future licensing question answerable only by reading git history and guessing at intent, on a
project whose *last* licensing change was possible only because that history happened to be
unambiguous.

The trigger for revisiting is a corporate contribution large enough that its employer wants a
written grant. That is the case a CLA exists for, and the case this decision does not cover.

## Alternatives rejected

- **Record the refusal instead: inbound=outbound under Apache-2.0 §5 is sufficient.** The real
  competing answer, and it costs nothing today. Rejected because "sufficient" is a claim about a
  provenance record that would then exist only as git metadata nobody asserted anything about — and
  the project has already been in the position of needing that record to be unambiguous, once,
  successfully, by luck rather than by design. The cost of the alternative is a single trailer per
  commit; the cost of being wrong is unbounded.
- **A full CLA.** Rejected above: it grants a right this project has no use for, and it puts a
  signing ceremony in front of a first-time contributor.
- **A third-party DCO action or GitHub App.** Rejected on this repository's own supply-chain
  posture: the one third-party action it runs is the subject of an open issue about exactly that
  (#433), and this check is forty lines of `git log` that needs no token and no external
  maintainer.
- **Make it required immediately.** Rejected for the ordering reason above, not on the merits.
