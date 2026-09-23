# CodeQL's `py/path-injection` and `py/url-redirection`: false positives, and what would reverse that

**Slug:** `codeql-sanitizers-it-cannot-see`

## Context

Code scanning carried 31 open alerts (#500) when this was written — since dismissed per the decision
below: 27 `py/path-injection` in the store and one in `services/discovery.py`, 4
`py/url-redirection` in `web/routes/`. #499's `{slug}` API parameter gave CodeQL its first
user-controlled source reaching the store, and 12 were re-attributed to that PR's untouched lines.
Each is a false positive for a structural reason: every path component is a `validate_slug`- or
`validate_filename`-checked name or a literal, with `is_contained` beneath (invariant 17); an
artifact type is only a key into the closed `ARTIFACT_FILENAMES`; every redirect is
`/sessions/{slug}` — a fixed prefix after which a scheme or `//` is unrepresentable. CodeQL's models
see neither a regex match nor a dict lookup as a sanitizer. A dismissal resting on a paragraph stays
dismissed after the sanitizer is weakened, so it needs a guard that goes red.

## Decision

**Dismiss the 31 as false positives, each naming the guard that would go red, and add the guard the
redirect class lacked:** `test_a_redirect_never_leaves_this_origin_under_a_hostile_slug`, with
`test_the_redirect_refusal_is_the_slug_guard_and_not_merely_a_missing_session` (must-fire) and
`test_a_legitimate_slug_still_redirects_where_it_should` (control). The API's parameters are held
by `test_no_slug_shaped_traversal_reaches_the_filesystem`,
`test_no_artifact_type_traversal_reaches_the_filesystem` and
`test_the_refusal_is_the_slug_guard_and_not_merely_a_missing_session`. The standing guards:
`test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline` (`_SLUG_RE`),
`test_an_explicit_over_long_slug_is_refused_at_the_boundary`,
`test_a_session_path_is_not_resolved_before_it_exists` and
`test_a_symlink_out_of_the_session_root_is_still_refused` (`is_contained`),
`test_downloading_an_unknown_artifact_type_refuses_rather_than_inventing_a_filename`, and
`test_slug_traversal_is_rejected`.

**Never turn the queries off or filter `core/persistence/` or `web/routes/` out** — they aim at
exactly what breaks if the sanitizers stop holding, in the store every session goes through.

**What reverses this:** `_SLUG_RE`/`_FILENAME_RE` admitting a separator, dot segment, drive letter or
control character; a new filesystem writer taking a value none of the guards checked; an open-ended
artifact-type table; a redirect built without the fixed literal prefix (`next=`, an absolute URL).
Any of these reopens the alerts; otherwise a re-raised alert is dismissed the same way.

## What breaking it cost

No shipped defect. It cost #499 a red CodeQL leg on lines its diff never touched, and would do so to
the next nearby PR had the alerts stayed open.

## Alternatives rejected

- **Disable the queries** — removes the one signal that catches a widened regex or weakened guard.
- **A path filter** — the same blindness, on the files most likely to carry a real instance.
- **Dismiss without a guard** — a reason with no red test is indistinguishable from one that used
  to be true.
