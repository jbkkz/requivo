"""A named test is a reference, so it has to resolve — and be findable (#75).

This repository answers "why is this line here?" by naming the test that enforces it, in `src/` and
in `CLAUDE.md`. Nobody decided that convention; it accreted, it is the right one, and
**nothing checked it**, which is this project's own recurring defect aimed at its own documentation.

Two things can go wrong and both were live when this guard was written:

* **The test is renamed or deleted.** The comment then names something that does not exist, and a
  reader who greps for it concludes the guard was removed — or, worse, that they cannot find it and
  gives up. A reference that does not resolve is worse than no reference: it spends the reader's
  trust and returns nothing.
* **The name is split by a line wrap.** `contracts.py` carried
  ``test_the_persisted_mirror_copies_every_`` at the end of one comment line and
  ``constraint_it_restates`` at the start of the next. The test exists. The reference is still
  useless, because the only way anyone uses one of these is to select it and grep, and the string
  they select is not in the repository. Two of sixteen were in that state.

Why a *name* and not a path, which is the question #75 left open: paths in this repository move.
The package was renamed once (`product_copilot` → `requivo`), `deterministic.py` became a package,
and `test_cli_deterministic.py` became seven files — all within a fortnight. A test name survives
every one of those and a path survives none, so the reference format is the name, for a test and for
a decision record alike.

What this guard deliberately does **not** do is decide whether a given paragraph *should* carry a
reference. That is a judgement, it is stated in CLAUDE.md as a rule for a person to apply, and a test
that tried to enforce it would be guessing at intent. This one only checks that the references that
exist are true and findable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pytest
from _scan import list_files

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "requivo"
TESTS = REPO_ROOT / "tests"
# `scripts/` carries references too and was outside the scan until #137. It is not shipped in the
# wheel, which is why it was missed, and that is irrelevant to the convention: a reference is read by
# a maintainer with a grep, and the harness scripts are read by exactly that person.
SCRIPTS = REPO_ROOT / "scripts"
# `docs/` is the third place CLAUDE.md's own "Where a bug narrative lives" section names as
# narrative's right home, alongside this file and the invariant list -- and until #156 it was the
# one of the three the guard never opened. Measured before adding it: 8 references, all resolving,
# none wrapped, so there is no false-positive cost to weigh against the gap.
DOCS = REPO_ROOT / "docs"

# Files that may carry a reference. `.md` is in here for CLAUDE.md, which names two tests in its
# invariants and is read more often than any module. `.js` joined with #156: `tests/web/busy_harness.js`
# is the file the motivating instance for this whole issue sat in, and a `.js` file under `src/` or
# `scripts/` is exactly as readable-by-grep as a `.py` one. Measured before adding it: zero references
# in either vendored or first-party `.js` under those two roots, so widening the suffix costs nothing
# where it is applied.
SUBJECT_SUFFIXES = (".py", ".html", ".md", ".js")
EXTRA_SUBJECTS = (REPO_ROOT / "CLAUDE.md",)

# The *resolution* check's roots -- `tests/` joined them in #190, closing the gap #156 opened and
# #188 measured but declined to close. Widening this glob alone produced eleven apparent dangling
# references (measured on the tree at #190's own base commit) and every one was a false positive, of
# exactly two distinguishable kinds -- not a spectrum, which is what makes a mechanical rule possible
# rather than a judgement call repeated by hand on every future one:
#
# * **Eight files read "Split out of `test_cli_deterministic.py` by #141"**, naming a module #141
#   deleted on purpose, to recount what happened to *that file* rather than to point a reader at a
#   guard to go read. `_HISTORICAL_MENTION` recognizes this exact idiom -- already the established
#   phrasing for provenance, not invented for this guard -- and `resolvable_references()` blanks the
#   name it captures before resolution ever sees it. The rule is narrow on purpose: "Renamed from
#   `X.py`" or a bare mid-sentence "`X.py` used to hold this" does *not* match and stays checked, so a
#   future rename that skips the idiom is exactly as loud as a broken pointer -- see
#   `test_a_similarly_shaped_mention_that_is_not_the_idiom_still_dangles`. And the exemption only
#   blanks the matched span, not every occurrence of the name in the file -- see
#   `test_the_same_dangling_name_outside_the_idiom_still_dangles` -- so a file that both recounts a
#   split *and* separately points at the same dead name the ordinary way still goes red on the second
#   half.
# * **This guard's own file supplies the other three** (`test_cli_deterministic` in its own prose
#   quoting the idiom above to explain it, plus two of its own wrap-detector fixture strings, which
#   *have* to look like broken references for `test_the_wrap_detector_*`'s positive control to mean
#   anything). A file cannot be a trustworthy resolver of its own examples -- that is a structural
#   fact about self-reference, not a prose-parsing problem, so `RESOLUTION_EXEMPT_FILES` excludes this
#   module by identity rather than by pattern-matching its own docstring. It stays fully in the wrap
#   scan, which has no such hazard.
#
# `CHANGELOG.md` is excluded from every root below for the same reason stated where it is declared:
# released history's dead pointers are correct, not stale.
RESOLUTION_ROOTS = (SRC, SCRIPTS, DOCS, TESTS)

# A reference inside this exact idiom recounts what a file *used to be*, never what currently guards
# anything -- it is a historical mention wearing the same `` `name.py` `` shape a live pointer would.
# Matching on the fixed phrase means the rule cannot be gamed by simply avoiding a marker nobody was
# ever asked to add -- it recognizes an idiom this repository already used twenty times before this
# guard existed, not a new convention invented for it.
_HISTORICAL_MENTION = re.compile(r"[Ss]plit out of `(test_[a-z0-9_]+)\.py`")

# This guard's own file necessarily contains broken-looking references by design: a truncated
# fixture proving the wrap detector fires, an intact one proving it does not, and prose that quotes
# both shapes (and the split-history idiom above) to explain why the guard exists at all. None of
# that is a pointer for a reader to follow. Excluded from *resolution* by identity, never from *wrap*,
# which stays mechanical and applies to this file exactly like every other.
RESOLUTION_EXEMPT_FILES = (Path(__file__).resolve(),)

# A second, distinct reason a file is resolution-exempt: vendored, minified third-party code, where
# a `test_`-shaped identifier is somebody else's library function, never a claim about this suite.
# `htmx.min.js` needed no such exemption when it was vendored -- measured then: zero `test_`-shaped
# identifiers in it -- so this tuple did not exist until #504 vendored `swagger-ui-bundle.js`, whose
# own cookie-parsing helpers are functions literally named `test_cookie_name`/`test_cookie_value`,
# coincidentally shaped exactly like this guard's own reference pattern (verified directly against
# the vendored file, not assumed --
# `test_a_vendored_bundles_coincidental_identifier_is_not_a_dangling_reference` below re-verifies it
# on every run so a future re-vendor that drops the collision is caught rather than leaving a stale
# exemption). Kept separate from `RESOLUTION_EXEMPT_FILES` above
# -- a set of one, by construction, and asserted as such by
# `test_the_wrap_scan_still_reaches_one_file_further_than_the_resolution_scan` -- so the two reasons
# ("this guard's own file" and "not first-party prose at all") stay individually named rather than
# merged into one tuple that means neither thing precisely. Resolution-exempt only: the *wrap* check
# stays mechanical and reaches this file exactly like any other, and passes today because a single
# giant minified line gives `_WRAPPED` no line boundary to catch anything at.
VENDORED_RESOLUTION_EXEMPT_FILES = (
    (SRC / "api" / "static" / "vendor" / "swagger-ui" / "swagger-ui-bundle.js").resolve(),
)

# The wrap check has no pointer-versus-mention problem -- it is purely mechanical, so `tests/` was
# already here before #190 widened `RESOLUTION_ROOTS` to match it: it is where the motivating
# instance for #156 actually sat (`busy_harness.js`, truncated mid-identifier since the day it was
# written, invisible on both counts of being a `.js` file under `tests/`). The two root tuples are
# the same four directories now; they differ only by `RESOLUTION_EXEMPT_FILES`, this guard's own
# module, which the wrap check has no reason to skip -- see
# `test_the_wrap_scan_still_reaches_one_file_further_than_the_resolution_scan`.
WRAP_ROOTS = (SRC, SCRIPTS, DOCS, TESTS)

# A reference is a `test_`-prefixed identifier. The length floor keeps `test_x` in an example snippet
# from being read as a claim about the suite; every real reference in this repo is far longer.
_REFERENCE = re.compile(r"\btest_[a-z0-9_]{10,}\b")

# A decision record is referenced the same way and for the same reason — by a stable slug, never by
# a path. `docs/decisions/README.md` says why; this pattern is what makes the claim checkable, so the
# second convention does not ship unguarded the way the first one did.
_DECISION_REF = re.compile(r"`decision:\s*([a-z0-9-]+)`")
DECISIONS = REPO_ROOT / "docs" / "decisions"

# `docs/decisions/` is a *subject* -- a record's own references are resolved like any other file's --
# and it is deliberately **not a referrer** (#384). Reachability here means reachable from the
# documentation a reader actually enters through: `CLAUDE.md`, `docs/*.md`, `src/`, `scripts/`,
# `tests/`, plus `.github/`. Leaving the records inside their own referrer set turned the orphan
# check off for the shape a record normally has, not for an edge case: a record usually explains
# *where its pointer belongs*, and to say that it has to quote its own slug -- at which point it
# satisfies its own reachability and the guard is green. Measured, not reasoned: at `3245e7e`
# `0002-elicitation-schema-hand-kept.md` was referenced from nowhere but itself and this guard
# passed.
#
# The whole directory comes out rather than only self-reference, which was the design question
# inside #384. Excluding self alone still passes a two-record cycle that cites nothing outside
# itself, and that unreachable island is exactly the failure the guard is named for. The cost is
# stated rather than hidden: a record whose only pointer is a sibling record now reads as an orphan,
# and the remedy is to leave the pointer where a reader will meet it. Pinned by
# `test_a_record_that_only_quotes_its_own_slug_is_an_orphan`,
# `test_a_record_reachable_only_from_a_sibling_record_is_an_orphan` and
# `test_the_records_are_resolution_checked_but_are_not_their_own_referrers`.
REFERRER_EXEMPT_ROOTS = (DECISIONS,)

# The wrap: an identifier that ends a line on a trailing underscore. A Python identifier never
# legitimately ends in `_` here — the suite has none — so this is the split, not a naming style.
_WRAPPED = re.compile(r"\btest_[a-z0-9_]*_$", re.MULTILINE)

# The same rule for a decision slug, detected by the *missing closing backtick* rather than by a
# trailing hyphen. `_WRAPPED` has to settle for a heuristic because a bare identifier gives it
# nothing else; an inline code span does, and a span that opens on a line and does not close on it
# is broken wherever the break happened to fall. The `[a-z0-9-]*[a-z0-9-]` requires at least one
# slug character before the break, which is what leaves the harmless shape alone: a wrap between the
# prefix and the slug keeps the slug whole and greppable, and `_DECISION_REF` still resolves it.
# See `test_no_decision_reference_is_split_across_a_line` for the whole argument, and
# `test_the_decision_wrap_detector_leaves_the_shape_that_still_resolves_alone` for the must-not-fire
# half.
_WRAPPED_DECISION = re.compile(r"`decision:[ \t]*[a-z0-9-]*[a-z0-9-](?=[^`\n]*$)", re.MULTILINE)


def _scan_subjects(roots: tuple[Path, ...], extra: tuple[Path, ...] = ()) -> list[Path]:
    """Every file under `roots` that may carry a reference, plus `extra`. An empty result is an
    error rather than an answer (#10) -- `_scan.py`'s `list_files` now (#288), shared with the two
    boundary guards; this file had re-derived the refusal a third time with no positive control of
    its own, see `test_the_guard_refuses_a_scan_it_could_not_make` below."""
    return list_files(roots, suffixes=SUBJECT_SUFFIXES, label="the narrative-reference guard",
                       extra=extra)


def subjects() -> list[Path]:
    """Every file the *resolution* check reads. See `RESOLUTION_ROOTS` for the roots,
    `RESOLUTION_EXEMPT_FILES` for the one file excluded from this list by identity (this guard's own
    module), and `VENDORED_RESOLUTION_EXEMPT_FILES` for the other reason a file is excluded here
    (vendored third-party code, not narrative at all)."""
    exempt = set(RESOLUTION_EXEMPT_FILES) | set(VENDORED_RESOLUTION_EXEMPT_FILES)
    return [p for p in _scan_subjects(RESOLUTION_ROOTS, EXTRA_SUBJECTS) if p.resolve() not in exempt]


def wrap_subjects() -> list[Path]:
    """Every file the *wrap* check reads — wider than `subjects()` on purpose. See `WRAP_ROOTS`."""
    return _scan_subjects(WRAP_ROOTS, EXTRA_SUBJECTS)


def _referrers_among(paths: Iterable[Path], exempt_roots: tuple[Path, ...]) -> list[Path]:
    """The subset of `paths` that may vouch for a decision record's reachability. Pure over its
    arguments so the selection rule is assertable against a fixture tree rather than only against
    this repository's own three records."""
    return [p for p in paths if not any(root in p.parents for root in exempt_roots)]


def referrer_subjects() -> list[Path]:
    """Every file that counts as *pointing at* a decision record -- `subjects()` minus the records
    themselves. See `REFERRER_EXEMPT_ROOTS` for why those are not the same list."""
    return _referrers_among(subjects(), REFERRER_EXEMPT_ROOTS)


def _orphan_slugs(declared: set[str], referrers: Iterable[Path]) -> list[str]:
    """The declared slugs nothing in `referrers` points at. Pure over its arguments for the same
    reason `_referrers_among` is."""
    referenced = {slug for path in referrers
                  for slug in _DECISION_REF.findall(path.read_text(encoding="utf-8"))}
    return sorted(declared - referenced)


# `requivo.testing` (#424) is the one place under `src/` that legitimately *defines* test methods
# rather than only citing them: `SessionRepositoryConformance` is a pytest mixin base class, shipped
# in the wheel on purpose, whose `def test_...` methods are collected only through a `tests/`-side
# subclass (`TestFileRepositoryConformance`, `TestInMemoryRepositoryConformance`) that never
# re-states them. Scoped to this one package rather than widening the scan to all of `SRC` -- nothing
# else under `src/` defines a test callable, and a narrower scan is less to reconsider the day
# something under `src/` genuinely does start looking like a reference instead of a definition.
TESTING_PACKAGE = SRC / "testing"


def declared_test_names() -> set[str]:
    """Every test callable the suite defines, plus every test module's stem.

    Both forms are in use and both are legitimate: `services/artifacts.py` names
    `tests/test_artifact_provenance.py`, a whole file, because the claim it makes is the file's
    subject rather than one assertion. Accepting only function names would fail that reference and
    teach the next person to delete the check instead of the reference.
    """
    names: set[str] = set()
    roots = (TESTS, TESTING_PACKAGE) if TESTING_PACKAGE.is_dir() else (TESTS,)
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            names.add(path.stem)
            for match in re.finditer(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)",
                                     path.read_text(encoding="utf-8"), re.MULTILINE):
                names.add(match.group(1))
    if not names:
        raise AssertionError(
            f"the narrative-reference guard found no tests under {TESTS} to resolve against — "
            f"every reference would 'resolve' against an empty set."
        )
    return names


