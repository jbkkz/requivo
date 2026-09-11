# The exported archive's default filename shares a reserved-stem shape, and it is not a live gap

**Slug:** `reserved-stem-export-filenames-are-not-a-live-gap`

## Context

`session export`'s default destination is `<slug>.requivo.zip`. A slug that is a reserved Windows
device name (`con`, `nul`, ...) produces a filename whose *stem* is exactly the shape `validate_slug`
(#372) refuses at session creation. Raised as a concern in review of #372: does the export path
re-open that refusal on write, for a filename stem rather than a directory name?

## Decision

No fix needed. A reserved slug can only reach `session export` by already occupying a session
directory on disk, and Windows refuses to *materialize* a directory under that name in the first
place (#372) -- so on the one platform where `con.requivo.zip` would also collide with the reserved
shape, there is no `con` session to export from. A caller who genuinely needs a portable archive name
unaffected by this has `--output`.

## What breaking it cost

Nothing: the concern was raised and closed in review before it shipped, not found afterward.

## Alternatives rejected

- **Validate the destination filename's stem against the reserved-name list before writing it.**
  Rejected: the check would never fire, since the session it would be exporting cannot exist on the
  one platform the check would matter for.
