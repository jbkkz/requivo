# Compatibility and deprecations

> What Requivo promises not to break, what it may change, and what is on the way out.

Requivo is versioned with [SemVer](https://semver.org/). From **1.0.0**, a break to anything on
this page costs a **major** version — not a minor, and never silently. Below 1.0 a minor was
permitted to change an interface, and the entries below dated 0.x were taken under that licence;
it is spent. The session format was outside it either way: it carries its own `format_version` and
a migration, and neither is a function of the release number. This page is the specific list.

Two things that rule leaves open are settled by `decision: a-release-is-justified-by-its-contents`,
and both are worth knowing before reading the list. **A *break* is correct code stopping working.**
An observable that moved on a path no correct code was on — an exit code that now refuses an
invocation which used to operate on the wrong session, a call that used to be paid for and discarded
— is graded `compatible` in its changelog fragment, with the moved observable named, and does not
cost a major; three majors in thirteen days were cut on exactly that shape before the distinction
was written down. And **a release is cut because of what is in it** — a user-visible capability, a
fix somebody is waiting on, or a blocking-class security finding — never because a count of merged
pull requests or an elapsed time was reached. Breaking changes batch and ship with the next release
that has a reason to happen; they are never the reason one happens. For an integrator the
consequence is the one the *Recommended consumption pattern* paragraph below already gives: pin
exactly, `requivo==X.Y.Z`, and bump it as a routine chore gated by your own tests and the conformance
suite.

## The session format is public

`.requivo/sessions/<slug>/` is the interface between the CLI, the Claude Code plugin, the Web app, and
anything you build on top. It is a **published contract**, not an implementation detail: the layout is
documented in [session-format.md](session-format.md), and a session written by one interface is
readable by all the others and by later versions.

Concretely, guaranteed:

- The directory layout: `session.json`, `request.md`, `model.json`, `revisions/NNNN-model.json`,
  `artifacts/`.
- `model.json` is a serialized model whose slot ids come from `framework/model_schema.json` — the same
  vocabulary `requivo schema` prints.
- `session.json` carries `format_version`. Today it is **1**.
- **A session written by an older Requivo keeps loading.** A field that has been retired is ignored
  rather than fatal; a field added since takes its default. Both are pinned by a test that loads a
  frozen 0.8.2 `session.json` verbatim.
- **A session written by a newer Requivo is refused, clearly** (`unsupported_format_version`,
  "upgrade requivo") rather than half-understood. `details` carries `{format_version,
  supported_format_version}` — *newer than what* is half the fact. A model authored against a newer
  **slot schema** is the second, independent version frontier and refuses under
  `unsupported_schema_version`; a session can be format-current and schema-ahead.
- **An older Requivo preserves a field it does not understand.** Since 0.9.4, an unknown key in
  `session.json` survives a round-trip: the older reader loads the session, mutates it, writes it back,
  and the newer version's field is still there. This is stronger than "readers ignore what they do not
  know", and it is what makes adding a field genuinely safe — before 0.9.4 the unknown key was dropped
  the first time an older Requivo wrote the file, so a mixed-version workspace quietly destroyed it.
  Keys that Requivo has *retired* are the deliberate exception: those are dropped, in `migrate_session()`.
- **The same is now true of `model.json`, and was not before** (#14). This page said "adding a
  field, anywhere" from the start and only `session.json` delivered it. `model.json` and
  every `revisions/NNNN-model.json` were read through the same contract an LLM reply is validated
  against, which is `extra="forbid"` on purpose, so a key added by a later version did not merely get
  dropped on write — the session could not be opened at all, and the refusal arrived as a Pydantic
  traceback rather than as a message naming the upgrade. Reads now go through a permissive sibling
  contract; the provider boundary is unchanged and still refuses an invented field, because there the
  unknown key means a drifted prompt and there is a retry that can correct it. Preservation is the
  same promise `session.json` carries: the key survives a **load, a mutation and a write back**, so
  an ordinary refinement turn through an older Requivo keeps it rather than quietly destroying it.
  Two limits are worth stating rather than leaving to be discovered. An *apply* **replaces** the
  slots, the summary and the questions with the ones the proposal carries, so an unknown key inside
  one of those does not survive a turn — it is superseded by a value this version built, not dropped
  by a reader that could not hold it. The reasoning layer (`decisions`, `challenges`,
  `opportunities`) and any key at the top level are carried, because a turn that says nothing about
  them is not a turn that deleted them. And an unknown **slot id** is still refused — that is
  `schema_version`'s frontier, described below, and absorbing a future slot as an unknown key would
  route it around the clear refusal that frontier exists to give.

What may change without a `format_version` bump:

- **Adding** a field, anywhere. Readers ignore what they do not know, and preserve it on write.
- **Retiring** a field that was never populated (this is how the unused session-level
  `prompt_versions` map was removed in 0.9.2).
- Adding a slot to the schema, a new artifact type, or a new value in a provenance field.

**The artifact-type half of that last line was a promise nothing kept until #260**, and it failed in
the shape this page warns about hardest. An `artifact_status` key naming a type the reading build has
no generator for was an *integrity problem*: `session verify` exited non-zero, `doctor` listed the
session as inconsistent, and `session import` refused a colleague's archive outright — while
`read_meta` opened the very same file without complaint. The diagnostic disagreeing with the loader
about one file is the worse of the two answers, and it is the identical defect #14 fixed one field
along for `model.json`. It would have bitten on the first new generator ever shipped, on every
session that generator had touched.

Such a type is now a **note** rather than a problem: named in `session verify` (`--json` carries it
under `notes`, a sibling of `problems`) and in `doctor` (`sessions.notes`), counted towards neither
`ok` nor the exit code, and accepted by `session import`. Nothing else about the entry is relaxed —
the recorded filename still goes through the bare-filename guard and the containment check, the file
still has to be there, and the revision it claims still has to exist.

**One deliberate narrowing, because tolerating widened a door that used to be shut.** Before this,
an archive carrying arbitrary `artifact_status` keys was refused; now a tolerated key is one this
build stores and prints for as long as the session lives. So the key has to *look* like an artifact
type — a plain lowercase name such as `risk-register`, at most 64 characters, the same shape every
key in `ARTIFACT_FILENAMES` already has. One that does not is refused as `unsafe_artifact_type`, a
code of its own so a consumer can tell *a type from the future* from *junk or a forgery*. A real
future generator is unaffected; nothing Requivo has ever written is.

What requires a `format_version` bump, an entry in the changelog, and a migration in
`migrate_session()`:

- Renaming, removing or changing the meaning of a *populated* field.
- Changing the directory layout or a file's role.
- Any change that would make an older session read *incorrectly* rather than merely incompletely.

Slot ids are a separate contract with its own `schema_version`, recorded on every session — and, since
0.9.5, actually enforced: a session authored against a *newer* slot schema is refused with the same
clarity as a newer `format_version`, instead of failing later as an `unknown_slot` error naming a slot
the user never typed. An older `schema_version` keeps loading.

**Another deliberate narrowing, for the same reason as the artifact-type one above: tolerating a name
widened a door that portability needed shut.** Since #221, `validate_slug` and `validate_filename`
refuse a Windows reserved device name (`con`, `prn`, `aux`, `nul`, `com1`-`com9`, `lpt1`-`lpt9`,
case-insensitively — the filename check on the stem before the first dot), on every platform. This is
**breaking**: a slug such as `con` was legal before this change and is refused now. It is refused
everywhere rather than only on Windows on purpose — a session slugged `con` and created on macOS or
Linux exported fine and then could not be materialized by `session import` on Windows at all, which is
the portability hole `.requivo/sessions/` is supposed to have closed. An existing session already
holding one of these names on disk is unaffected — nothing here reads or rewrites `session.json` to
enforce it — but `validate_slug`/`validate_filename` refuse it on any *new* creation or explicit
`--slug` from this version on.

## The `--json` outputs are public

**Every `--json` output is public — all sixteen of them, and the structured error envelope
(`{code, message, path?, details?}`).** They follow the same rule as the session format: fields get
added, populated fields do not change meaning without a note in the changelog.

| | | |
|---|---|---|
| `requivo status` | `requivo session show` | `requivo model validate` |
| `requivo doctor` | `requivo session verify` | `requivo model apply` |
| `requivo session init` | `requivo session export` | `requivo model diff` |
| `requivo session list` | `requivo session import` | `requivo artifact save` |
| `requivo session migrate` | `requivo session rescope` | `requivo artifact list` |
| `requivo session delete` | | |

Written out in full rather than abbreviated, because the guard below compares this table against the
parser literally, and an abbreviation is a fragment a test can match by accident.

Error `code` values are stable identifiers — assert on the code, never on the message text.

**This used to name six of the fourteen, and justify the six as "what the Claude Code plugin
drives".** That sentence was wrong in both directions, by three entries each: the plugin drives
`session init`, `model validate` and `artifact save`, which were not listed, and does not drive
`model diff`, `artifact list` or `session list`, which were. Eight outputs were in neither column —
and #84 made a breaking change to one of them before anyone noticed there was no promise to break.

So the perimeter is now the whole set rather than a subset, for a reason worth stating because it is
the reason a subset keeps failing: **a subset needs a boundary somebody can check, and the only one
this page ever offered was a claim about another artifact's current contents.** Nothing tested it, it
was false when written, and it would have gone on being false every time the plugin changed.
`test_every_json_verb_is_inside_the_promise` is the guard, and it can only be trivial because the
answer is *all of them* — a subset would need the guard to encode the boundary, and the boundary is
what drifts.

The promise is additive, not a freeze: nothing here says an output may never gain a field. What it
says is that a populated field will not quietly change meaning, and that a change of shape is
announced. That is cheap to keep for sixteen outputs and was never the expensive half.

**What "public" means for a payload, in one testable sentence: a payload's top-level key set, and
the JSON types of those values, are the contract.** Removing a top-level key, renaming one, or
changing the type a key holds is breaking, and needs a row in the ledger below. Adding one is free,
and still gets a row, because every additive change on this page already has one.

Nested shapes are deliberately outside that sentence. The behavioural tests exercise the fields that
carry weight, and a contract reaching two levels down would be a restatement of the code rather than
a promise about it.

`test_every_public_json_payload_keeps_its_recorded_top_level_shape` is what stops that being a
sentence, and it is a *second* guard rather than a widening of the first: the one above
(`test_every_json_verb_is_inside_the_promise`) checks that this page names every verb, which is
membership and says nothing about what a verb prints. The new one runs each of the sixteen against a
fixture workspace and compares the top level of what it printed with a recorded key-and-type table.
The two failure directions are reported separately, because they are not the same event — a key that
vanished or changed type is a break, and a key that appeared wants one line added to the record.

Why membership alone was not enough: four of the breaking changes below (#87, #84, #88, #107)
shipped in the 1.0.0 release alone. All four were deliberate and all four are correctly recorded
here, because somebody audited this surface by hand while the 1.0 contract was being cut — which is
the point rather than a mitigation. Nothing in the tree would have gone red if a fifth had been made
by accident, or made deliberately and its row forgotten.

**One payload on this page is conditional, and nothing said so until now.** `requivo status --json`
carries `slug`, `readiness`, `understanding`, `questions`, `summary` and `remaining_gaps` always;
`revision`, `context_cards` and `artifacts` are layered on **only when the reference resolves to a
canonical session**, because a bare `model.json` has no session to read them from. Both forms are
public and both are pinned. A consumer that passes a file path must not read the three.

**A code carries one fact, and one `details` shape.** That is what makes the advice above safe to
follow: matching a code and then reading a key out of `details` has to work for every payload
carrying it. Two changes in 0.10.0 were needed to make it true.

- **`empty_selector_token` split into two codes.** It carried two facts with two shapes: an empty
  *token inside* a selection (`{selector, position}`) and a selection that is *itself* empty
  (`{selector, tokens}`). A consumer matching the code and reading `details["position"]` got a
  `KeyError` from a payload that correctly carried the code it matched. Since 0.10.0:

  | Condition | Code | `details` |
  |---|---|---|
  | a stray comma — `--context "a,,b"` | `empty_selector_token` | `{selector, position}` |
  | a selection that selects nothing — `--context ""` | `empty_selection` | `{selector, tokens}` |

  **If you match `empty_selector_token`, match `empty_selection` alongside it.** The message is
  unchanged for both; only the code moved for the second case, and `position` is now guaranteed
  present on every `empty_selector_token` payload.

- **`no_context_cards` is new.** An install with no context cards at all now refuses rather than
  reasoning with an empty product context. It is distinct from `unknown_context_card` (the card you
  named is not there) and from `context_unreadable` (we could not look): here we looked, at every
  root, and there is nothing. `details` carries `{roots}` — the directories searched.

- **`no_context_cards` now also reaches session creation** (#41). It was wired into the calls that
  *read* the cards and not into `resolve_cards`, the validator every surface runs on the way in — so
  on a card-less install, naming a card at creation answered `unknown_context_card` (a name you
  typed wrongly) and the very next call answered `no_context_cards` (an install that is incomplete).
  One condition, two codes, and the misleading one arrived first. A caller matching
  `unknown_context_card` on a creation path should match `no_context_cards` alongside it; nothing
  changes on an install that has cards, where an unknown name is still `unknown_context_card`.
  Passing *no* selection at all is unaffected and still resolves to the every-card sentinel — the
  install is caught when the cards are read, as before.

- **`unsafe_selector_token` is new** (#40). A selector token — a context card name or a slot token —
  carrying a control character is refused rather than echoed back. `details` carries
  `{selector, position}`, the same shape as `empty_selector_token`. This can turn a *previously
  accepted* stored value into a refusal: a `session.json` whose `context_cards` holds a name with an
  embedded newline now reports `unsafe_selector_token` from `doctor` and `session verify` instead of
  printing it. No name Requivo itself writes is affected — `resolve_cards` has always resolved a
  selection against the installed cards, so such a value can only have arrived by import or by hand.

- **`doctor --json` gained `sessions.non_sessions`** (#67). Additive: a consumer reading only the
  existing `sessions` keys is unaffected, and on a workspace Requivo alone has written it is always
  `[]`. It carries what is under the session root and is *not* a session — one object per entry with
  `name`, `kind`, `entries`, `entry_count`, `error` and `slug_shaped`. See
  [Something here that is not a session](cli.md#something-here-that-is-not-a-session).

  **`null` and `[]` mean different things**, at both levels. The key itself is `null` when the session
  root could not be listed at all, matching `sessions.total`; `[]` means it was listed and holds
  nothing but sessions. Within an entry, `entries: null` with an `error` is a directory that could not
  be listed, and `entries: null` with no error is a `file` or an `other` — there is nothing to look
  inside. Branch on `error`, never on emptiness.

  Nothing is deleted, moved or rewritten as a result, and no field states a conclusion: there is no
  `is_lock_ghost`. `slug_shaped` is the one derived value and it is a property of the name — whether
  `create_session` can be asked for it — not a claim about where the entry came from.

- **`doctor --json` gained `sessions.unexaminable`** (#80). Additive, and `[]` on any workspace where
  every entry could be examined. It carries the names under the session root whose examination
  *raised*, one object per entry with `name` and `error` — a directory the process cannot stat into
  is the case it was found on.

  **It is deliberately not `non_sessions`, and a consumer must not merge them.** That key states a
  fact — *this is not a session* — and here nobody established one; the entry may well be a session.
  Nor is it in `total`, which stays the count that could be **confirmed**. `null` and `[]` read
  exactly as they do on `non_sessions`: `null` when the session root could not be listed at all,
  `[]` when it was listed and every entry in it could be examined.

  What changed for a caller is narrower than it looks and worth stating plainly: this condition
  previously produced `readable: false` with `total: null` for the **whole root**, which was broader
  than what had failed. A consumer branching on `sessions.readable` sees `true` where it used to see
  `false`, on a workspace where one entry is unexaminable and the root itself is fine.

- **`doctor --json`'s `locks.unexpected` no longer names a `<slug>.discovering` file** (#391).
  Narrowing, not a shape change: the key is unchanged and still an array of names, and the top-level
  `locks` skeleton is untouched. `_discovery_guard` (`services/discovery.py`, #209) writes that file
  into the same directory as `<slug>.lock` and never unlinks it — the identical POSIX reasoning
  `session_lock` already uses to leave its own `.lock` file behind for a deleted session —
  and `scan_lock_root` had simply never been taught the second shape, so it read every ordinary
  discovery's own guard file as "not a lock file Requivo recognises". A consumer that flagged every
  name in this array as unrecognised now sees one fewer false positive per session that has ever run
  a first discovery; a consumer counting `len(unexpected)` sees a workspace-dependent decrease.
  Nothing that was a genuine finding before — a stray file, a directory, a symlink, a malformed name
  — stopped being reported.

- **`doctor --json`'s `locks.unexpected` no longer names the lock or guard file of a session already
  on disk under a Windows reserved device name** (#401). Narrowing, on the same terms as the #391
  entry above it: the key is unchanged and still an array of names, and the `locks` skeleton is
  untouched. `scan_lock_root` classified each entry's stem with the *creation-time* slug rule, which
  refuses `con`, `nul`, `lpt1` and their siblings unconditionally — while both writers of that
  directory apply the conditional read-time rule #372 introduced, and happily write `con.lock` and
  `con.discovering` for a session that already occupies the name. So this array named two files
  Requivo's own code had written, under the wording "not a lock file Requivo recognises". A consumer
  sees `locks.total` count one more lock, `locks.unexpected` name two fewer entries, and
  `locks.unmatched` unchanged, on a workspace holding such a session — and no change at all on a
  workspace that holds none, which is every workspace created on Windows, where the OS itself
  refuses to make such a directory. A lock or guard file whose stem is a reserved name that **no**
  session occupies is still reported, as is a stray file, a directory, a symlink or a malformed
  stem.

- **`doctor --json`'s `locks.unexpected` no longer names a reserved-device-name lock or guard file
  whose session has since been deleted, and `locks.unmatched` names it instead** (#409). Narrowing
  again, same terms as the two entries above it. `_is_lock_stem` used to re-ask the #401 conditional
  rule (`_refuse_new_reserved_slug` against `session_root()`), so a `nul.lock` file `session_lock`
  legitimately wrote while a `nul` session was open kept reading as "not a lock file Requivo
  recognises" once that session was removed — the entry's classification flipped on a directory it
  does not even name. The stem's *shape* alone decides now whether either writer here could have
  produced it; whether a session still matches it is `locks.unmatched`'s question, answered
  separately, exactly as `scan_lock_root`'s own docstring always said it should be. A consumer sees
  `locks.total` count one more lock and `locks.unexpected` name one fewer entry for such a stem, with
  the stem itself appearing in `locks.unmatched` instead — on any workspace that has ever locked a
  reserved-name session and later removed it, and no change at all on a workspace that has not,
  which is every workspace created on Windows. A stray file, a directory, a symlink or a malformed
  stem is still reported exactly as before.

- **`doctor --json`'s `sessions.non_sessions[].slug_shaped` is `true` now for a non-session directory
  whose name is a Windows reserved device name** (#408). Widening, on the same terms as #401 above:
  the key is unchanged and still a bool per entry. `_describe_non_session` asked `is_slug` — the
  unconditional creation-time refusal — so a `con` directory holding no `session.json` read
  `slug_shaped: false` and `doctor`'s `[name taken]` hint never named it, even though
  `create_session('con', ...)` reads straight through the very same directory under #372's
  conditional read-time rule and loses its rename to it. The field asks that read-time question now
  — in practice `_shape_only`, since the directory being described already occupies the path in
  question, so the conditional half of the rule could never actually refuse — matching `lock_path`,
  `_child_of`, `web/dependencies.py`, `services/discovery.py` and `scan_lock_root` (#409, above). A
  consumer sees `slug_shaped: true` where it used to see `false` for such a directory, and the
  `[name taken]` hint follows it; nothing changes for a non-reserved name.

- **`session list --json` is an object, not an array** (#87). **Breaking**, and the one change on this
  page that a parser cannot survive: the payload was a bare array of rows and is now
  `{"sessions": [...], "degraded": <int>, "session_root": "<path>"}`. The rows are unchanged — the
  same key set, in the same order — so the migration is one level of indirection:
  `jq '.sessions[]'` where you had `jq '.[]'`.

  It was the only array among the fourteen `--json` payloads, and an array has no top level, so no
  field could ever be added to it. `degraded` recovers no fact — every row already carries `readable`
  and `error`, so the count was always derivable — what it buys is that **exit 4 is readable on
  stdout** rather than only signalled. `session_root` is new and is the absolute path the listing was
  taken from.

- **`session import --json` renames two keys** (#84). **Breaking.** It answered
  `{"imported": ..., "into": ...}` where every sibling verb answers `slug` and `path`; it now answers
  `{"slug": ..., "path": ..., "replaced": ...}`. A consumer looping over the session verbs and
  reading `row["slug"]` got a `KeyError` from the one that imported it.

  **`path` is not `into` renamed.** `into` was the sessions *root*; `path` is the session directory,
  which is what `session init --json`'s `path` already means. Keeping the old value under the agreed
  name would have preserved the defect — one key, two meanings — and made it harder to see, because
  the spelling would finally match.

- **`doctor --json` spells one enum value differently** (#88). **Breaking.**
  `output.streams[].state` answers `will_crash` where it answered `will-crash`; `safe`, `lossy` and
  `unknown` are unchanged. It was the only hyphenated value in any `--json` payload — every other
  enum here is one word or underscore-joined — so a consumer mapping a state onto an identifier had
  exactly one value to special-case. Shipped hyphenated in 0.11.0 and corrected in the release after,
  which is why this is a break rather than a tidy-up.

- **`artifact list --json` is an envelope, not the bare map** (#107). **Breaking.** The payload was
  the artifact map itself — `{"prd": {…}}`, its top level keyed by artifact type — and is now
  `{"slug": "<slug>", "artifacts": {"<type>": {…}}}`. The rows are unchanged — the same key set, in
  the same order — so the migration is one level of indirection: `jq '.artifacts'` where you had
  `jq '.'`.

  It was the last of the fourteen `--json` payloads with no real top level, and the argument is
  #87's one shape along. A top level keyed by *data* has the same property an array has: every key
  is a row, so there is nowhere to put a field that is not one. The natural consumer read is
  `for t, info in payload.items()`, and a key added later is both ambiguous with a future artifact
  type and breaks that loop. Holding the argument for an array and not for a map is not defensible.

  `slug` recovers no fact the caller was missing — they named the session to ask the question — and
  it is the **resolved** name rather than the body's, the reading `session verify` and `session
  import` already give it. What the top level buys is that the payload can gain a field at all. A
  session with nothing saved now answers `{"slug": …, "artifacts": {}}` where it answered `{}`,
  which named neither the session nor the fact that the question had been answered.

- **`session verify --json` gained a `session` object** (#97). Additive; every existing field keeps
  its name and meaning, so a consumer reading only `slug`, `ok`, `problems` and `context_cards` is
  unaffected on a workspace where every session can be examined. It is present on every payload and
  reads `{"checked": true, "error": null}` for a session that was examined.

  It exists because **`problems: []` spells two different facts** — *checked, nothing wrong* and
  *nothing was checked* — and an empty list cannot distinguish them. `session_exists` used a bare
  `Path.exists()`, which re-raises `EACCES`, so the verb used to answer that case with a traceback;
  it now answers it. **Branch on `session.checked`**, never on the emptiness of `problems`, exactly
  as `context_cards.checked` already required.

  The verb exits **4** in that state, not 1, under the rule below: 1 says *I checked and it is
  broken* about a session nothing looked at. The precedence rule is unchanged — real `problems`
  beside an unexaminable path still exits 1.

- **`session list --json` gained two fields, and a row can now be degraded** (#62). Every row carries
  `readable` (a boolean) and `error` (the reason, or `null`) alongside `slug`, `revision`, `provider`
  and `updated_at`. Both are additive, so a consumer reading only the original four is unaffected on
  a workspace where every session loads.

  What is new is that the command **no longer fails for the whole set** when one session cannot be
  read — a `session.json` from a newer Requivo, or one left half-written by a crash. It used to exit
  1 with a single message and no payload at all; it now prints the complete listing and exits **4**.

  A degraded row **keeps the same key set** as a healthy one, with `null` in `revision`, `provider`
  and `updated_at` rather than a missing key or a plausible `0`: a consumer looping over the payload
  reading `row["revision"]` gets `None` from a row it was handed deliberately, not a `KeyError`.
  **Branch on `readable`**, and treat `null` in those three as *we could not read this*, never as a
  value. A session at revision 0 is `readable: true` with `revision: 0` — not analysed yet is a
  normal state and is not this one.

  **A degraded row can now name an entry that is not known to be a session at all** (#80). Until then
  every row came from `list_session_slugs`, so even a degraded one was a name with a `session.json`
  behind it. An entry whose examination *raised* — a directory the process cannot stat into — is now
  a degraded row too, because the alternatives were to drop it, which loses it silently, or to
  exclude it and take the whole listing down with the exception, which is what used to happen: the
  command exited 1 with an empty payload and a `PermissionError` traceback, every healthy session
  invisible. Nothing about the row's shape changed, and `readable` is still the field to branch on.
  What a consumer must not do is read `slug` on a `readable: false` row as a name it can pass to
  another verb — that was already true for a `session.json` whose name is not a valid slug, and this
  widens the set slightly.

  The **terminal** output of the same command changed alongside it, in the same way `#40` changed
  `doctor`'s: a `slug`, `provider` or `updated_at` carrying a control character is now escaped rather
  than echoed, because all three are read back out of `session.json` and could otherwise write what
  reads as a second, authoritative row at column 0. A value that is already one safe line is returned
  byte-for-byte, so no session Requivo itself wrote is affected — such a value can only have arrived
  by import or by hand. `--json` is unchanged and was never affected: `json.dumps` escapes it.

- **`session show`'s terminal output escapes the same way, in eight fields** (#70). The identical
  defect, in the other verb. #62 found it while fixing the listing and reported it rather than riding
  it in on that diff, which is why the two land as separate changes. `slug`, `session_id`, `created_at`,
  `updated_at`, `provider`, `model_name`, each key of `artifact_status` and each artifact's
  `filename` are all bare strings in `session.json`'s body, and all eight reached the terminal
  unescaped. `current_revision`, an artifact's `revision` and its `stale` flag are untouched and need
  nothing — `read_meta` refuses a string in an `int` or a `bool` before the render runs.

  It is **eight** rather than the five the issue names: #62 listed the five that are `SessionMeta`
  scalars, which left out `slug` and the two fields that live on `ArtifactStatus` and its dict key.
  Recorded here because a count in a compatibility note is the thing a later reader checks their own
  work against.

  **`artifact list` renders two of the same fields and is fixed alongside it** (#70). It prints the
  `artifact_status` key and the `filename` at the same fixed column, off the same file, through
  `ArtifactService.list`. It is outside the issue's own footprint and is named here for that reason:
  fixing one verb's copy of a two-field render turns the rule from *a persisted value is escaped
  where it is shown* into *it is escaped where somebody looked*.

  Same rule as above, so the same guarantees: a value that is already one safe line comes back
  byte-for-byte and no session Requivo itself wrote changes, and `--json` is unaffected on all three
  verbs.

  **The reason `--json` is unaffected is narrower than #62 stated it**, and is corrected here rather
  than left standing. `_session_list_line`'s docstring, and #70's own issue text, both said
  `json.dumps` defaults to `ensure_ascii=True` and therefore escapes a control character — the bullet
  above says only that `json.dumps` escapes it, which is true and is why it needs no correction of its
  own. Measured, the flag is not
  what protects a newline: JSON's grammar forbids a literal control character below `U+0020` inside a
  string, so `\n` is escaped whether the flag is on or off. What the flag decides is the *non-ASCII*
  half of the guarded range, `U+007F`–`U+009F` — which carries `NEL` (`U+0085`), a line terminator
  `str.splitlines()` and some terminals honour, and `CSI` (`U+009B`), an escape introducer. So the
  default **is** load-bearing, for a different set of characters than was written down, and a test
  now probes both halves; one probing only with a newline is green either way and pins nothing.

- **`artifact save` without `--revision` is refused, and carries `unstated_source_revision`** (#6,
  #57). It used to succeed, filling the omission in with the session's current revision and recording
  `stale: false` — an answer that could not come out any other way, about a revision the caller never
  claimed to have read. Every documented invocation already passes the flag, so this only reaches a
  caller relying on the undocumented default, which was being handed a fabricated provenance. The
  condition itself is new rather than moved: nothing previously carried a code here, because nothing
  previously failed.

  **The code moved once before it shipped.** #6 raised the refusal under `invalid_session`; #57 gave
  it one of its own. Moving a condition to a new code is breaking under the rule at the foot of this
  section, and it is recorded as such — but no released version ever answered `invalid_session` here,
  because #6 and #57 land in the same release, so there is no consumer to break. What forced the
  inheritance was that a new code needs a row in the Web's status table and that file was held by
  another change in the same round; the precision sat in the exception *type* meanwhile, which a
  caller reading a serialized envelope cannot see. `UnstatedSourceRevisionError` is still an
  `InvalidSessionError` subclass, so catching the class is unaffected either way.

  `details` carries `{slug, type, source_revision, current_revision, cause}` — the same five keys as
  the refusal for a source revision that cannot be *read*, which carries `unreadable_source_revision`
  since #82 and kept `invalid_session` until then. Both are
  always present: here `source_revision` is `null` because none was stated and `cause` is `null`
  because no underlying failure occurred, and on the unreadable side `cause` names the exception type
  and its text. The shared shape **survives the split as a choice**, not an obligation: one code, one
  shape no longer binds two codes together, and narrowing this payload would break a consumer reading
  `details["cause"]` across both arms for no gain. `opaque_origin` and `origin_mismatch` below are the
  same answer to the same question. `tests/test_artifact_provenance.py` asserts that the two codes
  differ and that the two key sets still agree.

- **`cross_site_request` split into six codes** (#52). Requivo Web's cross-site guard raised one code
  for six distinct facts, and their `details` payloads had five different shapes between them — the
  exact condition this rule was written for. A consumer matching `cross_site_request` and reading
  `details["origin"]` got a `KeyError` from a payload that correctly carried the code it matched.

  | Condition | Code | `details` |
  |---|---|---|
  | no host could be read — absent, empty, or not an authority | `undetermined_host` | `{host_header_present, host_header, hint}` |
  | the host was read and is not one this server answers to | `host_not_allowed` | `{host, hint}` |
  | the browser's `Sec-Fetch-Site` says another site | `cross_site_fetch` | `{sec_fetch_site}` |
  | `Origin: null` | `opaque_origin` | `{origin, host}` |
  | the origin is not the host's trust domain | `origin_mismatch` | `{origin, host}` |
  | the request token was absent or wrong | `missing_request_token` | `{}` |

  **All six are still 403**, and all six are still `CrossSiteRequestError` subclasses, so a caller
  catching that class or matching on the status is unaffected. `cross_site_request` survives as the
  family base and keeps its status row, but **nothing raises it any more** — if you match that string,
  match the six above instead. `opaque_origin` and `origin_mismatch` share a `details` shape and are
  still two codes: a shared shape is not a shared meaning.

  This was inert before it was fixed, and deliberately noted anyway. Requivo Web does not serialize
  `details` — a refusal renders as HTML — so no consumer could observe the inconsistency, and an
  argued exception to the rule was the other defensible answer. What decided it is that the cost was
  already being paid: both #43 and #45 had to distinguish their new arm **by message**, which is the
  one handle this document tells you never to use.

Adding a code is not a breaking change under this policy, but *moving a condition to a new code* is,
so both are noted here and in the changelog rather than only in the latter.

- **`session migrate --json` gained `interrupted` and `errors`** (#262). Additive: `migrated` and
  `skipped_already_present` keep exactly the meaning they had. `skipped_already_present` did carry an
  ambiguity worth stating plainly, though not a payload-shape one — an occupied slug at
  `current_revision` 0 (a previous migrate that claimed the slug and crashed before applying the
  model) used to be indistinguishable in that list from a genuinely completed migration; it now
  reports separately, under `interrupted`, naming the recovery step. A legacy session whose
  `model.json` will not parse used to abort the whole command with no JSON printed at all; it is now
  named under `errors` and every other legacy session still migrates. The command's exit code is `4`
  (`EXIT_DEGRADED`, the same code `session list` and `session verify` already use) when either of the
  two new lists is non-empty, where it was always `0` before.

- **`session migrate --json` gained `unreadable`** (#411). Additive, on the same terms as `errors`
  above: a legacy directory the process could not stat into (a permission bit denying it, the
  reproduced case) used to escape the scan that builds the migration list as an uncaught
  `PermissionError` — no JSON printed, no receipt, an undefined exit code rather than a documented
  one. It is now named under `unreadable`, with the OS error, and every other legacy session in the
  sweep still migrates. The exit code is `4` under the same rule the two lists above already use: a
  crash was never a documented `0`, so converting it into a receipt is additive rather than a
  condition moving off a promised code.

  A whole-root failure — the legacy `out/` root itself unlistable, rather than one entry inside it
  — is the same reasoning one level up, found in review of this same change: it used to crash with
  the identical undefined code, and now exits `1` with a clean, one-line refusal (or `--json` error
  envelope) naming the root. Additive for the same reason — never a documented `0` — and `1` rather
  than `4` because nothing here was even examined: "no answer", not "the answer is incomplete".

### HTTP statuses in Requivo Web

The Web maps each code to a status, and **every code has an explicit mapping** — the table used to
default to 400, so a code added in one place reached the browser as "your request was bad" whatever
it actually meant. Consumers scripting against the Web should note the four that moved off that
default in 0.10.0, and the one that is new:

| Code | Was | Now | Why |
|---|---|---|---|
| `context_unreadable` | 400 | 500 | the server cannot read its own card directory |
| `no_context_cards` | — | 500 | the install shipped no cards; nothing the caller sent caused it |
| `provider_output_invalid` | 400 | 502 | upstream would not hold the contract, after every retry |
| `session_locked` | 400 | 503 | the write never started; retrying it unchanged is correct |
| `session_exists` | 400 | 409 | a conflict with the store's state, like `revision_conflict` |

An unrecognised code is now a 500 rather than a 400, for the same reason: "we could not classify
this" is not evidence that the caller erred.

`unstated_source_revision` is new in the Web's status map and is a **400** (#57). The request is
incomplete — the one fact only the caller holds was not stated — and nothing about the session is
wrong, so it sits with the other 4xx rows rather than with the 409 conflicts, which are about the
store's state. **No status moves for it**: the condition answered 400 under `invalid_session` before
the split, so what changed is the code the payload carries, not the number a client branches on.

### `invalid_session` is a family, and every arm now names its fact (#82)

`invalid_session` carried seven facts across eight raise sites with four `details` shapes, and no key
was present on all eight — `details["slug"]` raised `KeyError` on three of them. This page told
consumers to *assert on the code, never on the message text*, and then promised a condition
(`invalid_session`, "upgrade requivo") that the code could not distinguish from a corrupt zip. The
only handle separating them was the one handle this page forbids.

`InvalidSessionError` is now a **family that nothing raises directly**, so `except InvalidSessionError`
is unaffected and no caller has to enumerate ten names. Each arm carries its own code, its own
`details` shape, and its own HTTP status:

| Code | Condition | `details` | Status |
|---|---|---|---|
| `unsupported_format_version` | `session.json` written by a newer Requivo | `{format_version, supported_format_version}` | 409 |
| `unsupported_schema_version` | model authored against a newer slot schema | `{schema_version, supported_schema_version}` | 409 |
| `session_unreadable` | `session.json` truncated, mis-encoded or not JSON, **or** the session's write lock could not be opened | `{slug}` | 500 |
| `model_unreadable` | `model.json` or a `revisions/NNNN-model.json` truncated, mis-encoded, not JSON, or not a valid model | `{path}`, plus `slug` and `revision` when known | 500 |
| `artifact_revision_out_of_range` | artifact recorded against a revision the session lacks | `{slug, source_revision, current_revision}` | 500 |
| `unstated_source_revision` | `artifact save` stated no source revision | `{slug, type, source_revision, current_revision, cause}` | 400 |
| `unreadable_source_revision` | the stated source revision cannot be read | the same five keys | 500 |
| `inconsistent_archive` | an imported archive fails the integrity check | `{slug, problems}` | 400 |
| `unreadable_archive` | the file is not a readable `.zip` | `{archive}` | 400 |
| `invalid_archive` | an imported archive opens but is not shaped like an export | `{problem, …}` | 400 |
| `import_move_failed` | the validated session could not be moved into place | `{slug}` | 500 |

**The arms deliberately do not share a `details` shape.** Padding ten payloads to one key set would
answer the `KeyError` by stating facts nobody measured: three of them identify no session at all,
because none has been identified yet when a zip will not open, and a `slug: null` there is the
plausible-wrong-answer form of the bug the split removes. Branch on the code, then read the shape
documented for that code. `unstated_source_revision` and `unreadable_source_revision` do share five
keys, and that is a decision rather than an obligation — the same answer #52 gave for `opaque_origin`
and `origin_mismatch`: a shared shape is not a shared meaning.

**When #82 split the family it had nine arms, and six of the nine changed status.** Each was the
misattribution #34 fixed for `context_unreadable`, one condition further along: everything under
`invalid_session` answered 400, so a reader opening a session written by a newer Requivo was told
*your request was bad* — as was one whose `session.json` would not parse, and one whose artifact
history was incomplete. Those are facts about the store.

That sentence is **history and stays at nine**. #101 later added a tenth arm (`invalid_archive`),
which changed no status — it was already 400 under `invalid_model` and is 400 now — so restating the
split as "six of the ten" would claim something #82 did not do. #204 added an eleventh
(`model_unreadable`) for the same reason and with the same effect on the history: it is a condition
that previously had *no* code at all, because a pydantic `ValidationError` escaped the vocabulary
entirely, so nothing moved from one code to another. The two numbers in this section count
different things on purpose, and the arithmetic only closes if you read which:

| | Count | What it counts |
|---|---|---|
| six of **nine** | historical | what #82 moved, on the family as it stood then |
| four keep 400 | **present** | the family as it stands now, including #101's tenth arm and #204's eleventh |

So, present tense: a client that branched on 4xx should expect **409** for the two version frontiers
and **500** for the five store-state arms (`session_unreadable`, `model_unreadable`,
`artifact_revision_out_of_range`, `unreadable_source_revision`, `import_move_failed`). **Four** conditions keep 400 and are the ones
that really are about the request: the three archive arms, because the caller did hand us the
archive, and `unstated_source_revision`, which never moved. The family base keeps a row at **500** — nothing raises
it, but a nominal number is still one a reader sees, and 400 was the wrong one to leave there.

**This is breaking under the rule at the foot of this section** — moving a condition from one error
code to another, and changing an HTTP status. It is taken **in** 1.0.0 deliberately: that is the
release which draws the boundary, and after it the same change costs a major version, or a sentence
on this page that nobody can keep.

No status in that table moves for #41, but one **condition** crosses it. `POST /sessions` naming a
context card on an install that has none used to answer **400** — the reader was told their request
was bad — and now answers **500**, because the code it raises changed from `unknown_context_card` to
`no_context_cards`. That is the split this table exists to hold: nothing the caller sent caused an
install to ship without cards, and the previous 400 was the same misattribution one call earlier
than the one #34 fixed. A client scripting session creation and branching on a 4xx should expect a
5xx for this condition.

One populated field has changed meaning under that rule, and the changelog carries the note:
`doctor --json`'s `sessions.total` is `null`, not `0`, when the session directory could not be read
at all. It reported `0` before, which was indistinguishable from an empty workspace — a reader
gating on `total == 0` was being told "you have no sessions" by a check that had not managed to look.
The sibling `sessions.readable` says which case you are in.

### The import path names the archive, not the model (#101)

`session import` refused **eight** conditions under `invalid_model` — documented as *"a proposed model
is structurally or semantically invalid"*, and answering 400. None of them is about a model.

| Condition | Was | Now | Status |
|---|---|---|---|
| the archive contains no files | `invalid_model` | `invalid_archive` (`problem: empty`) | 400 |
| more entries (files and directories together) than `MAX_ARCHIVE_ENTRIES` | never checked | `invalid_archive` (`problem: too_many_entries`) | 400 |
| more files than `MAX_ARCHIVE_FILES` | `invalid_model` | `invalid_archive` (`problem: too_many_files`) | 400 |
| expands past `MAX_ARCHIVE_BYTES` | `invalid_model` | `invalid_archive` (`problem: too_large`) | 400 |
| an entry with a Windows separator | `invalid_model` | `invalid_archive` (`problem: unsafe_entry`) | 400 |
| an entry that is absolute or has a `.`/`..` segment | `invalid_model` | `invalid_archive` (`problem: unsafe_entry`) | 400 |
| an entry not inside a session directory | `invalid_model` | `invalid_archive` (`problem: entry_outside_session_directory`) | 400 |
| more than one session directory | `invalid_model` | `invalid_archive` (`problem: multiple_sessions`) | 400 |
| the slug is taken and `--force` was not passed | `invalid_model` | `session_exists` | **400 → 409** |

**`too_many_entries` is the eighth arm, added by #219 rather than by #101, and it is additive under
the same rule as every other `--json` field this page tracks.** The seven above only ever counted
*files* — `MAX_ARCHIVE_FILES` and `MAX_ARCHIVE_BYTES` are both computed over `z.infolist()` with
directory entries filtered out — so an archive built entirely of directory entries had zero files and
~zero declared bytes and sailed past both caps while the extraction loop still created every one of
them. `too_many_entries` bounds `len(z.infolist())` itself, files and directories together, before
either of the file-only caps runs. A real `session export` never writes a directory entry at all (it
walks real files only), so this bound is loose for the legitimate case and exists purely to close the
directory-only path.

**One code for the eight now, and what that code owes.** They share a remedy — *give me a different
archive* — and the rule stated in the section above refuses a candidate code that sends a reader where
an existing one already sends them. What a single code owes in exchange is exactly the thing #82 was
about: `details["problem"]` is present on **every** `invalid_archive` arm, with a closed vocabulary —
`empty`, `too_many_entries`, `too_many_files`, `too_large`, `unsafe_entry`,
`entry_outside_session_directory`, `multiple_sessions`. Each arm then adds only the numbers its own
sentence quotes (`{entries, max_entries}`, `{files, max_files}`, `{bytes, max_bytes}`, `{entry}`,
`{slugs}`). Eight conditions under one code with varying keys would have rebuilt the `KeyError` that
#82 removed.

**Only one status moves.** `session_exists` was already in the vocabulary, already 409, already
documented for exactly this fact — *"a session already occupies that slug"*. The eight archive arms
stay 400, because the caller did hand us the archive.

**One behavioural note, not a code change.** In `create_session` and `migrate_legacy`,
`SessionExistsError` is the *atomic claim* on a slug — the rename either wins it or raises. In
`session import` it is a **check**, with the TOCTOU window a check implies: a session created between
the check and the move is refused by the move instead. That window predates this change and is
unchanged by it; it is written down here because the same code now reaches a reader from two places
that mean subtly different things by it.

### `import_destination_occupied` — a new code, and one behaviour change (#114)

**New in the vocabulary: `import_destination_occupied`, 409, `details` `{slug, path}`.** Adding a code
is not breaking under the rule above, and this one moves no condition off an existing code that was
ever correct for it — it takes over a case `import_move_failed` was answering wrongly.

| Condition | Was | Now | Status |
|---|---|---|---|
| something that is not a session sits at the slug, POSIX, **non-empty** | `import_move_failed` | `import_destination_occupied` | **500 → 409** |
| something that is not a session sits at the slug, POSIX, **empty** | *imported silently* | `import_destination_occupied` | — → 409 |
| something that is not a session sits at the slug, Windows, empty or not | `import_move_failed` | `import_destination_occupied` | **500 → 409** |

The free-slug arm claims the slug by renaming the extracted directory onto it, and `os.replace` does
not answer that the same way on every platform: POSIX replaces an **empty** destination directory
silently, while Windows' `MoveFileExW` refuses *any* existing destination directory. So one stray
`mkdir` imported on macOS and Linux and failed on Windows — and the Windows refusal read *could not
move the imported session into place*, which is a fact about a move where the fact is about the
destination.

**The second row is a behaviour change and not only a rename**: an import that used to succeed on
POSIX now refuses. It is taken deliberately, because the alternative is converging the other way — an
import that deletes a directory the store never created and cannot interpret — and because no
in-code producer leaves an empty directory at that path (`create_session` stages and renames,
`session_lock` refuses rather than materialising one). Reaching this state takes a `mkdir` or a
half-cleaned checkout, and the remedy is to move or remove that directory. `--force` does not lift
it: `--force` replaces a *session*, and this code exists to say there is no session there.

### The write lock moved out of the session directory (#113)

The per-session write lock was `.requivo/sessions/<slug>/.lock`. It is now
`.requivo/locks/<slug>.lock`.

**Not a session-format change.** `.lock` was never part of a session: `session export` excludes it as
this machine's coordination, an import never carried one, and nothing reads it as data. No
`format_version` bump, no `migrate_session` entry, and a session directory written by any version
opens unchanged in any other. A `.lock` left inside an existing session by an earlier Requivo is
inert — nothing opens it, `session export` skips it, `session verify` ignores it — and is safe to
delete.

**One real limitation, and it belongs on this page rather than only in a changelog: two Requivo
versions writing the same workspace at the same instant no longer serialise against each other.** An
older one takes `<slug>/.lock` and this one takes `.requivo/locks/<slug>.lock`, and two different
files do not contend. Every other cross-version promise on this page is about a *file* one version
writes and another reads, and holds; this one is about a *lock*, which is not a file anybody reads,
so nothing can carry it across the change. Mitigation is ordinary: finish or close the older process
before running this one against the same workspace. Within one version, mutual exclusion is
unchanged and is now strictly stronger — `session import --force` holds the lock it used to rename
away.

**Why it moved.** An OS lock is a claim on an *inode*; every writer under it resolves the session
directory and writes by *pathname*. `session import --force` renames that directory, so the two
stopped describing the same thing: a writer mid-write went on writing into the freshly imported
session, and a third process opening the lock found a different file and acquired it. Keeping the
lock inside and simply holding it across the swap was not available either — a directory containing
an open handle is precisely what Windows refuses to rename.

**Deleting a session by hand leaves its lock file behind; `session delete` (#238) does not.** The
verb takes the same lock every other compound mutation does, removes the directory under it, and
unlinks `.requivo/locks/<slug>.lock` last — after the lock is released, not while still held (see
the ordering in `Store.delete_session`'s own docstring). A session removed *by hand* (`rm -rf`,
bypassing the verb), or by an older Requivo with no delete verb at all, still leaves the lock file
behind: the residue is one empty file under `.requivo/locks/`, which claims no slug and is read by
nothing; delete it or leave it. `requivo doctor` reports the lock root under `workspace.locks` so
there is a directory to point at, and — since #180 — its `locks` check walks it: which slugs a
`<slug>.lock` file names no longer have a session, reported as candidate residue and never as a
verdict, since the lock scan and the current session list it is checked against are two reads a
moment apart — see the note in `session-format.md`.

## What a proposal means

The JSON you hand to `model validate`, `model diff` and `model apply` is a **proposal**, and since
0.9.6 its shape is stated rather than implied:

- `model` is the complete slot set. An apply *replaces* the model — it does not merge — so a partial
  one is refused. Check a projection with `model validate --allow-partial`; that flag is gone from
  `apply` and `diff`, where it read as "apply a patch" and silently replaced a fifteen-slot model
  with whatever subset was sent.
- `summary.objective` must say something. A model of filled slots with nothing naming what it is for
  renders as a blank heading everywhere, so it is refused as incomplete (`invalid_model`).
- `decisions`, `challenges` and `opportunities` are **tri-state**: leaving the key out means "not
  speaking to it" and keeps what is established; `[]` deletes; a list replaces. Before 0.9.6 an
  omission was read as an empty list, so an ordinary refinement turn deleted the whole reasoning
  layer — and reported no change while doing it.

## The other public surfaces (#89)

The two sections above bound the session format and the `--json` outputs. Everything below was relied
on by design and was in neither column — neither promised nor disclaimed. A promise that absorbs
everything is one nobody can keep, and a surface in neither column is a promise nobody made and
everybody may assume, so each gets a verdict here.

The count is worth keeping honest: this began as four surfaces, gained a fifth when a release audit
found the web error banner's codes, and gained a sixth when #86 cited an exit-code policy that did not
exist. **Six, and there are six subsections below** — if those two numbers ever disagree, the count is
the one that is wrong. Two of the six were found *after* the section claiming to be exhaustive was
written, which is the argument for the closing sentence of
[what is explicitly not stable](#what-is-explicitly-not-stable): a surface in neither column is a bug
in this page, not a licence to assume.

**What belongs here, and what does not.** A subsection here answers *is this stable* about a surface,
and its heading carries the verdict. A **change note** — what moved, from what, to what — belongs with
the change it describes: `### invalid_session is a family (#82)` and
`### The import path names the archive (#101)` sit under the `--json` section for that reason. The
distinction is worth stating because it has already been got wrong once: #101's note was filed here,
which made the count above read as stale when the note was simply in the wrong section, and made this
section appear to contain a surface with no verdict in a section that says each gets one.

### The epic export envelope — **stable**, and versioned

`requivo epic <slug> --export-json` writes an envelope carrying its own `format` (`requivo-epic`) and
`version` (**2**, see below). It exists to be validated by something outside this repo — an importer, an n8n
flow — so declaring it unstable would contradict the code that declares it stable. Changing the shape
of `epic` inside it is breaking; the escape hatch is `version`, and bumping it is itself breaking and
announced here.

The same rule as the `--json` payloads applies inside it, one level deeper because this envelope has
one: the key skeleton of the envelope, of the `epic` object, and of each entry in `issues` is the
contract. `test_the_epic_export_skeleton_is_pinned_to_its_version` records that skeleton **per
version number**, so changing a key is red until `EPIC_EXPORT_VERSION` moves and the new number gets
a skeleton of its own beside the old one. Until #267 the only assertion on this envelope compared
`version` with the constant it was read from, which is true whatever the keys are — a version number
nothing forced to move, on the one payload whose stated consumer lives outside this repository and
cannot be grepped for breakage.

The **tracker plans** from `--github` and `--gitlab` are stable in the same way, with one asymmetry
worth stating: they describe somebody else's API. A change we make to their shape is breaking. A
change forced on us because GitHub or GitLab moved is not a promise we were ever able to make, and it
will be documented as what it is rather than dressed as a choice.

### The epic export gains a provenance stamp — `EPIC_EXPORT_VERSION` 1 → 2 (#274)

**New top-level keys on the envelope: `slug` (string) and `source_revision` (int).** Both tracker
plans (`--github`, `--gitlab`) carry `source_revision` through into their own top-level output too.
This is additive to the envelope's *keys* and breaking under the versioning rule stated just above,
which is why the version moved rather than the keys being added silently — an importer pinned to
`version == 1` still receives exactly the v1 skeleton from an older Requivo, and a v2 skeleton is
recorded beside it in `test_the_epic_export_skeleton_is_pinned_to_its_version`.

**Why:** `epic.json` (and its `--github`/`--gitlab` siblings) are written outside `ArtifactService`,
deliberately — see the "stable, and versioned" section above — so unlike `epic.md` they carry no
staleness row in `session.json`, and the automation that consumes them (an n8n flow) is the one
reader that cannot exercise judgment about whether the file it just read is still current. Before
this change there was no signal at all.

**One consumption rule, stated once here because it matters more than the two new keys:**
`source_revision` identifies the *basis* the export was rendered from — it never judges freshness by
itself. Comparing it against another revision number, or against a wall-clock age, is exactly
invariant 1's anti-pattern ("staleness is the dependency graph, never the revision number"). The
freshness verdict for a session is `requivo status --json`'s `artifacts.epic.stale`; an automation
that wants to know whether an export is still current fetches that and compares, it does not infer an
answer from `source_revision` alone. `docs/cli.md` states the same rule where the export flags are
documented.

`_cmd_epic` populates `source_revision` from the same `Generated.status.revision` snapshot the paired
`epic.md` save used for that invocation — one snapshot, per invariant 12 — never a second read of the
session's current revision, which could disagree with `epic.md`'s if something else wrote to the
session in between.

### Environment variables — **stable**, with one exception

`REQUIVO_WORKSPACE`, `REQUIVO_CONTEXT_DIR` and `REQUIVO_WEB_ALLOWED_HOSTS` are documented user-facing
knobs; a deployment sets them and removing or repurposing one breaks it. `REQUIVO_WEB_ALLOWED_HOSTS`
keeps its `WEB` for that reason even though #508 widened it to govern the HTTP API's host allowlist
as well: the name is the compatibility surface, and renaming it for accuracy would break a
deployment for one word. They are covered by the same
rule as a CLI flag: removing one, or changing what one means, is breaking.

`REQUIVO_OUTPUT_DIR` is **deprecated** (see the table below). It configures the retired `out/` layout,
which nothing has written since 0.8.0 and only `requivo session migrate` still reads. A live knob for a
dead path is worth retiring while retiring is still free.

**`MODEL` is deprecated too, in favour of `REQUIVO_MODEL`** (#268). It was the one model override in
the package not `REQUIVO_`-prefixed, which matters because `MODEL` is a generic name other tools set
— a CI job, a docker-compose file, an unrelated ML script sharing the same shell — so it can collide
silently and steer Requivo at a differently-priced or nonexistent model with no hint the value came
from outside. `current_model_name()` reads `REQUIVO_MODEL` first and falls back to bare `MODEL` only
when `REQUIVO_MODEL` is unset, so nothing already working today stops working. This page's own
promise applies: the fallback keeps working for at least one minor version and is named in the
deprecations table below; it prints no runtime notice on the fallback path (a working call has
nothing wrong to report — see the comment in `client.py`), so `requivo doctor` is where a future
notice would belong if the silent collision this issue exists to prevent turns out to still happen in
practice.

### Requivo Web's HTTP routes — **paths stable, bodies not**

This page already contemplates a client scripting `POST /sessions` and branching on its status, so the
paths are relied on by its own worked example. The whole set is small enough to name rather than
gesture at:

| Route | Method |
|---|---|
| `/` , `/sessions/new` | GET |
| `/sessions` | POST |
| `/sessions/example` | POST |
| `/sessions/{slug}` | GET |
| `/sessions/{slug}/export` | GET |
| `/sessions/{slug}/discover` , `/sessions/{slug}/answers` | POST |
| `/sessions/{slug}/artifacts/{type}` | GET, POST |
| `/health` | GET |

**The path, the method and the HTTP status are stable.** Removing a route, moving one, or changing the
status a condition answers with is breaking, and the status half is already governed by the table
above.

**The response bodies are not**, with two exceptions. Every route above renders HTML for the browser —
several return HTMX fragments whose shape follows whatever the page needs that week, and parsing one is
the web equivalent of parsing terminal output. `GET /health` and `GET /sessions/{slug}/export` are the
two that return data rather than a view, and they are stable.

### Artifact filenames — **stable**, and part of the session format

`<session>/artifacts/` holds files whose names come from a fixed map:

| Type | File |
|---|---|
| `brief` | `solution-assessment.md` |
| `prd` | `prd.md` |
| `stories` | `stories.md` |
| `criteria` | `acceptance-criteria.md` |
| `epic` | `epic.md` |
| `release` | `release-notes.md` |

They are inside the published session directory and are recorded in `session.json`, so **renaming one
needs a `format_version` bump and a migration**, exactly as renaming a populated key does. That was
unanswerable from this page before, and [session-format.md](session-format.md) described the directory
as *"generated views (PRD, assessment, …)"* without naming the files.

Note that the type and the filename deliberately differ for `brief`, which is stored as
`solution-assessment.md`. The type is the stable identifier; the filename is stable too, and they are
two facts rather than one spelling.

### CLI exit codes — **stable**, under the same rule as an error code

This section was written to bound four surfaces and missed a fifth, which #86 found by citing a
policy for exit codes that this page did not contain. It does now.

| Exit | Means |
|---|---|
| 0 | success |
| 1 | a clean, expected failure — an invalid proposal, a missing session, a provider error, an oversized request |
| 2 | bad arguments (argparse) |
| 3 | the command's work finished and its output could not be encoded |
| 4 | the work was done and part of the answer was unreachable |
| 130 | the operator interrupted the run (Ctrl-C / SIGINT) |

[cli.md](cli.md) carries what each means in full, and is the page to read; this one carries the
promise. **The promise is the same one made for `RequivoError.code`, one paragraph up: adding a code
is not breaking, and moving a condition from one code to another is.** So is changing what an
existing code means. A script gating on an exit code is doing the documented thing, and it is the
consumer with the least ability to notice a silent change — there is no payload to inspect, only a
number that is still a number.

**"Moving a condition" has a direction, and the direction decides it (#382).** Read literally,
the sentence above makes every move breaking regardless of which way it goes — and one release
shipped one of each and declared them differently: #360 moved three invocations off exit 0 onto 1
or 2 and was declared breaking; #249 moved one from exit 2 onto 0 and was declared compatible.
**Moving a condition onto 0 is not breaking. Moving one onto a nonzero code, or from one nonzero
code to another, is.** An invocation a script depended on succeeding cannot start failing without
that being a breaking change; an invocation nothing could have depended on succeeding — because it
never did — cannot break anything by starting to. Direction is a mechanical fact about the two
codes on either end of the move, checked by comparing them, never a question about whether this
particular narrowing happens to be safe — that door was closed by #255 below, on exactly that kind
of argument, and this clause does not reopen it: it adds a second mechanical fact to check, it does
not excuse the first one from being checked.

**4 is deliberately general.** It was `EXIT_DEGRADED_LISTING` and named one verb; #86 generalised it,
because an exit code describes a *shape of answer* — the work was done, part of it was unreachable —
rather than the verb that produced it. Minting a code per verb would rebuild the problem 4 was
introduced to solve. A new condition of that shape gets 4, not 5.

**Where a firm negative and a partial one meet, the firm one wins.** `session verify` on a session
that is both inconsistent *and* has cards it could not read exits **1**, not 4: a script asking *is
this usable* wants the definite answer. That precedence is part of the contract, not an
implementation detail.

There are now **two** ways to reach the partial answer, and they are the same answer: the cards
could not be read, or the session directory itself could not be stat-ed (#97). Both render as `4`
and both set `ok` to false; `--json` says which, in `context_cards.checked` and `session.checked`
respectively.

### Ctrl-C gets its own exit code (#206)

A `KeyboardInterrupt` reaching the CLI used to be an unhandled Python exception once it escaped
whatever local `except` happened to be in scope on that call — a traceback, and whichever exit code
the interpreter gives one, which was never a documented promise. It is now caught at the top of
`app()` and exits **130**, the conventional SIGINT code (128 + signal 2), distinct from 1 so a script
can tell "the provider refused this" from "the operator stopped it".

**One real behaviour change inside that:** `requivo discover`'s interactive loop and its
decision-brief step already named the claimed session and the continuation verb on a `KeyboardInterrupt`
(#202, #320) — and did it by exiting **1**, the code documented above for "a clean, expected failure".
They exit 130 now, like every other command. A script that gated on exit 1 to detect an interrupted
`discover` specifically needs to gate on 130 instead.

### `requivo impact` and an unmatched slot — a behaviour change (#250)

`requivo impact <slug> <unknown-slot>` used to print `Unknown slot(s): …` and exit **0** —
indistinguishable, to a script, from an empty but valid result. It now exits **1**: the *input* was
invalid, not the answer partial, so this is "a clean, expected failure" rather than `EXIT_DEGRADED`'s
"the answer was unreachable". Whatever matched, if anything, still renders in full above the exit.

### `session init` and an oversized request — a behaviour change (#255)

`SessionService.create_session` now calls `require_input_within_bounds` before a request reaches a
provider or lands on disk — the service layer is the integrity boundary (invariant 14), not a
route's own friendly re-render, which is careful but not a guarantee. A request over **20,000
characters** — `MAX_INPUT_CHARS` in `core/contracts.py` — is refused with `{"code":
"input_too_large"}` at exit **1**, instead of creating the session.

`git show v1.3.0:src/requivo/services/sessions.py` has no length check at all, so this moves
`session init` — one of the sixteen public `--json` payloads declared above — from exit 0 to exit 1
on an input it used to accept. That is the same shape #250 documents just above: a condition moved
from one code to another, which this page's own promise calls **breaking**. Unlike the concern
`changelog.d/255.fixed.md` first weighed and set aside — a caller relying on a billed provider call
having already gone out — `session init` is offline and spends nothing, so nothing about a paid call
softens this one.

### `requivo discover` and the three shapes of its argument — a behaviour change (#360)

`session init -`, `model apply <slug> -` and `artifact save --file -` have always read stdin from a
bare `-`. `discover` did not: it asked `is_file_argument`, which answers False for `-` — correctly,
`-` is not a file — so the argument fell through to the literal-text branch and the engine was asked
to discover a product from the two characters `-`. It now reads stdin, like its siblings, and
refuses an empty source before any provider call.

Three conditions move off exit **0**, each of which used to reach the provider and be billed:

| Invocation | Was | Now |
|---|---|---|
| `requivo discover -` with a terminal on stdin | 0 — discovered on the literal `-` | **1** |
| `requivo discover -` with an empty pipe | 0 — discovered on the literal `-` | **2** |
| `requivo discover <an empty file>` | 0 — a billed call on an empty request | **2** |

The third is the one no reading of #360's title predicts. The old branch read a file's contents and
never re-checked them, so only a blank *argument* was refused; a file that happened to be empty was
read, found empty, and sent anyway.

That is the same shape as #250 and #255 above — a condition moved from one code to another, which
this page's own promise calls **breaking** — and it is declared that way in
`changelog.d/360.fixed.md` rather than argued down on the grounds that every one of those old
behaviours was a defect and none was ever documented here. #255 was first declared compatible on
exactly that kind of harm argument and had to be corrected before its tag; the rule in this section
is mechanical for that reason.

**#249, in the same release, is the case the direction clause above was written for.** It moves
`requivo status <slug> --workspace DIR` from exit 2 to exit 0 — also a condition moving between
codes, but onto 0, so it is compatible under the rule stated above, not merely tolerated by it
despite what the rule says. #360 above narrows; #249 widens; #382 is where that asymmetry was made
mechanical rather than left to be rediscovered the next time a release carries one of each.

### The eight write verbs no longer accept a `model.json` path — a behaviour change (#402)

These eight verbs shared their positional's help with `status`/`impact`: "a session slug, or a path
to a saved model.json". Only `status`/`impact` ever meant it — they read the file's own bytes
directly. The other eight resolve a *slug* and then read/write the store's own copy of the session,
so a `model.json` path was mined for its parent directory's name (`SessionService.resolve_slug`)
rather than opened, and the mining ran whether or not the file existed — a fabricated path was
reported on under a slug the user never typed, or, worse, silently matched an unrelated real session
sharing that mined name.

The fix is the same shape as #360 above: a condition moves off exit 0.

| Invocation | Was | Now |
|---|---|---|
| `requivo brief <real-session>/model.json` (the session's own model.json, an existing slug's path) | 0 — resolved via the mined slug | **1** — refused, naming the path |
| `requivo brief <anything>/model.json` where no session shares the mined slug | 1 — `no session named <mined-slug>` | **1** — refused, naming the given path instead of the mined slug |

The first row is the one this page's own rule calls breaking: an invocation that used to succeed
(reading a real session by an alternate spelling of its slug) now fails. Declared that way in
`changelog.d/402.fixed.md` rather than argued down on the grounds that the old behaviour was itself
the defect — the same reasoning #360 above already rejects for the identical shape. `status` and
`impact` are unaffected; they never went through `resolve_slug` for a path and keep the wider help.

### `resolve_slug`'s directory branch closes the identical gap, one branch over (#414)

Every `deterministic/` verb (`accept_path=True`, the default) can still be handed a directory rather
than a slug. `resolve_slug` used to mine *any* such directory for its own name, whether or not a
session actually lived behind it — the same wrong-cause shape #402 closed for the `model.json`/
`session.json` branch above, and reachable at all only because that fix left path acceptance on for
this caller class.

| Invocation | Was | Now |
|---|---|---|
| `requivo session show <dir>` where `<dir>` genuinely is a session's own directory | 0 — resolved via its own name | 0 — unchanged, `<dir>` carries its own `session.json`/`model.json` |
| `requivo session show <dir>` where `<dir>` is an unrelated directory whose final segment happens to name a real session | 0 — silently resolved to and reported on the unrelated real session | **1** — refused, naming the given path |
| `requivo session show <dir>` where `<dir>` is an unrelated directory naming no session at all | 1 — `no session named <dir's name>` | **1** — refused, naming the given path instead of a slug carved from it |

The second row is what this page's own rule calls breaking, for the same reason the analogous row in
#402's table is: an invocation that used to succeed — silently, and against the wrong session — now
fails. Declared that way in `changelog.d/414.fixed.md` rather than argued down on the grounds that
silently operating on the wrong session was itself the defect, which #360 and #402 above both already
reject for the identical shape.

### The web error banner's `code` — **not stable**

Requivo Web renders a `(code: …)` line on a refusal. Four of those values —
`empty_request`, `invalid_request`, `not_found`, `internal_error` — are bare string literals rather
than `RequivoError` subclasses, so they are outside the vocabulary the `--json` outputs publish and
invisible to the test that walks it. **They are presentational.** A caller scripting the Web branches
on the HTTP status, which is stable; the code on the banner is for a human reading the page and may
change without notice.

### CI gained a `3.14` leg — the supported floor did not move (#298)

The compatibility-axis matrix (`.github/workflows/ci.yml`, the `test` job) gained a sixth entry,
`3.14`, beside the existing `3.9`–`3.13` five — a new job name (`Test (py3.14)`), additive, the same
shape as any other leg added to that matrix. **This is not a floor change.** `requires-python` stays
`>=3.9`, and so do `[tool.ruff] target-version`, `[tool.pyright] pythonVersion`, the `tomli` marker,
and the macOS/Windows platform matrix's `3.9`/`3.13` ends. Whether to raise the floor off `3.9` (EOL
since October 2025) is a separate decision this change deliberately does not make: it needs pypistats
download-share data on who is actually still pinned to a `3.9` system interpreter, which the audit
that filed #298 named as the step-2 gate.

**The new leg is not yet a required check.** Adding a leg to a workflow file does not add it to
`main`'s branch-protection required-status-checks list — that is a separate, manual API call (see the
`test` job's own header comment in `ci.yml`, and `decision: appending-a-required-check`), and it is
out of scope for this change: it is an administrative action on the repository, not a code change,
and the wrong verb (`PATCH` instead of a `POST` to the `contexts` sub-resource) silently replaces the
whole list rather than extending it.

## The Python import surface — the declared seam (#423)

Filed from the 2026-09 readiness audit (public-interfaces pass): "Python internals … not a published
API," below, disclaimed the whole import surface while the one consumer that matters most —
`requivo-cloud`, the first-party hosted deployment — already imported nine names across four modules,
including six `providers.anthropic` generator functions decision record 0003 explicitly measured as
unstable, and pinned `requivo>=1.2.0,<2.0.0` against a package that shipped three majors in the
thirteen days after 1.0.0. The 3.0.0 removal of a services method was graded breaking in the changelog
**despite** the disclaimer above — the surface was already being treated as semi-public in practice.
This section is where that practice becomes a promise, for the names it names and no others.

**The testable contract sentence:** moving, renaming, or changing the signature of any name below
costs a major version and a line on this page, priced exactly like the CLI, the `--json` envelopes and
[the other public surfaces](#the-other-public-surfaces-89) above. `tests/test_public_seam_423.py`
pins that the names below still resolve; it does not and cannot pin that a future change to one is
priced correctly — that is this page's job, same as everywhere else on it.

**The declared seam:**

- **The services** — `requivo.services.sessions.SessionService`, `requivo.services.discovery.
  DiscoveryService`, `requivo.services.artifacts.ArtifactService` — the only apply, generate and
  staleness implementations (CLAUDE.md's own rule), and their result types: `UpdateResult`,
  `SessionEntry`, `SessionSnapshot`, `Readiness`, `RescopeResult` (`services/sessions.py`) and
  `Generated` (`services/discovery.py`, the typed wrapper `generate()`'s overloads resolve).
- **The two protocols** — `requivo.services.repository.SessionRepository` and
  `requivo.providers.base.ReasoningProvider`. A hosted Postgres backing or a non-Anthropic reasoning
  backend is built against these, never against `FileSessionRepository` or `AnthropicProvider`
  internals.
- **`requivo.services.repository.FileSessionRepository`**, as the shipped `SessionRepository`
  implementation — its `__init__(root=...)` constructor (#272) and construction-only addressing are
  stable — and `default_repository()`. Its filesystem-only extensions are **not** part of this seam:
  `.store()`, and everything reached through `requivo.core.persistence.Store`, exist because this one
  backing has a filesystem underneath the protocol and a Postgres backing has nothing analogous to
  expose. (The #272 changelog entry flagged both `Store` and `FileSessionRepository.store()` as
  "provisional until #423 declares the storage seam frozen" — this is that declaration, and it
  freezes `FileSessionRepository` itself while leaving both of those out.)
- **The boundary contracts** a consumer holds in its hands: `requivo.core.contracts.EngineOutput` and
  `.ModelProposal` (what a proposal/apply exchanges — see ["What a proposal
  means"](#what-a-proposal-means) above), `requivo.core.persistence.SessionMeta`, `.ArtifactStatus`,
  `.RevisionRecord` and `.UnexaminableEntry` (what the protocol's own methods return), and the artifact
  contracts a generation can produce: `requivo.core.contracts.Brief`, `.PRD`, `.AcceptanceCriteria`,
  `.Epic`, `.ReleaseNotes`, `.Stories`, `.EstimateDraft`.
- **The failure vocabulary.** `requivo.core.errors.RequivoError` and every subclass it defines, plus
  `requivo.providers.errors.EngineError` and the three artifact-specific subclasses in
  `requivo.services.artifacts` (`UnknownArtifactTypeError`, `UnstatedSourceRevisionError`,
  `UnreadableSourceRevisionError`). The machine `code` on each was already promised
  ([above](#the-json-outputs-are-public)); this adds the classes themselves as importable, catchable
  names.
- **`requivo.usage`** — `UsageLedger`, `CallRecord`, `track_usage`, `record_call`, `current_ledger`.
  Provider-neutral by construction (#167) — a hosted caller reads a call's cost without importing
  anything Anthropic-specific. `CallRecord` gained an optional `operation: str | None = None` field
  (#435, additive — a consumer constructing one the old way is unaffected, and the default reads
  identically to a `CallRecord` built before the field existed).
- **`requivo.testing`** (#424) — `SessionRepositoryConformance` and `full_model`, both re-exported
  from the package's own `__init__.py` rather than only reachable via the submodule path.
  `SessionRepositoryConformance` is the factory-parametrised pytest suite behind CLAUDE.md's claim
  that "a Postgres repository reuses [the orchestration] verbatim". Requires
  `pip install 'requivo[testing]'` (pytest is not a base-install dependency); `FileSessionRepository`
  and this repo's own in-memory test fake both run against it in-tree (`tests/test_sessions.py`), and
  an out-of-repo implementation subclasses it directly — proven by building the wheel and running the
  suite from a directory outside this repository entirely, with no access to `tests/conftest.py` or
  any other repo-internal fixture. `full_model()` is the schema-complete model builder the suite's own
  test methods build against, exported alongside it because a subclass that extends or overrides a
  test method needs the identical fixture rather than a private duplicate of it. Both names' own
  correctness rules apply exactly as to every other seam name above: neither reaches into
  `core.persistence` internals, and moving or renaming either costs a major the same as any name here.

**`py.typed`.** The wheel now ships a PEP 561 marker (one empty file:
`src/requivo/py.typed`, declared in `[tool.setuptools.package-data]`) — the whole package was already
pyright-clean with zero diagnostics (`[tool.pyright]`, above), so the marker is what lets a downstream
`pyright`/`mypy` run resolve those types instead of treating every import as `Any`. Spot-checked for
this change: a throwaway project depending on this checkout's wheel, importing
`SessionService`/`DiscoveryService`/`SessionRepository`/`EngineOutput` and calling one method on each
with a deliberately wrong argument type, reports the error under `pyright` where it silently passed
before the marker existed.

**Everything else stays internal**, unchanged from the rule below — most pointedly
`requivo.providers.anthropic` (`client.py`/`completion.py`/`generators.py`/`pricing.py`/`provider.py`,
including `AnthropicProvider` itself), which decision record 0003 already flagged as free to move and
which this page does not now freeze. A consumer wanting Anthropic-backed reasoning implements
`ReasoningProvider` itself, or accepts that importing `AnthropicProvider` is exactly the unstable bet
this section exists to name rather than hide.

**Recommended consumption pattern**, in one paragraph: pin exactly (`requivo==X.Y.Z`), never a range —
three majors in thirteen days means a range ceiling reads as prudence and works as starvation, quietly
starving a consumer behind an early cap (see `docs/cloud-boundary.md` §2 for the measurement). Bump the
pin as a routine chore gated by two things: your own tests, and the repository conformance suite
(`requivo.testing.repository_conformance`, #424) if your backing implements `SessionRepository` — a
Postgres or other non-file implementation that passes it inherits the services' orchestration
verbatim; one that does not has found its bug before production did.

**Classifying the five surfaces this page's own rule (#89, closing sentence) called a bug for leaving
silent:** `requivo.render`, `requivo.paths`, `requivo.streams`, `requivo.cli` and `requivo.web` are
**not stable as Python import surfaces** — none of their names are part of the seam above. Each one's
*behavioural* promise is already made elsewhere on this page under a different heading: the CLI's exit
codes and `--json` payloads ([above](#the-other-public-surfaces-89)), Requivo Web's HTTP routes and
statuses (same section), the environment variables `paths.py` reads (same section), and terminal
output's "parse `--json`, never the rendered view" rule (`render`/`streams`, below). What is not
promised is importing a function or class *from* any of these five modules and depending on its
Python-level shape — that can move in a minor, the same as every `core.persistence`/`core.analysis`/
`core.context`/`core.dependencies`/`core.validation`/`core.integrity`/`core.adapters`/`core.selectors`
name not listed in the declared seam above.

## What the sdist and wheel contain (#431)

The wheel is the installable artifact and is what every promise on this page is verified against —
the wheel-install CI job and the publish gates build and smoke-test it on every release. The sdist
(`requivo-X.Y.Z.tar.gz`) is a second, separate artifact: source form, for a distro packager (Debian,
conda-forge) or anyone building from source rather than installing a wheel.

**The sdist ships no `tests/` directory, deliberately** (`MANIFEST.in`: `prune tests`). Before this
decision the default sdist file-finder pulled `tests/*.py` in anyway — 66 files, verified against the
actual PyPI 3.0.0 artifact — without the underscore helpers those files import
(`_fakes.py`, `_cli_harness.py`, `_scan.py`, `_credentials.py`, `conftest.py`), without `tests/web/`,
without the root `conftest.py`, without `scripts/` or `fixtures/golden` several guard tests read. The
result was collectable by nothing: `pytest --co` against the extraction died at
`ModuleNotFoundError: No module named '_fakes'`. Shipping the rest too (helpers, harnesses, fixtures,
scripts) was considered and rejected — it roughly triples the sdist to serve a consumer this suite's
self-scanning guards were never written for, since their subject is this repository's own tree, not
an installed package. **A distro packager verifying the built artifact should run the wheel** (the CI
leg and the publish gates already do this on every release), not attempt to re-run this suite from an
extracted sdist. `tests/test_sdist_contents_431.py` builds a real sdist in-process and pins that
`tests/` is absent from it; that test — not this paragraph — is what a future change to `MANIFEST.in`
has to keep green.

## Deprecations

| What | Status | Since | Removal | Instead |
|---|---|---|---|---|
| **`model apply --allow-partial`, `model diff --allow-partial`** | Removed — it merged nothing, it replaced | 0.9.6 | gone | `model validate --allow-partial` to check a projection; send the full slot set to apply |
| **Legacy flag CLI** (`python src/engine.py "…" --prd`) | **Removed** | deprecated 0.9.2 | 0.9.8 | `requivo discover` + `requivo prd` — see [cli.md](cli.md) |
| **`pc` command alias** | **Removed** | deprecated 0.7.0 (rename) | 0.9.8 | `requivo` |
| **Implicit `out/<slug>/` fallback** | **Removed** — migration is explicit | deprecated 0.8.0 | 0.9.8 | `requivo session migrate`, then `.requivo/sessions/` |
| **`/requivo-<skill>` plugin skill names** | Renamed | 0.9.2 | gone | `/requivo:<skill>` — Claude Code namespaces plugin skills |
| **`REQUIVO_OUTPUT_DIR`** | Deprecated | #89 | with `requivo session migrate` | nothing — it configures the retired `out/` layout that only the migrator reads. `REQUIVO_WORKSPACE` is the knob for where sessions live |
| **Bare `MODEL` env var** | Deprecated | #268 | not yet set | `REQUIVO_MODEL` — every other env var this package reads is `REQUIVO_`-prefixed; bare `MODEL` is read only when `REQUIVO_MODEL` is unset |
| **`epic --json`** | **Renamed** — it wrote a file, it never emitted JSON | #83 | gone in the same change | `epic --export-json`, beside `--github` and `--gitlab`. `epic` deliberately has no stdout `--json`; the flag also silently switched the error channel, which is the half the rename fixes |

The policy: anything deprecated keeps working for at least one minor version, says so when used where
that is possible, and names its replacement here. **From 1.0.0 it is removed only in a major.** Below
1.0 the floor was "not in a patch", which is what the entries above were deprecated under; the
opening of this page raised it, and this sentence used to state the old one.

**`epic --json` is the exception, and it is stated rather than left to be noticed.** It was removed in
the same change that renamed it, with no grace version. The policy above is what a deprecation buys
you, and a grace version buys nothing here: the flag did not do what its name said on any release it
shipped in — it wrote a file where every other verb's `--json` emits a payload — so a version of
*keeping it working* would have been a version of keeping it wrong, in the one window where removing
it costs the tag itself rather than a major after it. Below 1.0 this page permitted an interface
change in a minor, and 1.0.0 is the last release under that reading; from the release after, the
flag would have been frozen into the tag.

**The 0.9.8 removals.** These three were the last of the pre-store architecture, all deprecated for
several versions and none of them with a known user. They were carried on a "removal in 1.1.0" plan,
which would have meant maintaining them *through* 1.0 — the moment you least want
two answers to "where does a session live?". The public interface is unchanged: `requivo` and the
session store were already the only supported path. If you have `out/` sessions, `requivo session
migrate` still converts them, and it is now the only thing that reads that layout.

## What is explicitly *not* stable

- **Python internals, except the declared seam.** `requivo.core`, `requivo.services`,
  `requivo.providers` and `requivo.deterministic` are importable and documented, but they are the
  engine's own structure, not a published API, **with the exception of [the names §"The Python
  import surface" above declares](#the-python-import-surface--the-declared-seam-423)** — a carve-out
  added by #423, not a change to the rule: everything in these four module trees that is not on that
  list is exactly as unstable as it always was. `requivo.usage` moved the other way in the same
  change — it used to be named here too, and is now wholly part of the declared seam instead, because
  nothing in it is Anthropic-specific or file-backing-specific (see below). A refactor can still move
  an undeclared name, and it has twice: #73 moved `requivo.deterministic`, and #74/#167 turned
  `requivo.providers.anthropic` into a package while moving two names out of it — the usage ledger to
  `requivo.usage` (before #423, itself internal; now the declared seam) and `EngineError` to
  `requivo.providers.errors` (declared, above), neither of which was ever Anthropic-specific. That is
  why they are named here rather than left to silence. The error *code* those moves carry,
  `provider_unavailable`, is unaffected: it is published in the `--json` envelope and is promised
  above, independently of which module defines the class.

  `requivo.deterministic`'s `__all__` is internal plumbing for the offline verbs rather than an
  interface: every name in it is read from inside this repository and none is promised outside it.
  `register(sub)` is the argparse wiring `cli.py` binds the offline verbs through. `is_file_argument`,
  `print_json` and `read_source` are the shared helpers `cli.py` imports, each promoted from a private
  name when `cli.py`'s own duplicate of it was removed -- the file-vs-text check and the raw
  `json.dumps` call by #301, the `-`-means-stdin reader by #360. `read_user_text` is read only by
  `tests/test_encoding.py`, and `EXIT_DEGRADED` by the suite, to assert the code a degraded run exits
  with. The set is deliberately not counted here: it grows whenever a duplicate is folded into the
  shared module, and a number in this sentence would go stale on the next such change with nothing
  going red (CLAUDE.md's own rule about a count in prose).
  What those verbs promise is on this page already: the CLI exit codes, the `--json` payloads and the
  session format. Those are promises about what the command does when you run it, not about names you
  can import. A downstream consumer that depends on any of these names deliberately tracks the repo.
- **The slug derived from a request.** `derive_slug` turns request text into a session directory
  name, and what it derives is tuning rather than a promise. #245 changed it: accents are
  folded before tokenizing and a fixed list of function words is dropped, so *"We need a way to track
  vendor invoices"* lands on `track-vendor-invoices` where it used to land on `we-need-a-way-to`, and
  *"Nous aimerions un système…"* keeps `systeme` whole instead of splitting it into `syst` and `me`.
  The emitted alphabet is unchanged, so every slug that was valid still is.

  **Sessions already on disk keep their names.** Nothing re-derives a slug for a session that exists;
  the derivation runs only when one is created, and an explicit `--slug` is never derived at all.

  What does change is **idempotent re-discovery**, and it is worth knowing before it surprises you.
  `create_session` is idempotent on the request *keyed by the slug it derives*, so re-running
  `requivo discover` on a request first analysed under an older Requivo now derives a different base
  and creates a **second** session at revision 0 rather than resolving to the first. Under the older
  version that same command resolved to the existing session and was then refused by the
  revision-zero gate, before paying for anything; now it proceeds, and it is a paid discovery. Use
  `requivo session list` to find the original and `requivo answer <slug>` to continue it — which is
  what you wanted in both versions, and what the refusal used to say.
- **Prompt and context-card content.** These are tuned continuously — that is the point of the
  [golden harness](evaluations.md). Two versions can reason differently about the same request; the
  provenance recorded on each revision (model + prompt hash) is what makes that traceable.
- **Terminal output layout.** Parse `--json`, never the rendered view.
- **Requivo Web's response bodies.** The route paths, methods and statuses are stable and are listed
  under [the other public surfaces](#the-other-public-surfaces-89); what comes back is HTML rendered
  for a browser, HTMX fragments included, and parsing it is the web equivalent of parsing terminal
  output. `GET /health` and `GET /sessions/{slug}/export` are the two exceptions and return data.
- **The `code` on Requivo Web's error banner.** Presentational, and outside the error vocabulary the
  `--json` outputs publish. Branch on the HTTP status.
- **The `[api]` extra (`requivo.api`, `pip install 'requivo[api]'`) -- the whole surface, paths and
  bodies alike, until a named freeze event.** `create_api()` exists as of #425 and ships behind an
  optional extra; no route, method, status or response shape it answers with is stable, and none of
  them is a breaking change to move. The freeze is a specific event, not a version number:
  `docs/decisions/0004-the-http-api-facade.md` states it and the three preconditions gating it (the
  workspace becoming constructor state, `session delete`, and the estimate-artifact decision), after
  which this page gains its own API section -- paths, methods, statuses, the error envelope, and
  each response's top-level shape -- in the same "stable" form as everything above it. Until then,
  this page's silence about the API is the promise: there isn't one yet.

Everything **not** on this list and not promised above is in neither column, which is the state #89
was filed about. If you find one, that is a bug in this page rather than a licence to assume: file it.