def references(path: Path) -> set[str]:
    """Every test reference in one file, as a plain reader would see it -- including one inside the
    "Split out of `X.py`" historical-mention idiom. Resolution has its own narrower view; see
    `resolvable_references()`."""
    return set(_REFERENCE.findall(path.read_text(encoding="utf-8")))


def resolvable_references(path: Path) -> set[str]:
    """Every reference this file makes that the *resolution* check actually holds it to --
    `references()` minus the name captured inside a recognized historical-mention idiom.

    Blanking the matched span (rather than removing it, or discarding the whole match's name
    wherever it occurs) keeps every other offset in the string stable and, more importantly, keeps
    the exemption scoped to that one occurrence: a file that names the same dead test both inside the
    idiom and again as an ordinary pointer still has to answer for the second one. See
    `test_the_same_dangling_name_outside_the_idiom_still_dangles`.
    """
    text = path.read_text(encoding="utf-8")
    stripped = _HISTORICAL_MENTION.sub(
        lambda m: m.group(0).replace(m.group(1), "x" * len(m.group(1))), text
    )
    return set(_REFERENCE.findall(stripped))


def test_the_guard_reads_the_real_tree():
    """Name what was scanned, because everything below is a negative assertion."""
    files = subjects()
    assert len(files) > 20, f"only {len(files)} subject files — the scan is not seeing the package"
    assert any(p.name == "contracts.py" for p in files)
    # `scripts/` was outside the scan for as long as this guard existed, and it was never empty:
    # `plugin_cli_drift.py` named two tests and `golden_lib.py` named a third, all unchecked. The
    # convention is repository-wide — a maintainer greps a name out of a script exactly as they grep
    # one out of a module — so a root that carries references and is not scanned is the guard
    # reporting a clean tree it did not read (#137).
    assert any(p.parent.name == "scripts" for p in files), (
        "scripts/ is not in the scan, and it carries references — this guard would report them clean"
    )
    assert any(p.name == "CLAUDE.md" for p in files)
    # `docs/` joined with #156, the third place CLAUDE.md names as narrative's right home.
    assert any(p.suffix == ".md" and "docs" in p.parts for p in files), (
        "docs/ is not in the resolution scan, and CLAUDE.md names it as narrative's right home"
    )
    assert not any(p.name == "CHANGELOG.md" for p in files), (
        "CHANGELOG.md is released history — its dead pointers are correct, not stale, and it must "
        "never be swept"
    )
    assert len(declared_test_names()) > 100


