# Compatibility and deprecations

> What Requivo promises not to break, what it may change, and what is on the way out.

This page is a set of promise tables, one per public surface. Each row names the promise, the
version it has held since, and the test that goes red if it stops holding. The *history* of how a
promise reached its current shape — which issue split a code, which release moved a status — lives
in `CHANGELOG.md` and `docs/decisions/`; a row links to the changelog entry rather than retelling
it, and where the two disagree the changelog is what shipped and this page is what's stale.

## The rules

Requivo is versioned with [SemVer](https://semver.org/). **From 1.0.0, breaking anything on this page costs a major
version, never a minor, and never silently.** Below 1.0 a minor was permitted to change an interface, and the entries
dated 0.x in the deprecations table were taken under that licence; it is spent. The session format was outside this
either way — it carries its own `format_version` and a migration, which is not a function of the release number.

**A *break* is correct code stopping working.** An observable that moved on a path no correct code was ever on — an
exit code that now refuses an invocation which used to operate on the wrong session, a call that used to be paid for
and discarded — grades `compatible` in its changelog fragment, with the moved observable named (`decision:
a-release-is-justified-by-its-contents`; `changelog.d/README.md` is where a contributor states the grade).

**A `RequivoError.code`, a CLI exit code, and a `--json` field are governed by the same rule.** Adding one is free;
moving a condition off one and onto another is breaking — except that **moving a condition onto exit 0 is compatible,
never breaking**, since nothing could have depended on an invocation that always failed starting to succeed; only a
move *off* 0, or between two nonzero codes, costs a major.

What may change **without** a `format_version` bump, in the session format, or without a row here in every other
table: adding a field or a `--json` key anywhere (readers ignore what they don't know and preserve it on write);
retiring a field that was never populated; a new slot, a new artifact type, or a new provenance value. What
**requires** a bump, a changelog entry and — for the session format — a migration in `migrate_session()`: renaming,
removing, or changing the meaning of a populated field; changing a directory layout or a file's role; anything that
makes an older reader succeed *incorrectly* rather than merely incompletely.

**Recommended consumption pattern:** pin exactly, `requivo==X.Y.Z`, never a range — three majors shipped in thirteen
days once, so a range ceiling reads as prudence and works as starvation. Bump the pin as a routine chore gated by your
own tests and, if your backing implements `SessionRepository`, by `requivo.testing.repository_conformance` (`pip
install 'requivo[testing]'`) — a backing that passes it inherits the services' orchestration verbatim.

## The session format is public

`.requivo/sessions/<slug>/` is the interface between the CLI, the Claude Code plugin, the Web app,
and anything built on top. Its layout is documented in full in
[session-format.md](session-format.md); this table is what it promises.

| Promise | Since | Test |
|---|---|---|
| The directory layout — `session.json`, `request.md`, `model.json`, `revisions/NNNN-model.json`, `artifacts/` — is stable | 0.1 | `test_store_creates_session_and_revisions` |
| `model.json`'s slot ids come from its session's own **perimeter** schema (`assets/perimeters/<id>/model_schema.json`, one perimeter per session, frozen at creation) — the same vocabulary `requivo schema --perimeter <id>` prints | 0.1 (plural since #608) | `test_validate_rejects_unknown_slot`, `test_a_go_to_market_model_validates_and_reaches_readiness_against_its_own_vocabulary` |
| `session.json` carries `format_version`, currently **1** | 0.1 | `test_store_migrate_session_rejects_a_future_format` |
| `session.json` carries `perimeter` — `null` for a session written before #608, which reads as the **software** perimeter (a default, never a guess: there was only ever one); a *named* perimeter this install does not have is refused by name (`unknown_perimeter`), by the loader, `doctor` and `session verify` alike — the one place this page's own "adding a field is free" rule is deliberately not enough, because a perimeter is *interpreted* rather than carried through | #608 | `test_a_pre_perimeter_session_still_opens_as_software`, `test_an_unknown_perimeter_is_refused_by_name_by_the_loader`, `test_an_unknown_perimeter_is_refused_by_name_by_session_verify_and_doctor` |
| A session written by an **older** Requivo keeps loading — a retired field is ignored, a field added since takes its default | 0.1 | `test_a_session_written_by_an_older_requivo_still_loads` |
| A session written by a **newer** Requivo is refused clearly (`unsupported_format_version`, `{format_version, supported_format_version}`), and a model authored against a newer slot schema is refused independently (`unsupported_schema_version`) | 0.9.5 | `test_a_session_from_a_newer_requivo_is_refused_not_guessed`, `test_a_session_from_a_newer_slot_schema_is_refused_clearly` |
| An unknown key in `session.json` survives a load-mutate-write cycle by an older reader, rather than being dropped | 0.9.4 | `test_a_field_from_a_future_requivo_survives_a_round_trip` |
| The same is true of `model.json` and every `revisions/NNNN-model.json`, through a permissive read contract; an *apply* still replaces slots/summary/questions, but the reasoning layer and top-level keys survive a turn that says nothing about them | #14 | `test_a_model_written_by_a_newer_requivo_loads_and_survives_a_round_trip`, `test_an_unknown_key_survives_a_refinement_turn_and_not_only_a_re_save`, `test_the_persisted_contract_is_permissive_all_the_way_down` |
| An artifact type the reading build has no generator for is a **note** (`session verify`'s `notes`, `doctor`'s `sessions.notes`), never a `problems` entry, and `session import` accepts it | #260 | `test_an_artifact_type_from_a_newer_requivo_is_not_reported_as_a_defect`, `test_session_verify_passes_and_still_names_the_unknown_type`, `test_doctor_names_the_unknown_type_without_calling_the_session_inconsistent`, `test_a_future_artifact_type_survives_an_export_import_round_trip` |
| A tolerated unknown artifact type must still look like one — a plain lowercase name, ≤64 chars — or it is refused as `unsafe_artifact_type`; every other check (filename guard, containment, revision existence) still applies | #260 | `test_an_artifact_type_that_is_not_a_plausible_token_is_still_a_problem`, `test_a_tolerated_artifact_type_is_held_to_every_other_check`, `test_an_unsafe_artifact_filename_on_an_unknown_type_is_still_refused` |
| A Windows reserved device name (`con`, `prn`, `aux`, `nul`, `com1`-`com9`, `lpt1`-`lpt9`) is refused on any *new* session or artifact filename, on every platform — **breaking**: such a slug was legal before #221. A session already on disk under such a name is unaffected and stays readable | #221 | `test_reserved_windows_device_names_are_refused_as_slugs`, `test_reserved_windows_device_names_are_refused_as_filename_stems`, `test_a_session_already_on_disk_under_a_reserved_slug_is_readable_by_every_verb_that_named_it` |

## The `--json` outputs are public

**Every `--json` output is public — all sixteen — and so is the structured error envelope**
(`{code, message, path?, details?}`). A populated field does not quietly change meaning, a change of
shape gets a row below, and adding a field is always free.

| Verb | Test |
|---|---|
| `requivo status` | `test_every_json_verb_is_inside_the_promise` |
| `requivo doctor` | `test_every_json_verb_is_inside_the_promise` |
| `requivo session init` | `test_every_json_verb_is_inside_the_promise` |
| `requivo session list` | `test_every_json_verb_is_inside_the_promise` |
| `requivo session show` | `test_every_json_verb_is_inside_the_promise` |
| `requivo session migrate` | `test_every_json_verb_is_inside_the_promise` |
| `requivo session verify` | `test_every_json_verb_is_inside_the_promise` |
| `requivo session export` | `test_every_json_verb_is_inside_the_promise` |
| `requivo session import` | `test_every_json_verb_is_inside_the_promise` |
| `requivo session rescope` | `test_every_json_verb_is_inside_the_promise` |
| `requivo session delete` | `test_every_json_verb_is_inside_the_promise` |
| `requivo model validate` | `test_every_json_verb_is_inside_the_promise` |
| `requivo model apply` | `test_every_json_verb_is_inside_the_promise` |
| `requivo model diff` | `test_every_json_verb_is_inside_the_promise` |
| `requivo artifact save` | `test_every_json_verb_is_inside_the_promise` |
| `requivo artifact list` | `test_every_json_verb_is_inside_the_promise` |

Membership is guarded both ways by `test_every_json_verb_is_inside_the_promise` (a verb with
`--json` and no row here is public by accident; a row for a verb that no longer takes `--json` is
false coverage). The **shape** each one promises — a payload's top-level key set and the JSON type
of each value, nothing nested — is recorded and pinned per verb by
`test_every_json_verb_has_a_recorded_payload_shape` and
`test_every_public_json_payload_keeps_its_recorded_top_level_shape`. The `impact` verb is
deliberately not in this table: it has no `--json` (only a closing "thinner evidence" section on its
terminal output, since #493, pinned by `test_impact_reports_what_a_named_slot_reaches`) — which is
why it is spelled without its `requivo` prefix elsewhere here, since the membership guard above reads
every backtick-quoted `` `requivo <verb>` `` in this section as a promise.

**`requivo status --json` is conditional.** `slug`, `readiness`, `understanding`, `questions`,
`summary` and `remaining_gaps` are always present; `revision`, `context_cards` and `artifacts` are
added only when the reference resolves to a canonical session, because a bare `model.json` has no
session to read them from. Both forms are public. Pinned by
`test_status_and_impact_still_open_a_model_json_path_directly`.

**A code carries one fact, and one `details` shape** — the rule that makes "assert on the code, never
the message" safe to follow. Where a single code used to answer for more than one condition, it was
split; the table below is the current vocabulary for the families that were.

### The import path names the archive, not the model (#101)

`session import`'s archive-shaped refusals (the `unreadable_archive`/`invalid_archive` and
`inconsistent_archive` rows below) are about the **archive** itself — corrupt, oversized, wrongly
shaped, or failing the integrity check — never about the model packed inside it, and none of them is
raised as `invalid_model` any more.

| Condition | Code | `details` | Since | Test |
|---|---|---|---|---|
| a stray comma inside a selection (`--context "a,,b"`) | `empty_selector_token` | `{selector, position}` | 0.10.0 | `test_an_empty_token_and_an_empty_selection_are_two_codes` |
| a selection that selects nothing (`--context ""`) | `empty_selection` | `{selector, tokens}` | 0.10.0 | `test_an_empty_token_and_an_empty_selection_are_two_codes` |
| a selector token carrying a control character | `unsafe_selector_token` | `{selector, position}` | #40 | `test_a_control_character_in_a_selector_token_is_refused_not_echoed` |
| an install with no context cards at all, read | `no_context_cards` | `{roots}` | #41 | `test_resolve_cards_on_a_zero_card_install_names_the_install_not_the_card` |
| an install with no context cards at all, at session creation | `no_context_cards` | `{roots}` | #41 | `test_creating_a_session_on_a_zero_card_install_refuses_at_creation` |
| `artifact save` without `--revision` | `unstated_source_revision` | `{slug, type, source_revision, current_revision, cause}` (`source_revision`/`cause` null) | #6, #57 | `test_an_omitted_source_revision_is_refused_rather_than_read_as_now` |
| the stated source revision cannot be read | `unreadable_source_revision` | same five keys (`cause` populated) | #82 | `test_the_two_provenance_refusals_carry_two_codes_and_one_details_shape` |
| session/model file truncated, mis-encoded, not JSON, or a lock could not be opened | `session_unreadable` / `model_unreadable` | `{slug}` / `{path, slug?, revision?}` | #82, #204 | `test_nothing_raises_the_malformed_session_family_base`, `test_every_arm_of_the_family_names_a_distinct_fact` |
| an archive fails the integrity check on import | `inconsistent_archive` | `{slug, problems}` | #101 | `test_every_refusal_on_the_import_path_names_what_it_is_about` |
| an archive is not a readable zip, or not shaped like an export (8 sub-conditions, closed `problem` vocabulary: `empty`, `too_many_entries`, `too_many_files`, `too_large`, `unsafe_entry`, `entry_outside_session_directory`, `multiple_sessions`) | `unreadable_archive` / `invalid_archive` | `{archive}` / `{problem, …}` | #101, #219 | `test_import_refuses_an_archive_that_is_not_a_session`, `test_import_refuses_an_archive_bounded_by_files_and_bytes_but_not_by_directory_entries` |
| the import slug is taken and `--force` was not passed | `session_exists` | `{slug}` | #101 | `test_import_refuses_a_collision_unless_forced` |
| a non-session directory already occupies the import slug | `import_destination_occupied` | `{slug, path}` | #114 | `test_a_stray_directory_at_the_slug_is_refused_by_name_on_every_platform` |
| no usable host on a Requivo Web request | `undetermined_host` | `{host_header_present, host_header, hint}` | #52 | `test_a_request_that_states_no_host_at_all_is_refused` |
| a host this server does not answer to | `host_not_allowed` | `{host, hint}` | #52 | `test_a_request_addressed_to_another_host_is_refused` |
| the browser declares another site (`Sec-Fetch-Site`) | `cross_site_fetch` | `{sec_fetch_site}` | #52 | `test_a_browser_declared_cross_site_write_is_refused` |
| `Origin: null` | `opaque_origin` | `{origin, host}` | #52 | `test_the_opaque_origin_is_refused_deliberately_and_says_which_arm_fired` |
| the origin is outside the host's trust domain | `origin_mismatch` | `{origin, host}` | #52 | `test_a_write_from_another_origin_is_refused` |
| the request token was absent or wrong | `missing_request_token` | `{}` | #52 | `test_a_write_without_the_request_token_is_refused` |

`invalid_session` is the family base and nothing raises it directly — `except InvalidSessionError`
still catches every arm above without enumerating them (`test_nothing_raises_the_malformed_session_family_base`).
The unstated/unreadable source-revision pair deliberately still shares its five-key `details` shape;
`test_the_two_provenance_refusals_carry_two_codes_and_one_details_shape` pins that a shared shape is
a choice, not an obligation, the same answer #52 gives for `opaque_origin`/`origin_mismatch` above.

**`doctor --json` and `session list`/`migrate`/`verify --json` additions** — each additive, each
narrows or widens exactly one field, never a shape:

| Field | Promise | Since | Test |
|---|---|---|---|
| `doctor`'s `perimeters.schemas` | Per-installed-ID `{ok, slots, error}`; a load failure has `slots: null` and does not hide other rows. Legacy `schema` and perimeter-discovery fields retain their meanings | #623 | `test_doctor_reports_each_perimeters_schema_health`, `test_doctor_isolates_a_broken_perimeter_schema` |
| `sessions.total` | `null`, not `0`, when the session root itself could not be read | #34-family | `test_doctor_tells_an_empty_workspace_from_an_unreadable_one` |
| `sessions.non_sessions[]` | what is under the session root and is not a session; `slug_shaped` asks the read-time reserved-name rule, so a `con` directory now reads `true` | #67, #408 | `test_doctor_names_what_is_under_the_session_root_and_is_not_a_session`, `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken` |
| `sessions.unexaminable[]` | names under the session root whose examination raised; distinct from `non_sessions` and excluded from `total` | #80 | `test_an_unexaminable_entry_alone_earns_the_warning_glyph_not_the_clean_tick` |
| `locks.unexpected` | no longer names an ordinary discovery's own `.discovering` guard file, nor a reserved-name session's own lock/guard file while that session exists | #391, #401 | `test_an_ordinary_discover_leaves_no_lock_residue_doctor_flags`, `test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue` |
| `locks.unmatched` | names a reserved-name lock/guard file whose session has since been deleted, instead of `unexpected` | #409 | `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted` |
| `session verify`'s `session` object | `{checked, error}`; branch on `session.checked`, never on `problems` being empty | #97 | `test_session_verify_exits_four_when_the_lock_could_not_be_taken` |
| `session list`'s `readable`/`error` per row, and the payload becoming `{sessions, degraded, session_root}` rather than a bare array | #62, #87 | `test_one_unreadable_session_no_longer_takes_the_listing_down`, `test_json_is_an_object_so_it_can_ever_gain_a_top_level_field` |
| `session list`'s degraded row can now name an entry not known to be a session at all | #80 | `test_the_three_outcomes_have_three_exit_codes` |
| `session import`'s keys renamed to `{slug, path, replaced}` (`path` is the session directory, not the old `into` sessions-root) — **breaking** | #84 | `test_every_public_json_payload_keeps_its_recorded_top_level_shape` |
| `doctor`'s `output.streams[].state` spells `will_crash`, not `will-crash` — **breaking** | #88 | `test_every_public_json_payload_keeps_its_recorded_top_level_shape` |
| `artifact list`'s payload is `{slug, artifacts}`, not the bare type-keyed map — **breaking** | #107 | `test_every_public_json_payload_keeps_its_recorded_top_level_shape` |
| `session migrate`'s `interrupted`, `errors`, `unreadable`; exits `4` when any is non-empty rather than always `0` | #262, #411 | `test_session_migrate_survives_one_undecodable_legacy_request_beside_a_healthy_session`, `test_session_migrate_survives_a_reserved_name_legacy_directory_beside_a_healthy_one` |

Terminal (non-`--json`) rendering of `session show`, `session list` and `artifact list` escapes a
control character read back out of `session.json` rather than echoing it — `--json` is unaffected,
since `json.dumps` already escapes the full C0/C1 range. Pinned by
`test_session_show_cannot_be_made_to_print_a_line_a_session_wrote` and
`test_artifact_list_cannot_be_made_to_print_a_row_a_session_wrote`.

### HTTP statuses in Requivo Web

**Every `RequivoError` code has an explicit HTTP status** — there is no default. An unrecognised
code is a 500, never a 400: "we could not classify this" is not evidence the caller erred. The two
version-frontier codes are 409, the five store-state `invalid_session` arms are 500, the three
archive arms plus `unstated_source_revision` stay 400, and every `cross_site_request` arm is 403
(all in [the code table above](#the-import-path-names-the-archive-not-the-model-101)). The rows
below are the ones with a specific status and consequence worth stating on their own:

| Code | Status | Note |
|---|---|---|
| `context_unreadable` | 500 | the server cannot read its own card directory — not the caller's fault |
| `no_context_cards` | 500 | the install shipped no cards; nothing the caller sent caused it |
| `provider_output_invalid` | 502 | upstream would not hold the contract, after every retry |
| `session_locked` | 503 | the write never started — safe to retry unchanged |
| `session_exists` | 409 | a conflict with the store's state, like `revision_conflict` (409 too) |
| `input_too_large` | 413 | the request itself, refused before any provider call |
| `spend_ceiling_reached` | 403 | not 429 — a spend budget does not reset with time |

| Promise | Since | Test |
|---|---|---|
| Every code maps to an explicit status; none falls through to a default | #34, #422 | `test_every_error_code_has_an_explicit_http_status` |
| A server-side fault (store state, missing context) is never reported as the caller's bad request | #34 | `test_a_server_side_fault_is_not_reported_as_the_users_bad_request` |
| A provider transport failure is 502 | #34 | `test_a_provider_transport_failure_is_still_502` |

## What a proposal means

The JSON handed to `model validate`, `model diff` and `model apply` is a **proposal**, stated rather
than implied since 0.9.6:

| Promise | Since | Test |
|---|---|---|
| `model` is the complete slot set; an apply **replaces**, never merges, so a partial one is refused (check a projection with `model validate --allow-partial` instead) | 0.9.6 | `test_apply_refuses_a_partial_model_instead_of_replacing_the_whole_one` |
| `summary.objective` must say something, or the proposal is refused as incomplete | 0.9.6 | `test_validate_rejects_a_complete_model_with_no_objective` |
| `decisions`/`challenges`/`opportunities` are **tri-state**: an omitted key leaves them untouched, `[]` deletes them, a list replaces them | 0.9.6 | `test_reasoning_merely_omitted_by_a_turn_is_preserved` |
| `exclusions` — an option considered and deliberately ruled out — is a fourth tri-state reasoning collection, added as a free field (no `format_version` bump); each item carries a content-derived id and `rests_on` (slot ids), the same DAG edge `derived_from` is for a decision | #599 | `test_reasoning_items_carry_a_stable_content_derived_id`, `test_propagate_flags_dependent_decisions_and_artifacts` |
| `thresholds` — a decision that has not fired yet, "at X, do Y" — is a fifth tri-state reasoning collection, added as a free field (no `format_version` bump); each item carries a content-derived id and a required, non-empty `rests_on` (slot ids), the same DAG edge `derived_from` is for a decision | #604 | `test_a_threshold_is_a_fifth_reasoning_item_with_a_stable_content_derived_id`, `test_propagate_flags_dependent_thresholds` |

## The session lock

The per-session write lock lives at `.requivo/locks/<slug>.lock`, outside the session directory it guards
(`.requivo/sessions/<slug>/.lock` before #113). **Not a session-format change** — `.lock` was never part of an
exported session, a stray one left by an older Requivo is inert and safe to delete, and one real limitation follows
from the move: two Requivo versions writing the same workspace at the same instant no longer serialise against each
other, since an older version still takes the retired in-session path (mitigation is ordinary — finish or close the
older process first; see `session-format.md` for the note).

| Promise | Since | Test |
|---|---|---|
| The lock path is `.requivo/locks/<slug>.lock`; `doctor` reports the lock root under `workspace.locks` | #113 | `test_doctor_reports_where_the_write_lock_lives` |
| `session delete` unlinks the lock file after releasing it, leaving no residue; a session removed by hand or by an older Requivo leaves an inert, empty lock file | #238 | `test_a_session_removed_through_session_delete_leaves_no_lock_residue` |

## Exit codes

| Exit | Means | Since |
|---|---|---|
| 0 | success | 0.1 |
| 1 | a clean, expected failure — invalid proposal, missing session, provider error, oversized request | 0.1 |
| 2 | bad arguments (argparse) | 0.1 |
| 3 | the work finished and its output could not be encoded | #164 |
| 4 | the work was done and part of the answer was unreachable | #86 |
| 130 | the operator interrupted the run (Ctrl-C / SIGINT) | #206 |

[cli.md](cli.md) documents what each means in full; this table is the promise. It follows [the
rules](#the-rules) above exactly: adding a code is free, moving a condition *onto* 0 is compatible,
and moving one off 0 or between two nonzero codes is breaking. Where a firm negative and a partial
one both apply, the firm one wins — `session verify` on a session that is both inconsistent and
missing a card exits 1, not 4.

| Promise | Since | Test |
|---|---|---|
| `4` is a shape of answer ("done, partly unreachable"), not a code per verb; a new condition of that shape gets 4, not a new number | #86 | `test_the_degraded_exit_code_is_published_as_a_value_not_as_a_name` |
| A `KeyboardInterrupt` reaching the CLI exits 130, including from inside `discover`'s interactive loop | #206 | `test_a_top_level_interrupt_on_an_existing_session_exits_130_with_no_traceback` |
| `run` and `docs` refuse an unreadable session-existence probe at exit 1 before any provider call, rather than routing to a new request or another session | #589 | `test_run_refuses_an_unreadable_session_before_discovery`, `test_docs_refuses_an_unreadable_session_before_selecting_a_default` |
| `requivo impact <slug> <unknown-slot>` exits 1 (invalid input), not 0 | #250 | `test_impact_refuses_an_unknown_slot_naming_it_in_details` |
| `session init` on a request over `MAX_INPUT_CHARS` (20,000) refuses before any provider call, at exit 1 | #255 | `test_an_oversized_request_is_refused_before_any_provider_call` |
| `discover -` reads stdin like its siblings and refuses an empty source before any provider call, rather than discovering on the literal text `-` | #360 | `test_a_dash_with_a_terminal_on_stdin_is_refused_rather_than_discovered_on`, `test_an_empty_stdin_is_refused_rather_than_discovered_on` |
| The eight write verbs (`brief`, `prd`, `stories`, `estimate`, `criteria`, `epic`, `release`, `answer`) no longer mine a `model.json`/`session.json` path for its parent directory's name — a path is opened or refused, never silently resolved to an unrelated same-named session | #402 | `test_resolve_slug_no_longer_mines_a_nonexistent_model_json_path`, `test_a_nonexistent_model_json_path_does_not_silently_use_an_unrelated_real_session` |
| The same closes for a bare directory argument on every `deterministic/` verb | #414 | `test_resolve_slug_refuses_a_directory_that_is_not_a_session`, `test_a_directory_reference_does_not_silently_use_an_unrelated_real_session` |
| `run`, `status` and `impact` take an optional session positional, resolving against the workspace's most recently written session, or refusing `session_not_found` naming `run` — both moves off exit 2 onto 0/1, so compatible | #540, #541 | `test_several_sessions_default_to_the_most_recently_written`, `test_no_session_raises_and_names_run` |

## Environment variables — **stable**, with one exception

`REQUIVO_WORKSPACE`, `REQUIVO_CONTEXT_DIR` and `REQUIVO_WEB_ALLOWED_HOSTS` are documented,
user-facing knobs; removing one, or changing what it means, is breaking under [the
rules](#the-rules) above. `REQUIVO_WEB_ALLOWED_HOSTS` keeps its `WEB` even though #508 widened it to
also govern the HTTP API's host allowlist — the name is the surface, and renaming it for accuracy
would break a deployment for one word.

| Promise | Since | Test |
|---|---|---|
| `REQUIVO_WORKSPACE`, `REQUIVO_CONTEXT_DIR`, `REQUIVO_WEB_ALLOWED_HOSTS` are stable knobs | 0.7 | `test_a_constructed_model_makes_no_env_read` |
| `REQUIVO_MODEL` is read first; bare `MODEL` is a **deprecated** fallback, read only when `REQUIVO_MODEL` is unset, so nothing already working stops working | #268 | `test_current_model_name_prefers_requivo_model_over_the_default`, `test_current_model_name_falls_back_to_bare_model` |
| `REQUIVO_OUTPUT_DIR` is **deprecated** — it configures the retired `out/` layout only `session migrate` still reads | #89 | — (removal target only; see the deprecations table) |

## CLI verbs

**The standing promise is the verb name.** Adding one is free; renaming or removing one is breaking.
A verb's `--help` *text* is not separately frozen by this page — a description or a flag can be
reworded between releases without that being a breaking change.

| Promise | Since | Test |
|---|---|---|
| `run`, `discover`, `answer`, `status`, `impact`, `docs`, `brief`, `prd`, `stories`, `estimate`, `criteria`, `epic`, `release`, `web`, plus the `deterministic/` verbs, are each a stable name | varies | `test_the_deterministic_package_still_registers_every_verb`, `test_every_registered_verb_appears_in_exactly_one_help_group` |
| `requivo run [request\|file\|-\|slug]` is additive and moves nothing else — it is a thin layer over `discover`'s loop and `answer`'s apply path, both still directly callable | #540, #541 | `test_no_session_raises_and_names_run` |
| `requivo docs [slug] [type...] [--all]` is additive — a thin loop over the seven existing generator verbs, each still directly callable | #543, #544 | `test_docs_all_flag_generates_every_document_skipping_the_menu` |
| `--help` groups verbs into three tiers ("Start here" / "For scripts and integrations" / "Plumbing"); presentational only, and this change specifically verified every verb's name, behaviour and `--help` text byte-for-byte unchanged (not a standing per-release guarantee — see above) | #546, #547 | `test_start_here_leads_the_rendered_help`, `test_every_verb_help_is_byte_identical_regardless_of_the_root_formatter` |
| `epic --json` was **removed** in the same change that added `epic --export-json` — it wrote a file under a name every sibling verb's `--json` uses for a stdout payload, so there was no grace version | #83 | `test_epic_no_longer_accepts_the_old_json_spelling` |

## The epic export envelope — **stable**, and versioned

`requivo epic <slug> --export-json` writes an envelope with its own `format` (`requivo-epic`) and
`version` (**2**), stable one level deeper than the `--json` payloads above: the key skeleton of the
envelope, of `epic`, and of each `issues` entry is the contract, recorded **per version number** so a
future bump adds a new skeleton beside the old one. `--github`/`--gitlab` tracker plans are stable
the same way, with one asymmetry: a shape we choose to change is breaking, a shape GitHub or GitLab
forces on us is documented as what it is.

| Promise | Since | Test |
|---|---|---|
| The envelope's key skeleton is pinned per `version` | v1 | `test_the_epic_export_skeleton_is_pinned_to_its_version` |
| `slug` and `source_revision` are top-level, and both tracker plans carry `source_revision` too; it names the basis the export was rendered from and is never a freshness verdict by itself — `requivo status --json`'s `artifacts.epic.stale` is | v2, #274 | `test_epic_export_carries_the_session_slug_and_the_revision_it_was_rendered_from` |
| `--github` degrades honestly (a tracking issue plus task list; `depends_on` stated in bodies; an idempotency label) | #97-family | `test_to_github_plan_degrades_honestly_and_is_idempotent` |
| `--gitlab` maps `depends_on` to native issue links | #97-family | `test_to_gitlab_wires_depends_on_as_issue_links` |

## Requivo Web's HTTP routes — paths stable, bodies not

| Route | Method |
|---|---|
| `/`, `/sessions/new` | GET |
| `/sessions` | POST |
| `/sessions/example` | POST |
| `/sessions/{slug}` | GET |
| `/sessions/{slug}/export` | GET |
| `/sessions/{slug}/discover`, `/sessions/{slug}/answers` | POST |
| `/sessions/{slug}/artifacts/{type}` | GET, POST |
| `/health` | GET |

The path, method and status are stable (status is governed by [the table
above](#http-statuses-in-requivo-web)); removing or moving a route is breaking. The response
**bodies** are not — every route renders HTML or an HTMX fragment, except `GET /health` and
`GET /sessions/{slug}/export`, which return data and are stable. Pinned by
`test_the_policy_this_app_sends_and_the_origin_guard_it_runs_agree`.

## Artifact filenames — **stable**, and part of the session format

| Type | File | Since |
|---|---|---|
| `brief` | `solution-assessment.md` | 0.1 |
| `prd` | `prd.md` | 0.1 |
| `stories` | `stories.md` | #519 |
| `estimate` | `estimate.md` | #519 |
| `criteria` | `acceptance-criteria.md` | 0.1 |
| `epic` | `epic.md` | 0.1 |
| `release` | `release-notes.md` | 0.1 |

Renaming a filename here needs a `format_version` bump, exactly as renaming a populated key does. The
type and filename deliberately differ for `brief`; both are stable, as two separate facts. `estimate`
and `stories` joined this table in #519 (`decision: the-estimate-graduates`) as additive: an older
Requivo reads a session carrying either row as a note, and format_version stays at 1. Pinned by
`test_both_analyses_are_registered_everywhere_a_saveable_type_is` and
`test_generating_the_estimate_saves_the_stories_it_was_reasoned_against_from_one_snapshot`.

## The Python import surface — the declared seam (#423)

The whole package tree used to be disclaimed as "not a published API" while `requivo-cloud` already
imported nine names across four modules. `tests/test_public_import_seam.py` pins that the names below
still resolve; moving, renaming or changing the signature of one costs a major version, priced
exactly like the CLI and the `--json` envelopes above.

| Category | Names | Since | Test |
|---|---|---|---|
| Services | `SessionService`, `UpdateResult`, `SessionEntry`, `SessionSnapshot`, `Readiness`, `RescopeResult`, `DiscoveryService`, `Generated`, `ArtifactService`, `UnknownArtifactTypeError`, `UnstatedSourceRevisionError`, `UnreadableSourceRevisionError` | #423 | `test_every_declared_seam_name_actually_resolves` |
| Protocols and the shipped repository | `SessionRepository`, `ReasoningProvider`, `FileSessionRepository` (its `__init__(root=...)` and `default_repository()`; `.store()` and `core.persistence.Store` are **not** part of this seam) | #423, #272 | `test_every_declared_seam_name_actually_resolves` |
| Boundary contracts | `EngineOutput`, `ModelProposal`, `SessionMeta`, `ArtifactStatus`, `RevisionRecord`, `UnexaminableEntry`, `Brief`, `PRD`, `AcceptanceCriteria`, `Epic`, `ReleaseNotes`, `Stories`, `EstimateDraft` | #423 | `test_every_declared_seam_name_actually_resolves` |
| Failure vocabulary | `RequivoError` and every subclass, `EngineError` | #423 | `test_every_declared_seam_name_actually_resolves` |
| Usage ledger (provider-neutral) | `UsageLedger`, `CallRecord`, `track_usage`, `record_call`, `current_ledger` | #423, #167 | `test_every_declared_seam_name_actually_resolves` |
| Repository conformance suite | `SessionRepositoryConformance`, `full_model` — needs `pip install 'requivo[testing]'` | #424 | `test_compatibility_md_declares_the_suite` |
| `requivo.render`, `requivo.paths`, `requivo.streams`, `requivo.cli`, `requivo.web` | **not** import-stable — each module's *behavioural* promise is made elsewhere here (exit codes, `--json`, HTTP routes, env vars); the Python-level shape of a name inside them can move in a minor | #423 | `test_compatibility_md_classifies_the_previously_silent_modules` |

**`py.typed`** ships (PEP 561 marker) so a downstream `pyright`/`mypy` run resolves these types.
Everything **not** listed above — `requivo.providers.anthropic` most pointedly — stays internal and
moves freely (`decision: deferring-the-neutral-provider-layer`).

**`DiscoveryService.claim_and_ground`'s return shape — breaking in 4.0.0** (#601, priced into this
major per #607): it returned three positional values, then four, then a `ClaimAndGround`
NamedTuple (`meta`, `grounding`, `cards`, `routing`) — unpacking is unchanged from the four-value
shape, but a caller still doing the original `meta, grounding, cards = ...` now raises `ValueError:
too many values to unpack`. Read it by **attribute**, not by position: a fact added later is then a
new field on the NamedTuple, not another break for every caller that unpacks.

| Promise | Since | Test |
|---|---|---|
| The declared names above resolve | #423 | `test_every_declared_seam_name_actually_resolves` |
| `src/requivo/py.typed` ships and is declared as package data | #423 | `test_the_package_ships_a_py_typed_marker`, `test_py_typed_is_shipped_as_package_data` |
| Pin exactly (`requivo==X.Y.Z`) is the stated consumption pattern | #423 | `test_the_recommended_consumption_pattern_is_stated` |

## What the sdist and wheel contain (#431)

The wheel is the installable artifact every promise on this page is verified against. The sdist
(`requivo-X.Y.Z.tar.gz`) is source form for a distro packager or anyone building from source, and ships **no `tests/`
directory** — the pre-decision default pulled 66 files in with none of their helper modules, collectible by nothing. A
distro packager verifying the built artifact should run the wheel, not the sdist.

| Promise | Since | Test |
|---|---|---|
| The sdist excludes `tests/` entirely | #431 | `test_the_sdist_ships_no_tests_directory_at_all` |
| The sdist still ships the package and its bundled assets | #431 | `test_the_sdist_still_ships_the_package_and_its_assets` |
| `MANIFEST.in` declares the prune | #431 | `test_manifest_in_declares_the_prune` |

## Deprecations

| What | Status | Since | Removal | Instead | Test |
|---|---|---|---|---|---|
| `model apply --allow-partial`, `model diff --allow-partial` | Removed — it merged nothing, it replaced | 0.9.6 | gone | `model validate --allow-partial` to check a projection | `test_apply_refuses_a_partial_model_instead_of_replacing_the_whole_one` |
| Legacy flag CLI (`python src/engine.py "…" --prd`) | Removed | deprecated 0.9.2 | 0.9.8 | `requivo discover` + `requivo prd` | — |
| `pc` command alias | Removed | deprecated 0.7.0 | 0.9.8 | `requivo` | — |
| Implicit `out/<slug>/` fallback | Removed — migration is explicit | deprecated 0.8.0 | 0.9.8 | `requivo session migrate`, then `.requivo/sessions/` | — |
| `/requivo-<skill>` plugin skill names | Renamed | 0.9.2 | gone | `/requivo:<skill>` | — |
| `REQUIVO_OUTPUT_DIR` | Deprecated | #89 | with `session migrate` | `REQUIVO_WORKSPACE` | — |
| Bare `MODEL` env var | Deprecated | #268 | not yet set | `REQUIVO_MODEL` | `test_current_model_name_falls_back_to_bare_model` |
| `epic --json` | Renamed, no grace version — it wrote a file, never emitted JSON | #83 | gone in the same change | `epic --export-json` | `test_epic_no_longer_accepts_the_old_json_spelling` |

The policy: anything deprecated keeps working for at least one minor version and names its
replacement here. From 1.0.0 it is removed only in a major; below 1.0 the floor was "not in a patch".

## What is explicitly *not* stable

- **Python internals, except [the declared seam](#the-python-import-surface--the-declared-seam-423).** `requivo.core`, `requivo.services`, `requivo.providers` and `` `requivo.deterministic` `` are importable and documented, not published. `requivo.deterministic`'s own `__all__` is plumbing for the offline verbs `cli.py` binds through, not an interface — see `test_the_degraded_exit_code_is_published_as_a_value_not_as_a_name` for the argument pinned.
- **The slug `derive_slug` turns a request into.** Tuning, not a promise (#245 changed the tokenizer). Sessions already on disk keep their names; an explicit `--slug` is never derived.
- **Prompt and context-card content.** Tuned continuously — see the [golden harness](evaluations.md). Two versions can reason differently about the same request.
- **Terminal output layout.** Parse `--json`, never the rendered view.
- **Requivo Web's response bodies**, except `GET /health` and `GET /sessions/{slug}/export` — see [the routes table](#requivo-webs-http-routes--paths-stable-bodies-not).
- **The `code` on Requivo Web's error banner.** Four presentational literals (`empty_request`, `invalid_request`, `not_found`, `internal_error`) outside the `RequivoError` vocabulary. Branch on the HTTP status instead.
- **CI's Python-version matrix beyond `requires-python`.** A leg (`3.14`, added #298) is not a floor change and not automatically a required check — `test_the_floor_is_read_from_pyproject_and_matches_what_ci_runs` pins the floor half; the leg-declared-consistently half that #298 also added was retired by #551 (one incident, never recurred, cited nowhere else).

Everything not on this page and not promised above is in neither column — a bug in this page, not a
licence to assume. File it.

## The `[api]` extra — not yet frozen

`requivo.api` (`pip install 'requivo[api]'`) exists behind an optional extra. **No route, method,
status or response shape it answers with is stable, and none of them is a breaking change to move.**
The freeze is a specific event, not a version number: `docs/decisions/0004-the-http-api-facade.md`
states the three preconditions gating it, after which this page gains its own API section in the
same stable form as everything above. Until then, this page's silence about the API is the promise —
there isn't one yet.
