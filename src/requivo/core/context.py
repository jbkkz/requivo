"""Context cards + prompt assembly — deterministic, provider-free.

This is the string-assembly half of what used to live in `core/llm.py`: it reads the bundled prompt
files, the framework schema, and the context cards, and injects them into a prompt template. It makes
**no LLM call and imports no provider** — it only turns assets into a system-prompt string — so it is
safe to keep in `core`. The provider imports `build_prompt()` to feed a model, which assembles the
cards through `load_context()`; every surface imports `resolve_cards()` to validate a `--context`
selection on the way in; `doctor` and `session verify` import `check_selection()` to ask whether a
*saved* selection still resolves without paying for a turn to find out, and `available_cards()` to
report the vocabulary itself. None of it needs the SDK.

Exactly three of those resolve a name against the installed cards — `resolve_cards`, `load_context`
and `check_selection` — and they must agree about an install that has none, so
`_cards_for_selection()` is the single guarded read all three share. `available_cards()` is
deliberately outside it, because reporting an empty install is its job rather than refusing one.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from requivo.core.errors import (
    ContextUnreadableError,
    EmptySelectionError,
    EmptySelectorTokenError,
    NoContextCardsError,
    RequivoError,
    UnknownContextCardError,
    UnsafeSelectorTokenError,
)
from requivo.core.selectors import normalize_tokens
from requivo.paths import CONTEXT, FRAMEWORK, PROMPTS, user_context_dir

# Every refusal `load_context` can produce, so `check_selection` can report exactly what the loader
# would raise without listing them twice. `ContextUnreadableError` is deliberately absent: "we could
# not look" is not a verdict about the selection, and `check_selection` lets it propagate.
#
# `UnsafeSelectorTokenError` is in here rather than escaping (#40): a hostile card name only arrives
# *persisted*, so the first code to see one is a health check, and a health check that raises takes
# the whole listing down instead of degrading one row (invariant 15). Reported, never raised —
# `test_check_selection_reports_a_hostile_persisted_card_rather_than_raising`.
_SELECTION_REFUSALS = (
    NoContextCardsError, EmptySelectionError, EmptySelectorTokenError, UnknownContextCardError,
    UnsafeSelectorTokenError,
)


def _card_paths() -> dict[str, Path]:
    """Loadable context cards keyed by stem: the bundled cards in the package, plus any the user drops
    in `user_context_dir()` (so a pip-installed setup is extensible without a source checkout). A user
    card whose stem matches a bundled one **overrides** it — you can tweak a built-in without editing
    the package. `_`-prefixed files are skipped. Emitted in sorted-stem order so the assembled system
    is deterministic and the prompt cache holds."""
    paths: dict[str, Path] = {}
    for directory in (CONTEXT, user_context_dir()):  # user dir second → its cards win on stem clash
        if not directory.exists():
            continue
        # `Path.glob` swallows `PermissionError` and yields nothing, so a directory that cannot be
        # read is indistinguishable from one holding no cards — the absence this module is most
        # expensive to get wrong, since the empty result then reads as a complete vocabulary and
        # every card in that directory becomes an "unknown context card" whose stated remedy is to
        # restore a file that is already there. `iterdir()` raises where `glob` does not, so it is
        # used as the readability probe; the selection itself still goes through `glob`, whose match
        # rule (case-insensitive on Windows, case-sensitive on POSIX) is deliberately left alone.
        # The cost is one extra directory walk over a handful of files.
        try:
            list(directory.iterdir())
        except OSError as e:
            raise ContextUnreadableError(
                f"the context-card directory {directory} exists but cannot be read: {e}. Fix its "
                "permissions — cards in it would otherwise be reported as missing.",
                details={"directory": str(directory)},
            ) from e
        for p in sorted(directory.glob("*.md")):
            if not p.name.startswith("_"):
                paths[p.stem] = p
    return paths


def _cards_for_selection() -> dict[str, Path]:
    """The card table a **selection** is resolved against: `_card_paths()` with the empty-install
    guard already applied.

    One name every selector shares, so a fourth inherits the guard instead of re-deriving it; without
    it a selector answers `unknown_context_card` where its siblings answer `no_context_cards` (#41) —
    `test_every_card_selector_reports_the_same_code_for_the_same_install`.

    `available_cards()` deliberately does **not** route through here, which is why the guard cannot
    live in `_card_paths()` itself: observing an empty install is that function's job, and `doctor`'s
    `empty` state is a public `--json` field only an observation can produce. The split is between
    looking and selecting, not between guarded and unguarded by accident.
    """
    paths = _card_paths()
    _require_any_card(paths)
    return paths


def available_cards() -> list[str]:
    """Stems of the loadable context cards (bundled + user), sorted — the vocabulary of the
    `--context` selector.

    Reports an empty install as `[]` rather than refusing it; `_cards_for_selection` is the guarded
    read, and the paragraph there says why this one must stay observational."""
    return sorted(_card_paths())


def card_byte_size(path: Path) -> int:
    """The bytes one card contributes to a prompt — **not** its size on disk.

    `st_size` over-counts by one byte per line on Windows, where git checks text out with CRLF and
    the text-mode read collapses it before `{{CONTEXT}}` sees it, so the figure #257 exists to
    disclose was inflated on exactly one platform:
    `test_a_card_weighs_the_same_whatever_its_line_endings`. Decoding and re-encoding is the loader's
    own operation, so an undecodable card raises here rather than reporting a plausible size for a
    file `load_context()` would refuse (invariant 16).
    """
    return len(path.read_text(encoding="utf-8").encode("utf-8"))


def average_card_byte_size() -> int | None:
    """Average prompt weight, in bytes, across every loadable card (bundled + user) — `None` for an
    empty install. It discloses the cost/dilution tradeoff of the all-cards default before a paid
    call (#257), measured rather than typed into prose so the figure cannot go stale, and
    observational like `available_cards()` beside it: a UI hint has no business raising on an empty
    install. `test_average_card_byte_size_matches_an_independent_computation` and
    `test_average_card_byte_size_is_none_on_an_empty_install`; measured through `card_byte_size`,
    never `st_size`, for the reason that function gives."""
    paths = _card_paths()
    if not paths:
        return None
    return sum(card_byte_size(p) for p in paths.values()) // len(paths)


def resolve_cards(tokens: Iterable[str]) -> list[str] | None:
    """Map caller-supplied card names to card stems, case-insensitively. Returns None when *no*
    selection was made (== all cards), and raises on a name that does not exist or on an empty token.

    The failure mode this closes is silent *widening*: filtering unknown names out leaves an empty
    selection, and every downstream reader spells that "load every card". One resolver, shared by the
    CLI and the Web, so no surface is lenient where another is strict; the empty-token entrance into
    the same widening is closed by `normalize_tokens`
    (`test_resolve_cards_refuses_an_empty_token_instead_of_returning_all_cards`).

    **An install with no cards at all is refused here, ahead of the whole selection** (#41), so a
    card-less install reports itself instead of blaming the reader's spelling
    (`test_resolve_cards_on_a_zero_card_install_names_the_install_not_the_card`, with
    `test_the_install_is_diagnosed_ahead_of_a_malformed_token_too` for the precedence). A selection
    of no tokens at all stays outside that guard, for uniformity rather than leniency —
    `test_no_selection_at_all_is_still_no_selection`.
    """
    tokens = list(tokens)
    if not tokens:
        return None
    # One guarded read of the table, used for both the lookup and the error's `Available:` line. Those
    # were two separate `available_cards()` calls, so the vocabulary a reader was told to choose from
    # was enumerated separately from the one their name was matched against.
    paths = _cards_for_selection()
    keys = normalize_tokens(tokens, what="context card")
    # `sorted` is a tie-break, not tidiness, so do not drop it: two installed stems can differ only
    # in case — a bundled `foo.md` beside a user `Foo.md` — and they are two entries here, since
    # `_card_paths()` only collapses an *exact* stem clash. Which one a typed `foo` resolves to is
    # then decided by iteration order, and `sorted` is what `available_cards()` applied before this
    # read replaced it. Preserved deliberately: which of the two should win is a real question, and a
    # bug fix silently loading a different card than it did yesterday is not the place to answer it.
    avail = {stem.lower(): stem for stem in sorted(paths)}
    picked, unknown = [], []
    for raw, key in zip(tokens, keys):
        # an unknown name is echoed as typed (stripped), so the error names what the caller wrote
        (picked if key in avail else unknown).append(avail.get(key, raw.strip()))
    if unknown:
        raise UnknownContextCardError(
            f"unknown context card(s): {', '.join(unknown)}. Available: {', '.join(sorted(paths))}",
            details={"unknown": unknown},
        )
    return picked or None


def load_context(only: list[str] | None = None) -> str:
    """Concatenate the context cards. `only` (card stems) restricts the set — this is how a session
    trims irrelevant cards so they don't dilute impact estimation (every card is loaded otherwise).
    Selection is per-session, so the assembled system stays byte-identical across a run's calls and
    the prompt cache still holds.

    **An empty `{{CONTEXT}}` is never a legitimate thing to send a provider, whatever emptied it** —
    a selection that no longer resolves, `only=[]`, or an install with no cards at all (#33). The
    cost is the `information_value = uncertainty x impact` driver silently off on a call that was
    billed anyway, so there is deliberately no "then load nothing" fallback; recovery is to restore
    the card, point `REQUIVO_CONTEXT_DIR` at it, or `session rescope` (#168).
    `test_load_context_refuses_a_selection_that_matched_nothing`,
    `test_load_context_refuses_an_empty_selection_and_an_empty_token`,
    `test_a_persisted_card_selection_is_visible_when_the_card_is_gone`,
    `test_load_context_refuses_an_install_with_no_cards_at_all` and
    `test_build_prompt_never_sends_an_empty_context_to_a_paid_call`.
    """
    paths = _cards_for_selection()
    # `only` is materialised before the guard iterates it — a generator read twice yields nothing
    keep = _selection_keys(list(only), paths) if only is not None else None
    # `encoding` is explicit because `read_text()` defaults to the *locale's* encoding, not the file's,
    # and mojibake sent to the provider is invisible from a UTF-8 machine (invariant 16) —
    # `test_the_prompt_assembly_path_never_decodes_an_asset_with_the_locale_encoding`.
    cards = [f"## {stem}\n{paths[stem].read_text(encoding='utf-8')}"
             for stem in sorted(paths)
             if keep is None or stem.lower() in keep]
    return "\n\n".join(cards)


def _require_any_card(paths: dict[str, Path]) -> None:
    """Refuse an install that has no context cards at all.

    The third state beside "the card you named is not there" and "we could not look": we looked, at
    every root, and there is nothing. It is checked before the selection because with no cards
    installed *every* name is unknown — technically true, and it sends the reader to check the name
    they typed when the fault is that there is nothing to match against.
    """
    if paths:
        return
    roots = [str(CONTEXT), str(user_context_dir())]
    raise NoContextCardsError(
        "no context cards are installed, so there is no product context to reason from — impact "
        "estimation is the product's central idea and it runs on these cards. Looked in: "
        f"{' and '.join(roots)}. This install is incomplete: reinstall requivo, or point "
        "REQUIVO_CONTEXT_DIR at a directory holding your cards.",
        details={"roots": roots})


def _selection_keys(only: list[str], paths: dict[str, Path]) -> set[str]:
    """The guard a card selection must pass, as one function: the normalized keys it names, or the
    refusal it earns.

    It exists so that the check and the thing checked cannot drift. `load_context` applies it, and
    `check_selection` asks it as a question — a health check that reimplemented the rule would
    eventually answer differently from the call it is supposed to predict, which is this issue's own
    defect class one level up.
    """
    wanted = normalize_tokens(only, what="context card")
    if not wanted:
        # `EmptySelectionError`, not `EmptySelectorTokenError` (#35): an empty *token inside* a
        # selection carries a `position` and a selection that is itself empty has none, so one code
        # over two `details` shapes handed a consumer following the documented advice a KeyError —
        # `test_an_empty_token_and_an_empty_selection_are_two_codes`.
        raise EmptySelectionError(
            "an empty context-card selection selects nothing. Pass no selection at all to load "
            "every card, or name the cards to load.",
            details={"selector": "context card", "tokens": 0})
    known = {stem.lower() for stem in paths}
    # echoed as typed, like `resolve_cards` — the two are one design and a caller reading the
    # error should see the name they wrote, not the lower-cased key it was matched by
    missing = [raw.strip() for raw, key in zip(only, wanted) if key not in known]
    if missing:
        raise UnknownContextCardError(
            f"unknown context card(s): {', '.join(missing)}. Available: "
            f"{', '.join(sorted(paths)) or '(none)'}",
            details={"unknown": missing},
        )
    return set(wanted)


def check_selection(only: list[str] | None) -> RequivoError | None:
    """Whether a stored card selection still loads **on this machine** — reported, never raised.

    `None` when it loads; otherwise the exact `RequivoError` `load_context` would raise, so a caller
    gets the stable code and the offending names in `details` rather than a re-derived message.

    It asks the loader's own guards rather than reimplementing the rule: a checker that answers a
    slightly different question from the call it predicts is this module's defect class one level up
    (`test_check_selection_agrees_with_load_context_on_every_selection`, and
    `test_check_selection_agrees_with_load_context_on_a_zero_card_install` for the `only=None`
    short-circuit that was true until #33 and false after it). What it buys is `doctor` and
    `session verify` saying so offline and for free, rather than the next paid turn discovering that
    a selection validated once at creation no longer resolves —
    `test_a_persisted_card_selection_is_visible_when_the_card_is_gone`.

    It deliberately does not swallow a failure of the *card directory* itself: "we could not look" is
    a different answer from "we looked and the card is gone".
    """
    try:
        paths = _cards_for_selection()
        if only is not None:
            _selection_keys(list(only), paths)
    except _SELECTION_REFUSALS as e:
        return e
    return None


def build_prompt(name: str, only: list[str] | None = None) -> str:
    """Load a prompt file and inject the schema + product context (optionally a subset of cards)."""
    # Explicit encoding for the same reason as the cards above: these assets are UTF-8 on disk and
    # `read_text()` would decode them with whatever the locale happens to be.
    schema = (FRAMEWORK / "model_schema.json").read_text(encoding="utf-8")
    text = (PROMPTS / name).read_text(encoding="utf-8")
    return text.replace("{{SCHEMA}}", schema).replace("{{CONTEXT}}", load_context(only))