def test_the_guard_refuses_a_scan_it_could_not_make(tmp_path):
    """#288's own positive control: this guard had none before. Exercised through `_scan_subjects`
    directly, since `subjects()`/`wrap_subjects()` hard-code this file's real roots."""
    missing = tmp_path / "not-here"
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "irrelevant.txt").touch()                       # no subject suffix lives here
    for root in (missing, empty):
        with pytest.raises(AssertionError, match="could not look"):
            _scan_subjects((root,))


def test_the_wrap_scan_still_reaches_one_file_further_than_the_resolution_scan():
    """#190's decision, pinned rather than left as something only the comments above `RESOLUTION_ROOTS`
    and `WRAP_ROOTS` state. `tests/` is now in *both* roots -- `busy_harness.js`, the motivating
    instance for #156, resolves cleanly today and belongs in the resolution scan exactly like any
    other file, not exempted by directory or suffix. The gap between the two scans is this guard's
    own module (`RESOLUTION_EXEMPT_FILES`, excluded by identity -- see
    `test_this_guards_own_file_is_wrap_checked_but_not_resolution_checked`) plus, since #504, the one
    vendored bundle named in `VENDORED_RESOLUTION_EXEMPT_FILES`."""
    resolution_files = set(subjects())
    wrap_files = set(wrap_subjects())
    assert resolution_files < wrap_files, "the wrap scan must be a strict superset of the resolution scan"
    expected_gap = set(RESOLUTION_EXEMPT_FILES) | set(VENDORED_RESOLUTION_EXEMPT_FILES)
    assert wrap_files - resolution_files == expected_gap, (
        "the only files the wrap scan reaches and the resolution scan does not should be this guard's "
        "own module and the vendored bundle named above -- anything else means a root or an "
        "exemption drifted from what the comments claim"
    )
    assert any(p.name == "busy_harness.js" for p in wrap_files), (
        "tests/web/busy_harness.js is not in the wrap scan — the motivating instance for #156 would "
        "still be invisible"
    )
    assert any(p.name == "busy_harness.js" for p in resolution_files), (
        "tests/web/busy_harness.js dropped out of the resolution scan — #190 widened RESOLUTION_ROOTS "
        "to cover tests/*.js exactly like every other suffix, and this file resolves cleanly today"
    )
    assert not any(p.name == "CHANGELOG.md" for p in wrap_files), (
        "CHANGELOG.md must never be swept, wrap check included"
    )


