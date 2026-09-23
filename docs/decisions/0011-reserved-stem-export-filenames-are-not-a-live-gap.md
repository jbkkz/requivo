# The exported archive's default filename shares a reserved-stem shape, and it is not a live gap

**Slug:** `reserved-stem-export-filenames-are-not-a-live-gap`

## Context

`session export` defaults to `<slug>.requivo.zip`. A slug that is a reserved Windows device name
(`con`, `nul`, …) gives a filename stem `validate_slug` refuses at creation (#372). Raised in review:
does export reopen that refusal for a filename stem?

## Decision

No fix. A reserved slug reaches `session export` only by occupying a session directory, and Windows
refuses to create a directory under that name (#372) — so on the one platform where
`con.requivo.zip` would collide, there is no `con` session to export. `--output` names any other
destination.

## What breaking it cost

Nothing: raised and closed in review before shipping.

## Alternatives rejected

- **Validate the destination stem before writing.** It would never fire: the session it would
  export cannot exist on the one platform where the check matters.
