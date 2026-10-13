"""The documents' language (#790): the code a session records, its validation, and the guess offered
as its default, read off the request's function words: no provider call is paid to make it.
`decision: documents-speak-the-chosen-language`.
"""
from __future__ import annotations

import re

from requivo.core.errors import InvalidLanguageError

DEFAULT_DOCS_LANGUAGE = "en"

# The BCP 47 (RFC 5646) langtag grammar, lower-cased: language, script, region, variants, extensions
# (a singleton followed by at least one 2-8 subtag) and private use, or a private-use tag alone, so
# `en-u-ca-gregory` and `x-klingon` pass while `en-u` or `en-x` do not. The code reaches a paid prompt,
# so nothing else gets through (invariant 14), and nothing longer than BCP 47's practical maximum
# either (invariant 3: refused, never cut).
MAX_LANGUAGE_TAG_LENGTH = 35
_PRIVATE = r"x(?:-[a-z0-9]{1,8})+"
_CODE = re.compile(
    r"(?:[a-z]{2,3}(?:-[a-z]{4})?(?:-(?:[a-z]{2}|[0-9]{3}))?(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*"
    r"(?:-[a-wy-z0-9](?:-[a-z0-9]{2,8})+)*(?:-" + _PRIVATE + r")?|" + _PRIVATE + r")\Z")

LANGUAGE_NAMES = {"en": "English", "fr": "French", "es": "Spanish", "de": "German"}

# Function words only, per language; a word two languages share counts for both, so only the words
# that differ decide.
_FUNCTION_WORDS = {code: frozenset(words.split()) for code, words in (
    ("en", "the and of to is are we our you your for with that this would like need needs be an it in "
           "on from have has can should will want"),
    ("fr", "le la les des du de et est sont nous notre nos vous votre pour avec que qui une un dans sur "
           "pas au aux ce cette ces il ils elle on souhaitons voudrions avons besoin"),
    ("es", "el la los las de del y es son nosotros nuestro nuestra para con que una un en por como se "
           "lo al necesitamos queremos tenemos esta este"),
    ("de", "der die das und ist sind wir unser unsere sie für mit dass ein eine einen nicht zu den dem "
           "von auf im es brauchen möchten haben wird"),
)}


def normalize_language(code: str) -> str:
    """`code` as a session records it, or `InvalidLanguageError`: `FR` → `fr`, `pt_BR` → `pt-br`, and
    an English language name the table knows (`French`) → its code."""
    said = code.strip().lower().replace("_", "-")
    said = {name.lower(): c for c, name in LANGUAGE_NAMES.items()}.get(said, said)
    if len(said) > MAX_LANGUAGE_TAG_LENGTH or not _CODE.match(said):
        raise InvalidLanguageError(
            f"{code[:MAX_LANGUAGE_TAG_LENGTH]!r} is not a language code of at most {MAX_LANGUAGE_TAG_LENGTH} "
            "characters; name one such as en, fr, es or de",
            details={"language": code[:MAX_LANGUAGE_TAG_LENGTH + 1], "max_length": MAX_LANGUAGE_TAG_LENGTH})
    return said


def resolve_docs_language(recorded: str | None) -> str:
    """The language a session's documents are written in: English when none was recorded (a session
    from before #790), the recorded code otherwise, refused when it is not one."""
    return DEFAULT_DOCS_LANGUAGE if recorded is None else normalize_language(recorded)


def detect_language(text: str) -> str | None:
    """The request's language when its function words say so clearly, `None` when they do not: at
    least two hits, and strictly more than any other language."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    hits = sorted(((sum(w in vocab for w in words), code) for code, vocab in _FUNCTION_WORDS.items()),
                  reverse=True)
    (best, code), (second, _) = hits[0], hits[1]
    return code if best >= 2 and best > second else None


def default_docs_language(request: str) -> str:
    """The default offered for a new session: the request's language, else English."""
    return detect_language(request) or DEFAULT_DOCS_LANGUAGE


def language_name(code: str) -> str:
    """How a prompt or a question names `code`: `French (fr)`, or the bare code for one the table lacks."""
    name = LANGUAGE_NAMES.get(code.split("-", 1)[0])
    return f"{name} ({code})" if name else code