def test_a_vendored_bundles_coincidental_identifier_is_not_a_dangling_reference():
    """`swagger-ui-bundle.js` (#504) bundles real library functions named `test_cookie_name` and
    `test_cookie_value` -- coincidentally shaped exactly like this guard's own reference pattern, and
    not a claim about this suite. Two must-fire halves: the raw file really does contain the
    collision (so this test is not proving an exemption for a problem that no longer exists), and the
    exemption keeps it out of the *resolution* scan specifically, not out of scanning altogether."""
    vendored = VENDORED_RESOLUTION_EXEMPT_FILES[0]
    raw = vendored.read_text(encoding="utf-8")
    assert "test_cookie_name" in raw and "test_cookie_value" in raw, (
        "the vendored file no longer contains the collision this exemption exists for -- if it was "
        "re-vendored at a version that dropped these helpers, VENDORED_RESOLUTION_EXEMPT_FILES should "
        "shrink back to empty rather than carry a stale entry"
    )
    resolution_files = {p.resolve() for p in subjects()}
    assert vendored not in resolution_files, "the vendored bundle must not be in the resolution scan"
    wrap_files = {p.resolve() for p in wrap_subjects()}
    assert vendored in wrap_files, (
        "the vendored bundle dropped out of the wrap scan too -- the exemption is about resolution "
        "only, never about no longer looking at this file at all"
    )


