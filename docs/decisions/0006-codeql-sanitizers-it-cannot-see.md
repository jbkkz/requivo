# CodeQL's `py/path-injection` and `py/url-redirection`: false positives, and what would reverse that

**Slug:** `codeql-sanitizers-it-cannot-see`

## Context

Code scanning on `main` carries 31 open alerts (#500): 27 high `py/path-injection`
("uncontrolled data used in path expression"), all but one in `core/persistence.py`, the last in
`services/discovery.py`; 4 medium `py/url-redirection`, one in each of `web/routes/sessions.py`,
`web/routes/discovery.py` (two sites) and `web/routes/artifacts.py`. #499 is what surfaced them at
this volume: introducing an HTTP surface with a `{slug}` path parameter gave CodeQL its first
user-controlled *source* reaching `core/persistence.py`, and 12 of the 27 were re-attributed to that
pull request's diff even though it does not touch the lines flagged.

Every one of the 31 is a false positive for a structural reason, not a coincidental one:

- **`py/path-injection`.** Every path component reaching a filesystem sink is one of three things:
  a slug that has been through `validate_slug` (`_SLUG_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z"`, plus
  `MAX_SLUG_LENGTH`), a filename that has been through `validate_filename` (the sibling guard, same
  shape), or a literal. `is_contained` sits underneath both as a second layer (invariant 17). The one
  site in `services/discovery.py` reaches the store only through those same validated values.
  An **artifact type** is never a path component at all: `ArtifactService._filename` uses it as a
  key into the fixed `ARTIFACT_FILENAMES` dict and raises `UnknownArtifactTypeError` on a miss, so
  the filename that reaches the filesystem is always a member of a closed set of literals CodeQL
  could enumerate and never a string built from the request.
- **`py/url-redirection`.** Every flagged site builds `RedirectResponse(url=f"/sessions/{slug}...")`
  — a fixed literal prefix, `/sessions/`, concatenated with a slug that is either `safe_slug`-guarded
  (`web/dependencies.py`, for every `{slug}` path parameter) or `validate_slug`-checked directly
  (`create_session`'s `Form` field, the one route that can mint a slug). A leading `//` or a scheme
  cannot appear after a constant `/sessions/`, so an off-site redirect from this shape is not merely
  unlikely — it is unrepresentable regardless of what the slug guard admits, which is a stronger
  claim than the path-injection case and matters for what would reverse this decision (see below).

CodeQL's default Python queries recognise neither sanitizer: a regex `match` and a dict-lookup-to-a-
constant both leave the tainted value tainted in its model, so it keeps flagging the sink.

That reasoning is a reading of the code, and this repository does not accept a reading of the code as
evidence on its own (`CLAUDE.md`, *Where a bug narrative lives*). Dismissing 31 alerts on the strength
of a paragraph, then weakening `_SLUG_RE` two releases later for an unrelated reason, would leave every
one of those alerts dismissed and nothing to re-raise them — a dismissed CodeQL alert stays dismissed.
So the dismissal (item 2 of #500, done separately and after this record lands) has to be backed by a
guard that goes red when the reasoning above stops being true, not only by this paragraph.

## Decision

**Dismiss the 31 alerts as false positives, each dismissal naming the guard that would go red if its
sanitizer stopped holding, and add the guard the open-redirect class was missing** —
`tests/web/test_web_security.py`'s `test_the_discover_redirect_never_leaves_this_origin_under_a_hostile_slug`,
`test_the_answers_redirect_never_leaves_this_origin_under_a_hostile_slug`,
`test_the_generate_artifact_redirect_never_leaves_this_origin_under_a_hostile_slug`,
`test_the_create_session_failure_redirect_never_leaves_this_origin_under_a_hostile_slug`, with
`test_the_redirect_refusal_is_the_slug_guard_and_not_merely_a_missing_session` as the must-fire half
(a 404 from a nonsense slug would prove nothing) and `test_a_legitimate_slug_still_redirects_where_it_should`
as the must-not-fire control. These four sites had no equivalent before #500; the API layer's own
path-parameter guard (#425/#499) already covers the path-injection class through the API's own
`{slug}`/`{artifact_type}` parameters, with its own must-fire/must-not-fire pair of the same shape,
which the Web tests above translate to the HTML routes rather than duplicate reasoning about.

**Do not turn the query off, add a path filter, or exclude `core/persistence.py` (or any file) from
scanning.** The alerts are wrong today because the sanitizers hold today; the value of the query is
that it is aimed at exactly the thing that breaks if they stop holding. Silencing the query removes
that future signal along with today's noise, and `core/persistence.py` is the one file in the
repository where that trade is worst — it is the store, and every session on disk goes through it.

The guards that stand behind today's dismissal, so a reviewer can check this record against the tree
rather than trust it:

- `_SLUG_RE`'s shape (no dot, no separator, no drive letter, anchored so a trailing newline cannot
  sneak past it) — `test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline`.
- `MAX_SLUG_LENGTH` — `test_an_explicit_over_long_slug_is_refused_at_the_boundary`.
- `is_contained`, the second layer underneath the regex (invariant 17) —
  `test_a_session_path_is_not_resolved_before_it_exists` and its must-fire half
  `test_a_symlink_out_of_the_session_root_is_still_refused`.
- `ARTIFACT_FILENAMES` as a closed set, so an artifact type never becomes a path component —
  `test_downloading_an_unknown_artifact_type_refuses_rather_than_inventing_a_filename`.
- The Web's own slug guard at the HTTP boundary — `test_slug_traversal_is_rejected`, plus the four
  redirect-specific tests named above.

## What breaking it cost

Nothing yet, in the sense of a shipped defect — that is the whole point of writing this down before
the alerts are dismissed rather than after. What it already cost is a red CI leg on a pull request
that did not introduce the flagged lines: #499's CodeQL check failed on 12 alerts against code its
diff never touched, because introducing the first user-controlled source that reaches
`core/persistence.py` was enough for the scanner to re-attribute pre-existing findings to it. That is
the cost of *not* having triaged this earlier, and it is what happens again to the next pull request
that changes anything nearby if the alerts are left open rather than dismissed with reasons.

## Alternatives rejected

**Turn off `py/path-injection` and/or `py/url-redirection` entirely.** Rejected in the issue itself:
it would remove the one signal that would catch a real regression (`_SLUG_RE` widened, `is_contained`
weakened, `ARTIFACT_FILENAMES` made open-ended) along with the 31 false positives, and this is the
file where that signal is worth the most.

**Add a CodeQL path filter excluding `core/persistence.py` or `web/routes/`.** Same objection, scoped
to fewer files: a filter is blind to the exact class of change most likely to introduce a real
instance of what the query looks for, in exactly the files most likely to carry one.

**Dismiss without a guard, on the strength of the reasoning above alone.** Rejected because this
repository has a standing rule against exactly that (`CLAUDE.md`): a comment or a dismissal reason
that recounts why something is safe, with no test that goes red if it stops being safe, is
indistinguishable from a comment that used to be true. The four new tests plus the five existing
guards listed under *Decision* are what make the dismissal auditable rather than a shrug.

## What would reverse this

Any of the following would mean the position no longer holds, and the alerts it was dismissed for
would need to be reopened and re-triaged rather than assumed still safe:

- **A change to `_SLUG_RE` or `_FILENAME_RE`** that admits a path separator, a dot segment, a drive
  letter, or a control character. `test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline`
  is the guard most likely to catch a careless widening; a deliberate widening should treat this whole
  record as needing a fresh read, not just that one test.
- **A new writer that reaches the filesystem with a value that has not been through `validate_slug`,
  `validate_filename`, or `is_contained`.** Nothing currently scans for this by construction — it is
  caught only if the new writer happens to exercise one of the existing guards' fixtures, or a review
  catches it by eye.
- **`ARTIFACT_FILENAMES` (or an equivalent table) becoming open-ended** — accepting an artifact type
  it does not already know about and deriving a filename from it, rather than raising
  `UnknownArtifactTypeError`. `test_downloading_an_unknown_artifact_type_refuses_rather_than_inventing_a_filename`
  is the must-fire guard for exactly this.
- **A `RedirectResponse` built from a slug without a fixed literal prefix** — e.g. an absolute URL, a
  caller-supplied `next=` parameter, or string formatting where the slug is not strictly appended
  after `/sessions/`. This is the one CodeQL is structurally correct to flag on any future site that
  does not share this shape: the "unrepresentable" argument above is about *this specific
  concatenation*, not about slugs in general, and does not transfer to a redirect target built any
  other way.

If none of these has happened, a future run of the same two queries against the same files is still
a false positive for the reasons stated here, and should be dismissed the same way: by naming the
guard that would go red, not by re-deriving the argument from scratch.
