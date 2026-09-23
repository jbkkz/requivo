# Appending a required status check to `main`

**Slug:** `appending-a-required-check`

## Context

`main` requires a list of status checks by exact name. Adding a leg to `.github/workflows/ci.yml`
does **not** add it to that list: the second is an API call a maintainer makes by hand, a fact
about a GitHub endpoint that no test here can go red for. It used to live as a comment block in
`ci.yml` (#55 added the four platform legs).

## Decision

Append with the `contexts` sub-resource, and verify by reading the count back either side:

```sh
gh api -X POST repos/jbkkz/requivo/branches/main/protection/required_status_checks/contexts \
  -f 'contexts[]=Test (py3.9, macos-latest)'   -f 'contexts[]=Test (py3.13, macos-latest)' \
  -f 'contexts[]=Test (py3.9, windows-latest)' -f 'contexts[]=Test (py3.13, windows-latest)'

gh api repos/jbkkz/requivo/branches/main/protection/required_status_checks --jq '.contexts | length'
```

An endpoint whose failure mode is a silent 200 is verified by reading, not by an exit code. The
continuations are single backslashes; the form that sat in `ci.yml` had doubled ones.

## What breaking it cost

Nothing yet. The command first recorded was `PATCH .../required_status_checks` with `contexts[]`,
which **replaces** the list: run as written it would have cut the required checks to the four it
named, answered 200, and surfaced weeks later as a PR merging green over a check that no longer ran.

## Alternatives rejected

- **Delete the note.** The wrong verb is the one the API docs lead to; the record spends the
  mistake once.
- **Reconcile the list from CI.** Needs branch-protection write access from CI — strictly worse to
  hold than a manual step taken a few times a year.
- **Fold the platform legs into the `test` matrix.** Adding an `os` axis renames the required
  `Test (pyX.Y)` checks, none reports again, and no PR can merge (noted in `ci.yml` at that job).