def test_every_named_test_reference_resolves():
    """A reference that names nothing spends a reader's trust and returns nothing.

    The failure names the file and the dead reference, because "some reference is broken" costs the
    next person the same search this test just did.
    """
    known = declared_test_names()
    dangling = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()} -> {name}"
        for path in subjects()
        for name in resolvable_references(path)
        if name not in known
    )
    assert not dangling, (
        "these references name a test that does not exist:\n  " + "\n  ".join(dangling) +
        "\nEither the test was renamed (update the reference) or it was deleted (delete the "
        "reference, and ask what is guarding the line it was attached to)."
    )


def test_no_reference_is_split_across_a_line():
    """The only way one of these is used is: select it, grep it. A name broken by a wrap is not in
    the repository as a string, so it fails at exactly the moment it is needed — and it fails
    silently, because the test it names really does exist.

    Two of sixteen were in that state when this was written (`contracts.py`,
    `deterministic/doctor.py`). Reflow the comment; never hyphenate or wrap an identifier.
    """
    split = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()}:{path.read_text(encoding='utf-8')[:m.start()].count(chr(10)) + 1} "
        f"-> {m.group(0)}…"
        for path in wrap_subjects()
        for m in _WRAPPED.finditer(path.read_text(encoding="utf-8"))
    )
    assert not split, (
        "these references are split by a line wrap and cannot be found by grep:\n  " +
        "\n  ".join(split) + "\nReflow the surrounding text so the identifier is on one line."
    )


