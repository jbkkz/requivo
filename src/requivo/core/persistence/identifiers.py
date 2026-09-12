"""Slug and filename identifiers: shape, reserved-name refusal, and derivation.

Split out of `core/persistence.py` by #550 (the lean pass, #548): every function that decides
whether a slug or a filename is a *safe name* -- the pattern, the length, the reserved-Windows-
device-name refusal, and `derive_slug`'s tokenising of a request into one -- with no dependency
on `Store`, `lock.py` or `scan.py`, so `lock.py`'s `lock_path` can import from here without a cycle.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from requivo.core.errors import InvalidFilenameError, InvalidSlugError, SessionUnreadableError

# A slug becomes a directory name, so it is bounded by what the filesystem accepts (~255 bytes on ext4
# and APFS, and the whole *path* on Windows). 80 leaves generous room for the session subtree beneath
# it. `derive_slug()` stays under the smaller base ceiling so a uniqueness suffix still fits inside the cap.
MAX_SLUG_LENGTH = 80
_SLUG_BASE_LENGTH = 64


# Latin letters NFKD cannot decompose, spelled out before the fold runs. NFKD splits a letter into a
# base plus a combining mark and the ASCII fold then drops the mark; a letter carrying no mark
# decomposes to *itself*, so the fold has nothing to do but delete it — 'Straßenverkehr' arrived as
# `stra-enverkehr`, which is the same mid-word mangling as `syst-me` one letter along. Lower-case
# only, because the fold runs after `.lower()`. Pinned by
# `test_folding_expands_a_latin_letter_that_carries_no_combining_mark`.
_LATIN_EXPANSIONS = str.maketrans({
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i",
})

# Function words dropped before the five tokens are taken (#245). English plus the three other Latin
# languages this project's users actually write requests in — the slug is a handle in whatever
# language the request arrived in, and folding accents without also dropping `nous`/`un`/`des` just
# moves the junk one character along.
#
# Two rules kept this list from becoming a general-purpose stoplist. A word is in it only if it is a
# function word in *some* in-scope language and not a content word in *any* of them — which is why
# `son`, `hay`, `sin`, `man`, `war`, `bin` and `hat` are deliberately absent despite being ordinary
# function words in French, Spanish or German. And nothing is here for being *common*: `system`,
# `data`, `report` and `user` open a great many requests and are exactly what the handle should say.
#
# The exclusion rule is prose, so it has a guard rather than a promise:
# `test_the_stopword_list_keeps_the_words_its_own_comment_promises_to_keep` asserts those seven are
# absent. `son` was in the Spanish half anyway, two lines under the paragraph saying it was not.
#
# Two accepted costs, stated rather than discovered. **`die`** is the German article and an English
# verb; the article is far the more frequent in a request opening, so the trade is taken knowingly.
# And **matching is case-folded ASCII, so a short function word collides with an acronym** — `er`
# eats the ER in "an ER diagram", as do `im`, `am`, `us`, `et`, `est`, `par`. Not fixable by pruning:
# dropping `er` costs German requests more often than "ER diagram" costs English ones. The
# fewer-than-two-survivors fallback below keeps it survivable, and `--slug` is the way past it.
_SLUG_STOPWORDS = frozenset("""
    a an and are as at be been being but by can could d did do does for from had has have i if in
    into is it its like ll m me my need needed needs of on or our ours ourselves please re s should
    so some t that the their them then there these they this those to us ve want wanted wants was
    way ways we were what when where which who whose will with would you your
    au aux avec avoir avons besoin ce ces cet cette dans de des du elle elles en est et etaient
    etait ete etre faut ils je la le les leur leurs ne nos notre nous ou par pas plus pour qui quoi
    sa se ses sommes sont sur tu un une vos votre vous y aimerions aimerait souhaitons souhaiterions
    voudrais voudrions voulons
    al como con del el ella ellos es esta estas este esto estos la las lo los mi necesita necesitamos
    necesito nuestra nuestro para podemos podria podriamos por que queremos quiero se ser su sus
    tiene tenemos un una unas unos deberiamos
    aber alle als am auch auf aus bei benotigen benotigt brauche brauchen braucht das dass dem den
    der des die dies diese ein eine einem einen einer eines er es fur haben ich ihr ihre im ist kein
    keine mit mochte mochten nach nicht oder sein sich sie sind uber um und von vor wenn wie wir
    wollen wurde wurden zu zum zur
