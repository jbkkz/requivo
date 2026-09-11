# changelog.d/ — changelog fragments

One file per pull request, so two open pull requests never edit the same line of
`CHANGELOG.md` and stop conflicting on every merge. Fragments are folded into
`CHANGELOG.md` at release time and deleted.

This directory is created empty, before there is anything to put in it, because
`.github/workflows/oss-changelog.yml` reads it on every pull request and an absent
directory is a failure rather than an empty one. The first red build in a repository
should not be the pull request that installed the check.

## Naming

```
<issue>.<section>.md
```

`<section>` is a Keep a Changelog heading, lowercased: `added`, `changed`,
`deprecated`, `removed`, `fixed`, `security`.

## Body

A single top-level `-` list. No headings, no raw HTML, no unclosed fences. Name the
issue in the text as well as in the file name — the file name is metadata, and
metadata does not survive being read out of context.

## Compatibility, on a `removed` fragment

A `removed` fragment must say whether the removal breaks anything, as an ordinary
bullet in the body:

```markdown
- Compatibility: breaking|compatible - <reason>
```

The release number is proposed from these fragments, and a `removed` fragment that
declares nothing stops the proposal rather than defaulting quietly — a patch bump
over a breaking change is indistinguishable in the tag from a considered one. A word
that is neither `breaking` nor `compatible` stops it too, so a value nothing
recognises never grades as compatible.

The reason after the verdict is required: a bare flag is the same unsourced verdict
one field further along, and the sentence is the part worth having.

**`breaking` means correct code stops working.** Not "something observable moved":
an exit code that now refuses an invocation which used to operate on the wrong
session, a call that used to be paid for and discarded, a diagnostic that reports
what it used to pass over — those moved on a path no correct code was on, and they
grade `compatible`, with the moved observable named in the reason so a reader still
learns it moved. The grade turns on the consequence, not on the observability,
because a major is what the grade costs and three of them landed in thirteen days
on observables nobody's correct code depended on. The argument, and why there is no
third grade for the in-between case, is
`decision: a-release-is-justified-by-its-contents`.

Only `removed` is required to carry one. Every other section may, and a fragment that
says nothing is read as compatible with the count of such fragments reported out
loud. A field on every fragment is a field on every fragment to get wrong, so it is
required exactly where the question is genuinely open.

It is a plain bullet rather than front matter, so the assembler needs no special case
and the claim ships into `CHANGELOG.md` where a user reads it, instead of being
metadata deleted at the fold.

## Nothing user-visible in this change?

Label the pull request `no-changelog`. **That label is not created for you.** Writing
a file into a checkout is a change somebody reads in a diff and reverts; creating a
label changes the repository on the forge, from a tool that was run to write files.
So it is named here instead, with the command:

```bash
gh label create no-changelog --description "Change is invisible to users"
```

Until that label exists the check has no escape hatch, and every pull request needs a
fragment.