def declared_slugs() -> set[str]:
    """Every slug a decision record declares, read from its `**Slug:**` line rather than from its
    filename — the filename carries an ordering number that is deliberately not the reference."""
    slugs: set[str] = set()
    for path in sorted(DECISIONS.glob("*.md")):
        if path.name == "README.md":
            continue
        m = re.search(r"^\*\*Slug:\*\*\s*`([a-z0-9-]+)`", path.read_text(encoding="utf-8"), re.MULTILINE)
        if not m:
            raise AssertionError(
                f"{path.relative_to(REPO_ROOT).as_posix()} declares no `**Slug:**` line. A record "
                f"nothing can reference by name is a record only its path can reach, which is the "
                f"one thing docs/decisions/README.md says not to build."
            )
        slugs.add(m.group(1))
    return slugs


def test_every_decision_reference_resolves_to_a_record():
    """The slug half of the same rule. A `decision:` reference naming no record is the dangling
    pointer above wearing a different prefix, and the second convention must not ship unguarded the
    way the first one did — sixteen references, two of them broken, and nothing looking."""
    if not DECISIONS.is_dir():
        pytest.skip("no docs/decisions/ yet — nothing declares a slug, so nothing can dangle")
    known = declared_slugs()
    dangling = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()} -> {slug}"
        for path in subjects()
        for slug in _DECISION_REF.findall(path.read_text(encoding="utf-8"))
        if slug not in known
    )
    assert not dangling, (
        "these `decision:` references name no record:\n  " + "\n  ".join(dangling) +
        f"\nDeclared slugs: {sorted(known)}"
    )


def test_the_decision_records_are_reachable_from_somewhere():
    """A record nothing points at is a document nobody opens, which is the failure mode the whole
    rule was written to avoid. Not a hard requirement of the convention and it is checked anyway,
    because an unreferenced record is a signal that a narrative was moved out of the code and the
    pointer was never left behind.

    The referrer set is `referrer_subjects()`, never `subjects()` -- a record is not a referrer for
    itself or for its siblings (#384). See `REFERRER_EXEMPT_ROOTS` for what that changes and why.
    """
    if not DECISIONS.is_dir():
        pytest.skip("no docs/decisions/ yet")
    referrers = list(referrer_subjects())
    # `.github/` is outside `src/`, and it is where the first record's referrer lives.
    referrers.extend(sorted((REPO_ROOT / ".github").rglob("*.yml")))
    orphans = _orphan_slugs(declared_slugs(), referrers)
    assert not orphans, (
        f"these records are referenced from nowhere a reader enters through: {orphans}. Leave a "
        f"`decision: <slug>` line at whatever the record explains -- in CLAUDE.md, in docs/, or at "
        f"the call site -- or the move traded a paragraph for a file nobody opens. A pointer from "
        f"another decision record does not count: see REFERRER_EXEMPT_ROOTS."
    )


def test_no_decision_reference_is_split_across_a_line():
    """The slug half of the wrap rule, and #384's second open question answered with the mechanism
    rather than by analogy.

    A `decision:` reference can break in two places and they are not the same case:

    * **inside the slug** -- the slug is then not in the repository as a string, exactly like a
      wrapped test name, and it is *worse*: `_DECISION_REF` requires the closing backtick after the
      slug and `[a-z0-9-]+` cannot cross a newline, so the reference matches nothing at all.
      `test_every_decision_reference_resolves_to_a_record` never sees it, and the record it was
      meant to point at silently loses a pointer. This is what `_WRAPPED_DECISION` catches.
    * **after the prefix** -- the pattern's whitespace class does span a newline, so the reference
      still resolves and the slug is still on one line, still greppable. Deliberately left alone;
      that shape is what #384 had in mind when it weighed declining this check entirely.

    Detected by the missing closing backtick rather than by a trailing hyphen: an inline code span
    that opens on a line and does not close on it is broken wherever the break fell, which is a
    stronger signal than `_WRAPPED` can get from a bare identifier.
    """
    split = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()}:"
        f"{path.read_text(encoding='utf-8')[:m.start()].count(chr(10)) + 1} -> {m.group(0)}…"
        for path in wrap_subjects()
        for m in _WRAPPED_DECISION.finditer(path.read_text(encoding="utf-8"))
    )
    assert not split, (
        "these `decision:` references are split by a line wrap, so the slug cannot be found by "
        "grep and the reference resolves to nothing at all:\n  " + "\n  ".join(split) +
        "\nReflow the surrounding text so the slug is on one line."
    )


# ── controls: a guard that cannot fail is not a guard ──────────────────────────────────────────

@pytest.mark.parametrize("source, expected", [
    ("# see `test_the_persisted_contract_is_permissive_all_the_way_down` for why",
     {"test_the_persisted_contract_is_permissive_all_the_way_down"}),
    ("`tests/test_artifact_provenance.py` asserts it", {"test_artifact_provenance"}),
    ("def test_x(): pass", set()),                       # too short to be a reference
    ("no reference here at all", set()),
])
def test_the_extractor_sees_a_reference_and_only_a_reference(tmp_path, source, expected):
    p = tmp_path / "sample.py"
    p.write_text(source, encoding="utf-8")
    assert references(p) == expected