""".split())


def derive_slug(text: str) -> str:
    """Derive a session directory name from arbitrary text — the one producer of the canonical shape.

    Public because it is consumed outside this module: `SessionService.slug_hint` is the surface's
    route to it, and `validate_slug` below is written against exactly what this emits.

    Three steps, and the order between the first two is load-bearing (#245). **Fold, then filter,
    then take five.** Taking five tokens verbatim off the front of a request named the greeting
    rather than the subject — `we-need-a-way-to`, from "We need a way to track vendor invoices" — so
    two unrelated requests differed only by the collision hash; filtering before folding would leave
    `syst`/`me` in the stream as two words neither list can match. Below two survivors the
    *unfiltered* words are used, because an all-function-word request is a real shape ("We need it")
    and an empty token list falls through to `discovery`, this function's own defect reintroduced by
    its fix.

    **The residual limit, documented because it is not fixed here:** the ASCII fold deletes
    non-Latin scripts, so a Japanese or Cyrillic request still lands on `discovery-<hash>` —
    transliteration needs a dependency this package does not carry.
    `test_a_non_latin_request_still_derives_the_documented_discovery_fallback`.

    Nothing re-derives a slug for a session that already exists, so a session on disk keeps its name;
    what did change is idempotent re-discovery, which `docs/compatibility.md` carries alongside the
    other "two versions, one workspace" promises. The alphabet is unchanged (`[a-z0-9-]`).
    """
    folded = unicodedata.normalize(
        "NFKD", text.lower().translate(_LATIN_EXPANSIONS)).encode("ascii", "ignore").decode("ascii")
    tokens = re.findall(r"[a-z0-9]+", folded)
    content = [w for w in tokens if w not in _SLUG_STOPWORDS]
    words = (content if len(content) >= 2 else tokens)[:5]
    base = "-".join(words) or "discovery"
    if len(base) <= _SLUG_BASE_LENGTH:
        return base
    # Five words are usually short, but nothing guarantees it: one 300-character token yields a
    # 300-character directory name and the filesystem refuses it with a bare OSError. Truncate
    # deterministically, then re-attach identity as a short hash so two different long requests can
    # never collapse onto the same session directory.
    keep = base[:_SLUG_BASE_LENGTH - 7].rstrip("-")
    return f"{keep}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:6]}"




# A slug names a directory under the session root; it must never be able to escape it. `derive_slug()` and
# `resolve_slug()` always emit this shape, but an *explicit* `--slug` (or a future API caller) is
# untrusted input — so the two path constructors below validate before joining. The pattern forbids
# every traversal vector at once: `/`, `\`, `.`, `..`, a leading root, and the empty string.
#
# `\Z` and not `$`, here and on `_FILENAME_RE` below (#40, adjacent): Python's `$` also matches just
# before a trailing newline, so a guard whose stated job is to make a control character
# unrepresentable admitted exactly one.
# `test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline`.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")

# Windows refuses to create a file or directory named one of these, case-insensitively and whatever
# the extension (the OS matches the component *before the first dot*). Both name patterns admit them,
# so without this a session slugged 'con' is valid on macOS/Linux, exports fine, and then cannot be
# materialized by `session import` on a colleague's Windows machine -- a portability hole the session
# format's own promise never mentions (#221).
# `test_reserved_windows_device_names_are_refused_as_slugs`.
#
# Refused on *every* platform: refusing only on Windows would still let a POSIX user create an
# archive Windows can never open, which relocates the defect rather than closing it. `com0`/`lpt0`
# and a bare `com`/`lpt` are deliberately absent -- only `com1`-`com9` and `lpt1`-`lpt9` are real
# devices, and a check wider than the real set refuses a name nobody needed refused.
#
# This refuses *creation*. A session already on disk under a reserved name is data rather than a
# request to make anything, and reading it is a narrower conditional exception -- see
# `_refuse_new_reserved_slug` (#372).
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def _reserved_stem(name: str) -> bool:
    """Is the component of `name` before its first dot a Windows reserved device name?

    Shared by `validate_slug` (whose "stem" is the whole slug -- a slug never carries a dot) and
    `validate_filename` (whose stem is genuinely the part before the first `.`, so `con.tar.gz` is
    caught on `con` and not on `con.tar`)."""
    stem = name.split(".", 1)[0]
    return stem.lower() in _RESERVED_DEVICE_NAMES


def _raise_reserved_slug(slug: str) -> None:
    """The one wording for "this slug is a reserved Windows device name" -- shared by `validate_slug`
    (which raises it unconditionally) and `_refuse_new_reserved_slug` (which raises it only when
    nothing already claims the name, #372), so the two paths cannot drift into two different
    sentences for what is, from the caller's side, the identical refusal."""
    raise InvalidSlugError(
        f"invalid session slug {slug!r}: {slug.lower()!r} is a reserved Windows device name and "
        "cannot be created as a directory there",
        details={"slug": slug})


def _slug_shape(slug: str) -> str:
    """Pattern and length -- the parts of slug validity that hold regardless of what is already on
    disk. `validate_slug` layers the reserved-device-name refusal on top of this unconditionally;
    `_child_of` and `lock_path` layer a *conditional* version of it instead (#372, see
    `_refuse_new_reserved_slug`)."""
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise InvalidSlugError(
            f"invalid session slug {slug!r}; expected kebab-case [a-z0-9-], e.g. 'leave-approval'",
            details={"slug": slug})
    # Length is part of validity, not a separate concern: an over-long slug is a directory name the
    # filesystem rejects, and it fails deep inside a write as an OSError instead of at the boundary.
    # `derive_slug()` never emits one; an explicit --slug or an API caller can.
    if len(slug) > MAX_SLUG_LENGTH:
        raise InvalidSlugError(
            f"session slug is {len(slug)} characters; the maximum is {MAX_SLUG_LENGTH}",
            details={"slug": slug[:MAX_SLUG_LENGTH], "length": len(slug),
                     "max_length": MAX_SLUG_LENGTH})
    return slug


def _refuse_new_reserved_slug(slug: str, existing_check: Path) -> None:
    """Refuse a reserved Windows device name (#221) unless something already occupies
    `existing_check` -- the creation/read split #372 draws. A genuinely *new* reserved slug is refused
    exactly as strictly as before, because `_probe` finds nothing there; what changes is a name a
    session already occupies on disk, which is data rather than a request to create anything and was
    otherwise stranded behind the very guard meant to keep it portable.
    `test_reserved_windows_device_names_are_refused_as_slugs` and
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken`.

    Routed through `_probe` rather than a bare `.exists()` so the third answer stays a third answer:
    a stat this cannot make surfaces as `SessionUnreadableError` instead of this function picking a
    side of a question nobody could decide."""
    if _reserved_stem(slug) and not _probe(existing_check, slug):
        _raise_reserved_slug(slug)


def validate_slug(slug: str) -> str:
    """Return `slug` if it is a safe session identifier, else raise `InvalidSlugError`. Lives in Core
    so every surface (CLI, provider, a future web service) inherits the same directory-traversal guard,
    not just FastAPI. Belt-and-suspenders: callers additionally confirm the resolved path stays under
    the root, but the pattern alone already makes a separator or dot segment unrepresentable.

    **The reserved-device-name refusal here is unconditional, on purpose** (#372): a caller who
    *names* a slug directly is asking to create or address one deliberately, and widening this
    function would widen every creation path with it. `_child_of` and `lock_path` are where an
    *existing* session earns the narrower read-only exception -- see `_refuse_new_reserved_slug`, and
    `test_reserved_windows_device_names_are_refused_as_slugs` for this half."""
    slug = _slug_shape(slug)
    # A slug never carries a dot (the pattern above forbids it), so this is a whole-slug check --
    # see `_reserved_stem` and #221.
    if _reserved_stem(slug):
        _raise_reserved_slug(slug)
    return slug


def is_slug(name: str) -> bool:
    """Whether `name` is a slug that could be *created* right now — the same question `validate_slug`
    answers, as a predicate: the unconditional, creation-time form, which refuses a reserved
    Windows device name whether or not anything already occupies it.

    Deliberately implemented by *calling* it rather than by re-testing `_SLUG_RE`: validity is the
    pattern **and** the length, and an earlier predicate written against the pattern alone marked an
    81-character kebab-case directory as a name a session would silently lose, when `canonical_dir`
    refuses it outright and loudly. One rule, one place, found by review.

    **Has no caller in this codebase as of #408, and that is correct rather than dead weight.** The
    two callers it used to have were both describing an entry that already exists on disk, so their
    question is `_shape_only`'s, not this one's -- see
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken` for what that cost.
    `is_slug` stays as the creation-time predicate `validate_slug` is missing a bool form of, for a
    caller that genuinely asks about a fresh name."""
    try:
        validate_slug(name)
    except InvalidSlugError:
        return False
    return True


def _shape_only(name: str) -> bool:
    """Whether `name` matches `_slug_shape` -- pattern and length -- with no reserved-device-name
    question asked at all, as a bool.

    The read-time predicate for a caller that already knows, by construction, that something occupies
    the path it would ask `_refuse_new_reserved_slug` about -- so that conditional check could only
    ever answer "does not refuse". `_is_lock_stem` (#409) and `_describe_non_session`'s `slug_shaped`
    (#408) are both this now; see each for why its own "something occupies the path" holds, and
    `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted` and
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken` for the two defects
    the other predicates caused there. Not `_slug_shape` bare, which raises rather than a bool."""
    try:
        _slug_shape(name)
    except InvalidSlugError:
        return False
    return True


def _is_lock_stem(stem: str) -> bool:
    """Whether a `<stem>.lock` or `<stem>.discovering` under `lock_root()` is one this store could
    have written -- the **stem** half of what `lock_path` and
    `services.discovery._discovery_guard_path` each validate before joining their own suffix.

    **Shape alone, since #409 -- not `_slug_shape` plus a read of `session_root()`** (#401). A lock
    file's provenance is fixed the moment it is written, so asking the read-time question made the
    classification of a fixed fact depend on whether an unrelated directory still exists *now*:
    `nul.lock`, written while a `nul` session was open, read as unrecognised the moment that session
    was deleted -- invariant 17's shape, a verdict decided by a resource the answer does not name,
    and a violation of `scan_lock_root`'s own "never *is `slug` still a session*". Whether a session
    still matches a stem is `_lock_health`'s question, asked separately. Pinned by
    `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted` and
    `test_a_lock_file_for_a_reserved_name_with_no_session_on_disk_is_recognised_not_residue`.

    **Not `lock_path(stem)` in a `try` either**, which reads as the tidier "one rule, one place" and
    imports a third check about a *path*: `lock_path` ends in `is_contained(root / (stem + ".lock"))`,
    so asked the `.discovering` question it answered about a **different file** and an unrelated
    symlink at `<stem>.lock` flipped a real guard file into `unexpected` -- invariant 17 one layer
    down, a verdict about one entry decided by a sibling's state.
    `test_a_symlink_at_the_lock_name_does_not_sink_the_guard_file_beside_it`. Containment is not
    missing here, it is inapplicable: entries come out of `iterdir(root)`, `_slug_shape` makes a
    separator unrepresentable in a stem, and a symlink at the entry itself is already excluded.

    **Not `is_slug` either**, whose refusal is unconditional because it guards *creation*: asking the
    creation-time question reported a reserved-name session's own files as residue nobody recognises
    (#401) -- `test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue`.

    **No longer probes the filesystem at all**, so `scan_lock_root`'s `unexaminable` bucket has lost
    a source by design: `test_a_reserved_lock_stem_no_longer_probes_the_session_root`."""
    return _shape_only(stem)


# A filename is the *other* half of an artifact write target, and it was unvalidated while its slug
# sibling on the same call was not. The same shape as `_SLUG_RE`, one separator class wider: a name is
# runs of [a-z0-9] joined by single `.`, `-` or `_`. That forbids every vector at once — `/`, `\`, a
# `..` segment (two dots in a row cannot be written), a leading or trailing separator, a leading dot,
# and the empty string — while still admitting every name the store actually writes
# (`solution-assessment.md`, `acceptance-criteria.md`, `epic.github.json`).
#
# Deliberately lowercase-only, matching the slug: a rejection is loud and one edit away, whereas a
# permissive pattern is the thing being removed here. Every filename in `ARTIFACT_FILENAMES` and every
# epic export name already fits.


_FILENAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")

# Room for the whole name plus the unique scratch suffix `_atomic_write` appends (a dot, the pid, 8
# hex and `.tmp` — about 20 characters), inside the ~255-byte ceiling ext4 and APFS impose.
MAX_FILENAME_LENGTH = 120


def validate_filename(filename: str) -> str:
    """Return `filename` if it is a safe bare filename, else raise `InvalidFilenameError`.

    The sibling of `validate_slug`, and it exists for the reason stated there: the guard belongs in
    Core so every surface inherits it, not in the callers that happen to be careful. Every in-repo
    caller passes a literal or an `ARTIFACT_FILENAMES` lookup — which is precisely why this was
    missing, and precisely the argument invariant 14 makes for putting it here anyway: the threat
    model is the external consumer calling the service directly, not the CLI."""
    if not isinstance(filename, str) or not _FILENAME_RE.match(filename):
        raise InvalidFilenameError(
            f"invalid artifact filename {filename!r}; expected a bare lowercase name such as "
            "'prd.md' — no directories, no dot segments, no leading dot",
            details={"filename": filename})
    # The stem before the first dot, not the whole filename: `con.tar.gz` is reserved on the `con`
    # component alone, and Windows refuses it regardless of what follows (#221, see `_reserved_stem`).
    if _reserved_stem(filename):
        stem = filename.split(".", 1)[0]
        raise InvalidFilenameError(
            f"invalid artifact filename {filename!r}: {stem.lower()!r} is a reserved Windows device "
            "name and cannot be created as a file there",
            details={"filename": filename})
    # Length is part of validity for the same reason it is for a slug: an over-long name is refused by
    # the filesystem deep inside the write, as a bare OSError, instead of at the boundary.
    if len(filename) > MAX_FILENAME_LENGTH:
        raise InvalidFilenameError(
            f"artifact filename is {len(filename)} characters; the maximum is {MAX_FILENAME_LENGTH}",
            details={"filename": filename[:MAX_FILENAME_LENGTH], "length": len(filename),
                     "max_length": MAX_FILENAME_LENGTH})
    return filename




def _probe(marker: Path, slug: str) -> bool:
    """Is `marker` there? — with the third answer routed out through the error channel.

    `Path.exists()` has two returns and three outcomes: it swallows `ENOENT`/`ENOTDIR` into `False`
    and **re-raises everything else**, which escaped as a bare `PermissionError` traceback — the
    identical unguarded probe #80 removed from `_scan_session_root`, hit again by the `session verify
    <slug>` footer #80 itself prints (#97). `test_session_exists_answers_could_not_tell_through_the_error_channel`
    and `test_absent_is_still_false_because_absent_is_a_real_answer`.

    **The bool is not widened, because a bool cannot hold three states.** `cli.py` and
    `session import --force` read these to decide whether to *create or overwrite*, so answering
    `False` would turn *I could not tell* into a write proceeding on an unknown. The third state
    leaves as `SessionUnreadableError`; `ENOENT` still returns `False`, because absent is a real
    answer and the commonest one."""
    try:
        return marker.exists()
    except OSError as e:
        raise SessionUnreadableError(
            f"could not determine whether session '{slug}' exists: {e}",
            details={"slug": slug}) from e


