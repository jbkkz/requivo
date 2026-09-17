"""Slug and filename identifiers: shape, reserved-name refusal, and derivation (#550). No dependency
on `Store`, `lock.py` or `scan.py`, so `lock.py` can import from here without a cycle.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from requivo.core.errors import InvalidFilenameError, InvalidSlugError, SessionUnreadableError

# A slug becomes a directory name (~255 bytes on ext4/APFS, the whole path on Windows); `derive_slug()`
# stays under the smaller base ceiling so a uniqueness suffix still fits.
MAX_SLUG_LENGTH = 80
_SLUG_BASE_LENGTH = 64


# Latin letters NFKD cannot decompose, spelled out before the fold (`ß` was deleted mid-word). Lower-case
# only, since the fold runs after `.lower()`. `test_folding_expands_a_latin_letter_that_carries_no_combining_mark`.
_LATIN_EXPANSIONS = str.maketrans({
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i",
})

# Function words dropped before the five tokens are taken (#245), for English, French, Spanish and
# German. A word is in only if it is a function word in some in-scope language and a content word in
# none (`test_the_stopword_list_keeps_the_words_its_own_comment_promises_to_keep` pins the seven
# exclusions); nothing is here for being common. Accepted costs: `die`, and short function words
# colliding with acronyms (`er`); `--slug` is the way past it.
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
    """Derive a session directory name from arbitrary text: fold, then filter stopwords, then take
    five tokens, in that order (#245); below two survivors the unfiltered words are used. A non-Latin
    request lands on `discovery-<hash>`: `test_a_non_latin_request_still_derives_the_documented_discovery_fallback`.
    A session on disk keeps its name; the alphabet is `[a-z0-9-]`."""
    folded = unicodedata.normalize(
        "NFKD", text.lower().translate(_LATIN_EXPANSIONS)).encode("ascii", "ignore").decode("ascii")
    tokens = re.findall(r"[a-z0-9]+", folded)
    content = [w for w in tokens if w not in _SLUG_STOPWORDS]
    words = (content if len(content) >= 2 else tokens)[:5]
    base = "-".join(words) or "discovery"
    if len(base) <= _SLUG_BASE_LENGTH:
        return base
    # One 300-character token is a directory name the filesystem refuses: truncate, then re-attach a hash.
    keep = base[:_SLUG_BASE_LENGTH - 7].rstrip("-")
    return f"{keep}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:6]}"




# A slug names a directory under the session root and must never escape it: the pattern forbids every
# traversal vector at once. `\Z`, not `$`, which matches before a trailing newline (#40):
# `test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline`.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")

# Windows refuses these names case-insensitively, on the component before the first dot, so a slug
# `con` exports fine and cannot be imported on a colleague's machine (#221); refused on every platform.
# Only `com1`-`com9`/`lpt1`-`lpt9` are real devices. This refuses *creation*; reading an existing one is
# `_refuse_new_reserved_slug`'s narrower exception (#372). `test_reserved_windows_device_names_are_refused_as_slugs`.
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def _reserved_stem(name: str) -> bool:
    """Is the component of `name` before its first dot a Windows reserved device name? (`con.tar.gz` is caught on `con`.)"""
    stem = name.split(".", 1)[0]
    return stem.lower() in _RESERVED_DEVICE_NAMES


def _raise_reserved_slug(slug: str) -> None:
    """The one wording for a reserved-device-name refusal, shared by both paths (#372)."""
    raise InvalidSlugError(
        f"invalid session slug {slug!r}: {slug.lower()!r} is a reserved Windows device name and "
        "cannot be created as a directory there",
        details={"slug": slug})


def _slug_shape(slug: str) -> str:
    """Pattern and length: the parts of slug validity that hold regardless of what is on disk."""
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise InvalidSlugError(
            f"invalid session slug {slug!r}; expected kebab-case [a-z0-9-], e.g. 'leave-approval'",
            details={"slug": slug})
    # Length is part of validity: an over-long slug fails deep inside a write as a bare OSError.
    if len(slug) > MAX_SLUG_LENGTH:
        raise InvalidSlugError(
            f"session slug is {len(slug)} characters; the maximum is {MAX_SLUG_LENGTH}",
            details={"slug": slug[:MAX_SLUG_LENGTH], "length": len(slug),
                     "max_length": MAX_SLUG_LENGTH})
    return slug


def _refuse_new_reserved_slug(slug: str, existing_check: Path) -> None:
    """Refuse a reserved Windows device name (#221) unless something already occupies `existing_check`
    (#372): a session on disk under such a name is data, not a request to create anything. Through
    `_probe`, so a stat this cannot make is `SessionUnreadableError`, not a side taken.
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken`."""
    if _reserved_stem(slug) and not _probe(existing_check, slug):
        _raise_reserved_slug(slug)


def validate_slug(slug: str) -> str:
    """Return `slug` if it is a safe session identifier, else raise `InvalidSlugError`. The
    reserved-device-name refusal is unconditional here (#372): naming a slug is asking to create one.
    `test_reserved_windows_device_names_are_refused_as_slugs`."""
    slug = _slug_shape(slug)
    # A slug never carries a dot, so this is a whole-slug check (#221).
    if _reserved_stem(slug):
        _raise_reserved_slug(slug)
    return slug


def is_slug(name: str) -> bool:
    """Whether `name` is a slug that could be *created* right now: `validate_slug` as a predicate,
    by calling it (validity is the pattern and the length). No caller as of #408: a read-time
    question is `_shape_only`'s."""
    try:
        validate_slug(name)
    except InvalidSlugError:
        return False
    return True


def _shape_only(name: str) -> bool:
    """Pattern and length only, as a bool, with no reserved-name question: the read-time predicate for
    an entry known to occupy its path (#408, #409). `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted`."""
    try:
        _slug_shape(name)
    except InvalidSlugError:
        return False
    return True


def _is_lock_stem(stem: str) -> bool:
    """Whether a `<stem>.lock` or `<stem>.discovering` is one this store could have written: shape
    alone (#409), never a read of `session_root()` (#401, invariant 17), never `lock_path` (which
    answers about a different file) and never `is_slug` (which guards creation).
    `test_a_symlink_at_the_lock_name_does_not_sink_the_guard_file_beside_it`,
    `test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue`,
    `test_a_reserved_lock_stem_no_longer_probes_the_session_root`."""
    return _shape_only(stem)


# A filename is the other half of an artifact write target: `_SLUG_RE`'s shape, one separator class
# wider (`.`, `-`, `_`), lowercase only, admitting every name the store writes.


_FILENAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")

# Room for the name plus `_atomic_write`'s scratch suffix, inside the ~255-byte ceiling.
MAX_FILENAME_LENGTH = 120


def validate_filename(filename: str) -> str:
    """Return `filename` if it is a safe bare filename, else raise `InvalidFilenameError`; the sibling
    of `validate_slug`, in Core so every surface inherits it (invariant 14)."""
    if not isinstance(filename, str) or not _FILENAME_RE.match(filename):
        raise InvalidFilenameError(
            f"invalid artifact filename {filename!r}; expected a bare lowercase name such as "
            "'prd.md' — no directories, no dot segments, no leading dot",
            details={"filename": filename})
    # The stem before the first dot, as Windows reads it (#221).
    if _reserved_stem(filename):
        stem = filename.split(".", 1)[0]
        raise InvalidFilenameError(
            f"invalid artifact filename {filename!r}: {stem.lower()!r} is a reserved Windows device "
            "name and cannot be created as a file there",
            details={"filename": filename})
    # Length is part of validity, as for a slug.
    if len(filename) > MAX_FILENAME_LENGTH:
        raise InvalidFilenameError(
            f"artifact filename is {len(filename)} characters; the maximum is {MAX_FILENAME_LENGTH}",
            details={"filename": filename[:MAX_FILENAME_LENGTH], "length": len(filename),
                     "max_length": MAX_FILENAME_LENGTH})
    return filename




def _probe(marker: Path, slug: str) -> bool:
    """Is `marker` there, with the third answer routed out through the error channel: `Path.exists()`
    swallows `ENOENT` into `False` and re-raises everything else, which becomes `SessionUnreadableError`
    rather than widening a bool (#80, #97). `test_session_exists_answers_could_not_tell_through_the_error_channel`."""
    try:
        return marker.exists()
    except OSError as e:
        raise SessionUnreadableError(
            f"could not determine whether session '{slug}' exists: {e}",
            details={"slug": slug}) from e