@pytest.mark.parametrize("source, blanked_name", [
    ("Split out of `test_cli_deterministic.py` by #141; the shared harness is elsewhere.",
     "test_cli_deterministic"),
    ("Split out of `test_engine_wide_reasoning_paths.py` (#72). One file rather than two.",
     "test_engine_wide_reasoning_paths"),
])
def test_the_historical_mention_idiom_is_excluded_from_resolvable_references(tmp_path, source, blanked_name):
    """The must-not-fire half of #190's rule: a name that appears only inside the recognized
    "Split out of `X.py`" idiom -- in either citation style actually used in this repository -- is a
    historical mention, not a pointer, and must not reach `resolvable_references()`. `references()`
    still sees it, because a plain reader does too; only resolution is meant to look away."""
    p = tmp_path / "sample.py"
    p.write_text(source, encoding="utf-8")
    assert blanked_name in references(p)
    assert blanked_name not in resolvable_references(p)


def test_a_similarly_shaped_mention_that_is_not_the_idiom_still_dangles(tmp_path):
    """The must-fire complement: the exemption is the exact phrase, not "any `.py`-suffixed name in a
    sentence about the past". A rename that does not use the recognized idiom is exactly as loud as
    any other broken pointer -- which is the point, since a marker nobody is asked to use cannot be
    relied on to appear tomorrow."""
    p = tmp_path / "sample.py"
    p.write_text(
        "Renamed from `test_cli_deterministic_and_then_some.py`, which used to hold this.\n",
        encoding="utf-8",
    )
    assert "test_cli_deterministic_and_then_some" in resolvable_references(p)


def test_the_same_dangling_name_outside_the_idiom_still_dangles(tmp_path):
    """The exemption blanks the matched span, not every occurrence of the captured name in the file.
    A file that both recounts a split *and* separately points at the same dead name the ordinary way
    still has to answer for the second one."""
    p = tmp_path / "sample.py"
    p.write_text(
        "Split out of `test_cli_deterministic_once_more.py` by #141.\n"
        "See `test_cli_deterministic_once_more` for the original discussion.\n",
        encoding="utf-8",
    )
    assert "test_cli_deterministic_once_more" in resolvable_references(p)


def test_this_guards_own_file_is_wrap_checked_but_not_resolution_checked():
    """The other half of #190's rule, and the one with no parsing in it at all: this module is
    excluded from `subjects()` by identity, not by pattern, because it necessarily contains
    broken-looking references by design and cannot be a trustworthy resolver of its own examples. It
    stays in `wrap_subjects()`, which has no such hazard."""
    here = Path(__file__).resolve()
    assert here in RESOLUTION_EXEMPT_FILES
    assert here not in {p.resolve() for p in subjects()}
    assert here in {p.resolve() for p in wrap_subjects()}


def test_the_wrap_detector_sees_the_shape_it_was_written_for(tmp_path):
    """The positive control, and the one that matters: this guard exists because two real references
    were in exactly this shape and every other check in the repository was blind to them."""
    p = tmp_path / "wrapped.py"
    p.write_text("# … the defect class #14 exists to remove. `test_the_persisted_mirror_copies_every_\n"
                 "# constraint_it_restates` pins the general property.\n", encoding="utf-8")
    assert _WRAPPED.search(p.read_text(encoding="utf-8"))


def test_the_wrap_detector_does_not_fire_on_an_intact_reference(tmp_path):
    """The must-not-fire half. A reference that ends a line *complete* is fine — what is forbidden is
    an identifier continued on the next line, not one that happens to sit at the margin."""
    p = tmp_path / "intact.py"
    p.write_text("# pinned by `test_the_persisted_mirror_copies_every_constraint_it_restates`\n"
                 "# which is why it cannot drift.\n", encoding="utf-8")
    assert not _WRAPPED.search(p.read_text(encoding="utf-8"))


def _record(path: Path, slug: str, body: str) -> None:
    """One decision record, in the only two respects this guard reads it: the `**Slug:**` line
    `declared_slugs()` parses, and whatever prose the reference patterns then run over."""
    path.write_text(f"**Slug:** `{slug}`\n\n{body}\n", encoding="utf-8")


def test_a_record_that_only_quotes_its_own_slug_is_an_orphan(tmp_path):
    """#384's motivating instance, as a fixture rather than as a fact about one commit.

    A record that explains where its pointer belongs has to quote its own slug to say so, which
    makes this the *normal* shape of a record rather than an edge case — and while the records were
    inside their own referrer set it meant the orphan check could not fire for the whole class.
    """
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    record = decisions / "0002-only-itself.md"
    _record(record, "only-itself",
            "A `decision: only-itself` pointer belongs in CLAUDE.md's Extending section.")
    entry = tmp_path / "CLAUDE.md"
    entry.write_text("The Extending section, with no pointer in it.\n", encoding="utf-8")

    referrers = _referrers_among([record, entry], (decisions,))
    assert _orphan_slugs({"only-itself"}, referrers) == ["only-itself"]

    # The must-not-fire half, on the same fixture: the guard is not simply always red. One pointer
    # from a file a reader enters through is all it ever wanted.
    entry.write_text("Kept by hand: `decision: only-itself` says why.\n", encoding="utf-8")
    assert _orphan_slugs({"only-itself"}, referrers) == []


def test_a_record_reachable_only_from_a_sibling_record_is_an_orphan(tmp_path):
    """Why the whole directory comes out of the referrer set and not only self-reference (#384).

    Excluding self alone still passes a cluster of records that cite each other and nothing outside,
    and that unreachable island is exactly the failure the guard is named for. Both records here
    point at a real, declared sibling, so every reference resolves; what none of them has is a
    reader who could arrive.
    """
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    first, second = decisions / "0001-a.md", decisions / "0002-b.md"
    _record(first, "slug-a", "Superseded by `decision: slug-b`.")
    _record(second, "slug-b", "Supersedes `decision: slug-a`.")
    entry = tmp_path / "CLAUDE.md"
    entry.write_text("No pointer at either of them.\n", encoding="utf-8")

    referrers = _referrers_among([first, second, entry], (decisions,))
    assert _orphan_slugs({"slug-a", "slug-b"}, referrers) == ["slug-a", "slug-b"]


def test_the_records_are_resolution_checked_but_are_not_their_own_referrers():
    """Two scans over one directory, on the real tree. A record's own `decision:` and test-name
    references still have to resolve — it stays a *subject* — and it still cannot vouch for anybody's
    reachability, its own included. The assertion that there are records at all is the positive
    control: with an empty `docs/decisions/` the loop below would pass having checked nothing."""
    records = sorted(p for p in DECISIONS.glob("*.md") if p.name != "README.md")
    assert records, "no decision records — the exclusion asserted below would be vacuous"
    subject_paths = {p.resolve() for p in subjects()}
    referrer_paths = {p.resolve() for p in referrer_subjects()}
    for record in records:
        assert record.resolve() in subject_paths, (
            f"{record.name} dropped out of the resolution scan — its own references would stop "
            f"being checked, which is not what #384 asked for"
        )
        assert record.resolve() not in referrer_paths, (
            f"{record.name} is still counted as a referrer — see REFERRER_EXEMPT_ROOTS"
        )
    assert referrer_paths < subject_paths


def test_the_decision_wrap_detector_sees_a_slug_split_across_the_break(tmp_path):
    """The positive control for `_WRAPPED_DECISION`, and the measurement behind #384's second
    question. The reference below is invisible to `_DECISION_REF` — the closing backtick is on the
    far side of the newline and `[a-z0-9-]+` cannot cross one — so nothing else in this module would
    have said a word about it."""
    p = tmp_path / "wrapped.md"
    p.write_text("kept by hand: see `decision: elicitation-schema-\nhand-kept` for the measurement\n",
                 encoding="utf-8")
    text = p.read_text(encoding="utf-8")
    assert _WRAPPED_DECISION.search(text)
    assert _DECISION_REF.findall(text) == [], (
        "if this ever finds the slug, the resolution check covers the case and the wrap detector "
        "should be weighed again rather than kept out of habit"
    )


def test_the_decision_wrap_detector_leaves_the_shape_that_still_resolves_alone(tmp_path):
    """The must-not-fire half, and the half #384 was right about. A break *before* the slug keeps
    the slug whole on one line, so the only thing anybody greps is still there and `_DECISION_REF`
    still resolves the reference. An intact one-line reference must not fire either."""
    wrapped_prefix = tmp_path / "prefix.md"
    wrapped_prefix.write_text("see `decision:\nelicitation-schema-hand-kept` for why\n", encoding="utf-8")
    text = wrapped_prefix.read_text(encoding="utf-8")
    assert not _WRAPPED_DECISION.search(text)
    assert _DECISION_REF.findall(text) == ["elicitation-schema-hand-kept"]

    intact = tmp_path / "intact.md"
    intact.write_text("see `decision: elicitation-schema-hand-kept` for why\n", encoding="utf-8")
    assert not _WRAPPED_DECISION.search(intact.read_text(encoding="utf-8"))
