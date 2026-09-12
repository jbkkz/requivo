"""Text IO encoding guards -- every text read declares its codec, every print survives its console.

Why this file exists (#11, #29)
-------------------------------
`_atomic_write` has always written `encoding="utf-8"`. Almost nothing else named a codec: **29 call
sites** across `src/` and `scripts/` -- 28 reads and one write (`scripts/golden_lib.py`, so the
asymmetry was never quite total) -- used the `Path` text methods with no `encoding`, which fall back
to `locale.getpreferredencoding(False)`: UTF-8 on macOS and on Linux CI, the ANSI codepage (cp1252)
on Windows unless `PYTHONUTF8=1`. `pyproject.toml` is `requires-python = ">=3.9"`, so UTF-8
mode-by-default (new in 3.15) covers none of the supported range.

That asymmetry has three consequences and all of them are quiet:

  - a French `problem` slot round-trips into mojibake that is still valid JSON, so nothing fails and
    the PRD ships it;
  - `integrity.py` rehashes the mis-decoded string and reports `revision_hash_mismatch` -- the verb
    whose whole job is answering *is this session intact* accuses the user of editing a file nobody
    touched, and `session import` refuses a legitimate archive on it;
  - 20 of the bundled assets are not pure ASCII, so on an ASCII locale `requivo discover`, `demo`,
    `schema` and `context` raise `UnicodeDecodeError` before any API call is made. Measured, because
    the issue body had this backwards: only **2** of those 20 are undecodable as *cp1252*. The other
    18 decode successfully into mojibake, so on Windows the usual outcome was never a crash -- it was
    a prompt assembled from corrupted product context, shipped to the model and billed.

The same class runs the other way on output (#29): a glyph the console cannot *encode* raises
`UnicodeEncodeError` at the `print` -- after the mutation that print was reporting has already
landed. The operator sees a crash, re-runs, and pays for a second provider call on top of the first.

The three guards here
---------------------
1. `test_every_text_read_declares_its_encoding` -- a static AST walk over `src/` and `scripts/`.
   This is the part that removes the *class* rather than the 29 instances: passing `encoding=` at 29
   call sites leaves the 30th, written next week, unguarded. This repo has already watched that
   happen twice -- a bare `read_text()` appeared three lines from #33's fix in a neighbouring
   module, and the guard written for #10 had the identical defect *in the guard itself*.
2. `test_the_cli_survives_a_console_that_cannot_encode_its_glyphs` -- a subprocess under
   `PYTHONIOENCODING=ascii`. #29 observed that this suite captures to `io.StringIO`, so no
   in-process test can ever reach the console encoder even once a Windows leg exists. A subprocess
   with a forced narrow encoder reaches the real one, and it does so identically on every platform,
   so this control fires on the Linux legs too rather than waiting for Windows.
3. `test_the_cli_reads_its_assets_with_an_explicit_encoding` -- a subprocess under
   `PYTHONWARNDEFAULTENCODING=1` with `-W error::EncodingWarning`, which turns every locale-default
   text read into an exception identically on every platform and in every locale. 3.10+ only; on 3.9
   it skips loudly, naming what went untested, rather than passing.

Every runtime control is paired with a control on the *lever*, because "the process did not crash"
also passes when the environment variable did nothing at all. A control that cannot fire is worse
than no control, so where a lever does not bite the test names the claim that went unmade.

What these guards cannot see
----------------------------
Stated rather than left to read as clean:

  - an aliased or dynamically built call -- `getattr(path, "read_text")()`, `f = p.read_text; f()`;
  - a third-party library reading text with the locale codec inside itself;
  - a console that *encodes* a glyph and then draws it as something else. Encoding is all this can
    reach; what the terminal renders afterwards is the terminal's business.
"""
from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from _scan import list_python_files, parse_utf8, write_tree

from requivo import cli, cli_support, streams
from requivo.core.errors import InvalidModelError
from requivo.deterministic import read_user_text

REPO_ROOT = Path(__file__).resolve().parents[1]

# Both trees CI already lints (`ruff check src tests scripts`). `scripts/` is in scope because the
# golden harness reads provider-written prose out of `fixtures/golden/*.runs.json` -- em dashes and
# curly quotes by construction -- and mis-decoding a baseline reports a prompt regression that never
# happened, which is the one failure mode a regression lens must not have.
SCAN_ROOTS = {
    "src/requivo": "the package",
    "scripts": "the golden harness and the install-free launcher",
}

# One stable anchor per root, so adding a module does not fail this file while a rename still does.
# `Path.rglob` on a directory that does not exist returns `[]`, and `assert not []` is an all-clear
# nobody earned -- the failure #10 exists in order not to repeat.
SCAN_ANCHORS = {
    "src/requivo": ("core/persistence/store.py", "deterministic/__init__.py"),
    "scripts": ("golden_lib.py",),
}

# The methods whose default codec is the *locale's*, not the file's.
_TEXT_METHODS = {
    "read_text": "Path.read_text() with no encoding decodes with the locale codepage, not the file's",
    "write_text": "Path.write_text() with no encoding encodes with the locale codepage, not UTF-8",
    "open": "Path.open() in text mode with no encoding uses the locale codepage in both directions",
}

# Receivers whose `.open()` has no text layer at all, so demanding an `encoding` of them would fail
# correct code. Each carries its reason, so the next person argues with a named line rather than
# deleting the table.
_EXEMPT_RECEIVERS = {
    "os": "os.open returns a raw file descriptor -- there is no text layer and no codec to declare",
    "webbrowser": "webbrowser.open takes a URL and opens a browser, not a file",
    "zipfile": "zipfile members are opened as bytes unless wrapped explicitly",
    "tarfile": "tarfile members are opened as bytes unless wrapped explicitly",
    "shutil": "shutil has no text-mode open",
    "subprocess": "subprocess streams take their codec from the Popen call, not from open()",
}


_parse = parse_utf8  # was its own duplicate of test_boundaries.py's copy; see _scan.py (#288)


def scan(root: Path) -> list[Path]:
    """Every Python file under `root`, recursively. An empty result is an error, not an answer --
    `_scan.py`'s `list_python_files` now, shared with `test_boundaries.py` (#288)."""
    return list_python_files(root, label="the encoding guard")


def _has_keyword(node: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in node.keywords)


def _mode_of(node: ast.Call, positional_index: int) -> tuple[str | None, bool]:
    """The literal `mode` string of an open() call, and whether it could be determined at all.

    Returns `(mode, known)`. A mode assembled at runtime is `(None, False)` -- the third state, which
    the caller reports as a finding rather than waving through, because a guard that cannot tell text
    from binary must not answer as though it could.
    """
    for kw in node.keywords:
        if kw.arg == "mode":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value, True
            return None, False
    if len(node.args) > positional_index:
        arg = node.args[positional_index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value, True
        return None, False
    return "r", True  # the default, and it is text


_UNKNOWN_MODE = (
    "whose mode is not a literal -- this guard cannot tell text from binary here, so pass "
    "encoding= explicitly or read bytes"
)


def encoding_violations(path: Path) -> list[str]:
    """Every text read or write in `path` that takes whatever codec the locale happens to offer.

    One string per violation, carrying the line, the construct and the reason. A guard that says only
    "False" costs the next reader the whole search again.
    """
    out: list = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        # The bare builtin: `open(p)`. An attribute call is handled below.
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            if _has_keyword(node, "encoding"):
                continue
            mode, known = _mode_of(node, positional_index=1)
            if not known:
                out.append(f"line {node.lineno}: open() {_UNKNOWN_MODE}")
            elif "b" not in mode:
                out.append(f"line {node.lineno}: open() in text mode -- {_TEXT_METHODS['open']}")
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _TEXT_METHODS:
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in _EXEMPT_RECEIVERS:
            continue
        if _has_keyword(node, "encoding"):
            continue
        if node.func.attr == "open":
            mode, known = _mode_of(node, positional_index=0)
            if known and "b" in mode:
                continue
            if not known:
                out.append(f"line {node.lineno}: .open() {_UNKNOWN_MODE}")
                continue
        out.append(f"line {node.lineno}: .{node.func.attr}() -- {_TEXT_METHODS[node.func.attr]}")
    return sorted(out)


def _nonascii_literals(node: ast.AST) -> list:
    """Every string constant reachable from `node` that is not pure ASCII."""
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and any(ord(c) > 127 for c in n.value)]


def fixture_violations(path: Path) -> list:
    """Encoding-less text IO in a *test* whose content is not ASCII.

    A narrower rule than `encoding_violations`, applied to `tests/` for a stated reason. Most of the
    ~90 encoding-less reads and writes in this suite move pure ASCII between a fixture and an
    assertion in the same process: the locale's codec is used on both sides, so they agree, and
    demanding `encoding=` of all of them would be a diff far larger than the bug with no defect
    behind it.

    The ones that matter are the fixtures carrying a character outside ASCII, because those cross a
    real boundary: the *test* writes with the locale's codec and the *product* now reads UTF-8. On
    Windows those disagree, and the leg goes red about a product that is behaving correctly. That is
    the trap #3 names -- a harness rendering an environment limit as a product verdict -- and it is
    worth catching here, where it costs a second, rather than one multi-leg CI matrix at a time.

    Found exactly one instance when this was written (`test_selection.py`, a context card with an em
    dash, read back through `load_context`), which is the evidence that the narrow rule is worth
    having and the broad one is not yet.
    """
    out: list = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in ("read_text", "write_text"):
            continue
        if _has_keyword(node, "encoding"):
            continue
        found = [s for arg in node.args for s in _nonascii_literals(arg)]
        if found:
            out.append(
                f"line {node.lineno}: .{node.func.attr}() with non-ASCII content and no encoding -- "
                f"the fixture is written with the locale's codec and read back by a product that "
                f"decodes UTF-8; they disagree on Windows. Content: {found[0][:60]!r}"
            )
    return sorted(out)


# Two sites read with the locale's default *on purpose*, and both would be destroyed by "fix" here:
# they exist to measure what the default does. Keyed by enclosing function rather than line number,
# which drifts, and each carries its reason so the next person argues with a line.
# Keyed by path relative to `tests/`, not by basename: `tests/` already has a `web/` subdirectory, so
# a future `tests/web/test_boundaries.py` would silently inherit this file's exemptions and any bare
# read inside a function that happened to share a name would stop being reported. An exemption that
# covers a file it was never written for is the silent-absence class wearing an allowlist.
_LOCALE_DEFAULT_BY_DESIGN = {
    "test_boundaries.py": {
        "_force_default_encoding":
            "probes whether the ambient default could be forced at all; passing encoding= here would "
            "make the probe measure nothing and the control it gates would silently stop firing",
        "test_the_guard_reads_source_as_utf8":
            "demonstrates the pre-#10 failure by performing it -- the bare read IS the thing under "
            "test, and it is asserted to raise",
    },
    "test_persistence_guards.py": {
        "test_an_artifact_round_trips_non_ascii_content":
            "same shape as the two above, and the one this table was written a commit too late for: "
            "the bare read performs the defect under a forced ASCII default so the `raises` can catch "
            "it. A mechanical sweep gave it an explicit encoding, which cannot raise -- invisible on "
            "3.10+, where the force does not take and the test skips, and a failing 3.9 leg",
    },
}


def _enclosing_function_names(tree: ast.Module) -> dict:
    """Map each AST node's id() to the name of the function it sits in. Outermost wins, which is what
    an exemption keyed by test name wants."""
    out: dict = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                out.setdefault(id(node), fn.name)
    return out


def read_violations_in_test(path: Path) -> list:
    """Encoding-less *reads* in a test, minus the ones that are deliberately measuring the default.

    Reads and writes are treated asymmetrically in `tests/`, and the asymmetry is the finding rather
    than a compromise. For a **write**, the hazard is the content, and the content is a literal in the
    source -- so `fixture_violations` below can see exactly which writes are dangerous, and demanding
    `encoding=` of the other ~53 would be a large diff with no defect behind it.

    For a **read**, the hazard is in the file being read, which the source never mentions. Nothing
    static can tell a test reading an ASCII fixture it just wrote from a test reading a bundled asset
    full of em dashes. That is not hypothetical: the first Windows leg this branch adds went red on
    `test_demo_payload_matches_the_browsable_example`, which compares the bundled demo payload against
    the browsable copy with two bare `read_text()` calls -- `UnicodeDecodeError: 'charmap' codec can't
    decode byte 0x90`. The product read those files correctly; only the test did not. The narrow
    write-side rule could not have seen it, because there was no literal to look at.

    So every read declares its codec, and the two that must not are named above.
    """
    tree = _parse(path)
    try:
        key = path.relative_to(REPO_ROOT / "tests").as_posix()
    except ValueError:
        key = ""  # a fixture tree under tmp_path: no exemption can apply to it, which is correct
    exempt = _LOCALE_DEFAULT_BY_DESIGN.get(key, {})
    enclosing = _enclosing_function_names(tree)
    out: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "read_text":
            continue
        if _has_keyword(node, "encoding"):
            continue
        if enclosing.get(id(node)) in exempt:
            continue
        out.append(
            f"line {node.lineno}: .read_text() with no encoding -- the file being read may hold "
            f"characters the locale's codec cannot decode, and unlike a write there is no literal "
            f"here for a narrower rule to inspect"
        )
    return sorted(out)


def test_every_read_in_the_suite_declares_its_encoding():
    """The gap the first Windows leg found. The guard walked `src/` and `scripts/`; the 30th site
    appeared in the one directory it did not walk."""
    offenders: dict = {}
    for path in scan(REPO_ROOT / "tests"):
        found = read_violations_in_test(path)
        if found:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        "a test that reads text without naming its codec decodes with whatever the platform offers, "
        "so it passes on Linux and fails on Windows about a file the product reads correctly: "
        + repr(offenders)
    )


def test_the_by_design_exemptions_still_exist():
    """An exemption for a function that has been renamed or deleted is an exemption silently covering
    nothing -- or, worse, still suppressing a real finding under a name somebody reused. Pin them."""
    for filename, functions in _LOCALE_DEFAULT_BY_DESIGN.items():
        path = REPO_ROOT / "tests" / filename
        assert path.is_file(), f"{filename} is exempted from the read rule and does not exist"
        defined = {n.name for n in ast.walk(_parse(path))
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = sorted(set(functions) - defined)
        assert not missing, (
            f"{filename} exempts {missing}, which it no longer defines. An exemption naming nothing "
            f"is either dead or about to cover the wrong function."
        )
        # The file is clean *with* the exemption applied...
        assert read_violations_in_test(path) == [], "unexpected: exempted file has other bare reads"
        # ...and would not be without it. An exemption that suppresses nothing is dead weight, and
        # dead weight in an allowlist is how the next real finding gets covered by an entry nobody
        # re-read. Checked by removing the table rather than by trusting the comment beside it.
        saved = globals()["_LOCALE_DEFAULT_BY_DESIGN"]
        globals()["_LOCALE_DEFAULT_BY_DESIGN"] = {}
        try:
            without = read_violations_in_test(path)
        finally:
            globals()["_LOCALE_DEFAULT_BY_DESIGN"] = saved
        assert len(without) == len(functions), (
            f"{filename} exempts {len(functions)} function(s) but only {len(without)} bare read(s) "
            f"exist without the table; an exemption suppressing nothing should be deleted: {without}")


def test_the_read_rule_fires_and_spares_correctly(tmp_path):
    """Both edges. A rule with an exemption table is the one that quietly stops firing."""
    root = tmp_path / "tests"
    _write_tree(root, {"test_x.py": """
        from pathlib import Path

        def test_bare(p: Path) -> str:
            return p.read_text()

        def test_explicit(p: Path) -> str:
            return p.read_text(encoding="utf-8")
    """})
    path = scan(root)[0]
    found = read_violations_in_test(path)
    assert len(found) == 1 and "line 5" in found[0], found


def test_no_test_fixture_writes_non_ascii_with_the_locale_codec():
    """The harness half of #3, kept honest by the same walk that keeps the product honest."""
    offenders: dict = {}
    for path in scan(REPO_ROOT / "tests"):
        found = fixture_violations(path)
        if found:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        "a test fixture carrying non-ASCII text must name its codec, or the Windows leg reddens "
        "about a product that is correct: " + repr(offenders)
    )


_FIXTURE_CASES = {
    "must_fire.py": """
        from pathlib import Path

        def fixture(root: Path) -> None:
            (root / "card.md").write_text("ACME \\u2014 the context")
    """,
    "must_not_fire_ascii.py": """
        from pathlib import Path

        def fixture(root: Path) -> None:
            (root / "card.md").write_text("ACME - the context")
    """,
    "must_not_fire_explicit.py": """
        from pathlib import Path

        def fixture(root: Path) -> None:
            (root / "card.md").write_text("ACME \\u2014 the context", encoding="utf-8")
    """,
}


def test_the_fixture_rule_fires_exactly_where_it_should(tmp_path):
    """Both edges of the narrow rule, because a rule this narrow is the easy one to get backwards."""
    root = tmp_path / "tests"
    _write_tree(root, _FIXTURE_CASES)
    verdicts = {p.name: bool(fixture_violations(p)) for p in scan(root)}
    assert verdicts == {
        "must_fire.py": True,
        "must_not_fire_ascii.py": False,
        "must_not_fire_explicit.py": False,
    }, verdicts


_write_tree = write_tree  # was its own duplicate of test_boundaries.py's copy; see _scan.py (#288)


# --------------------------------------------------------------------------------------------------
# The scan set itself: "could not look" must never render as "looked and found nothing".
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("relative", sorted(SCAN_ROOTS))
def test_the_guard_scans_the_real_trees(relative):
    """Name what was scanned. Everything below this line is a negative assertion, and a negative
    assertion over an empty set is an all-clear nobody earned."""
    root = REPO_ROOT / relative
    names = sorted(p.relative_to(root).as_posix() for p in scan(root))
    missing = [a for a in SCAN_ANCHORS[relative] if a not in names]
    assert not missing, (
        f"the encoding guard scanned {root} and did not find {missing}; it is not looking at "
        f"{SCAN_ROOTS[relative]}. Scanned: {names}"
    )


def test_the_guard_refuses_a_scan_it_could_not_make(tmp_path):
    """The #10 shape, in both of its forms: a path that resolves to nothing, and one that resolves
    to nothing useful. `rglob` raises on neither."""
    missing = tmp_path / "not-here"
    assert list(missing.rglob("*.py")) == [], "the shape guarded against: rglob returns [], not an error"
    with pytest.raises(AssertionError, match="no such directory"):
        scan(missing)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AssertionError, match="no Python files"):
        scan(empty)


# --------------------------------------------------------------------------------------------------
# The static guard, and controls on both of its edges.
# --------------------------------------------------------------------------------------------------

def test_every_text_read_declares_its_encoding():
    """#11 itself. Every text read and write in the shipped trees names its codec, so a session
    written as UTF-8 reads back as what was written, on every platform Requivo installs on."""
    offenders: dict = {}
    for relative in sorted(SCAN_ROOTS):
        for path in scan(REPO_ROOT / relative):
            found = encoding_violations(path)
            if found:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        "every text read and write must pass encoding= explicitly: the default is the *locale's* "
        "codec, so a file this project wrote as UTF-8 is decoded as cp1252 on Windows and the "
        "round-trip corrupts silently while still validating (#11). Offenders: " + repr(offenders)
    )


_ENCODING_VIOLATIONS = {
    "bare_read.py": """
        from pathlib import Path

        def load(p: Path) -> str:
            return p.read_text()
    """,
    "bare_write.py": """
        from pathlib import Path

        def save(p: Path, text: str) -> None:
            p.write_text(text)
    """,
    "bare_builtin_open.py": """
        def load(name):
            with open(name) as f:
                return f.read()
    """,
    "builtin_open_explicit_text_mode.py": """
        def load(name):
            with open(name, "r") as f:
                return f.read()
    """,
    "path_open_text.py": """
        from pathlib import Path

        def load(p: Path) -> str:
            with p.open() as f:
                return f.read()
    """,
    "open_with_computed_mode.py": """
        def load(name, mode):
            with open(name, mode) as f:
                return f.read()
    """,
    "chained_read.py": """
        import json
        from pathlib import Path

        def load(p: Path) -> dict:
            return json.loads((p / "session.json").read_text())
    """,
    "buried/deeper.py": """
        from pathlib import Path

        def load(p: Path) -> str:
            return p.read_text()
    """,
}


def test_the_guard_sees_each_way_of_writing_the_violation(tmp_path):
    """Positive control, one fixture per shape. "No offenders" also passes when the guard has gone
    blind, so every forbidden form gets a fixture the guard must flag -- including the buried one,
    which is what proves the walk is recursive rather than a flat glob."""
    root = tmp_path / "src"
    _write_tree(root, _ENCODING_VIOLATIONS)
    missed = [p.relative_to(root).as_posix() for p in scan(root) if not encoding_violations(p)]
    assert not missed, f"the encoding guard is blind to these: {missed}"


_LEGITIMATE_IO = """
    from __future__ import annotations

    import os
    import webbrowser
    from pathlib import Path


    def load(p: Path) -> str:
        return p.read_text(encoding="utf-8")


    def save(p: Path, text: str) -> None:
        p.write_text(text, encoding="utf-8")


    def load_bytes(p: Path) -> bytes:
        return p.read_bytes()


    def save_bytes(p: Path, blob: bytes) -> None:
        p.write_bytes(blob)


    def binary_stream(p: Path):
        return p.open("rb")


    def builtin_binary(name):
        return open(name, "rb")


    def builtin_text(name):
        return open(name, encoding="utf-8")


    def lock(d: Path) -> int:
        # a raw file descriptor: no text layer, so no codec to declare
        return os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)


    def show(url: str) -> None:
        webbrowser.open(url)
"""


def test_the_guard_does_not_fire_on_correct_io(tmp_path):
    """The must-fire cases above are only meaningful next to a must-not-fire case: a detector that
    flags everything is as useless as one that flags nothing, and this one gets deleted by the next
    person the first time it reddens correct code."""
    root = tmp_path / "src"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_IO})
    assert encoding_violations(scan(root)[0]) == []


# --------------------------------------------------------------------------------------------------
# The other half of the same call site: a keyword the floor does not have (#464, #469)
# --------------------------------------------------------------------------------------------------
#
# Every guard above asks whether a text call declares its codec. This one asks whether the keywords
# it declares them with *exist* on the oldest interpreter this project supports, because the fix for
# #464 reached for `write_text(..., newline="")` -- correct in intent, correct on the developer's
# 3.13, and a `TypeError` on every single write under the declared 3.9 floor. `_atomic_write` is
# called by every persistence path, so three CI legs went red with 518 failures apiece over one
# keyword.
#
# It lives here rather than in a new scanning tier because it is the same walk over the same two
# trees asking about the same three methods -- CLAUDE.md's rule is to extend an existing tier's scan
# rather than open a fourth (#288, #355).
#
# **The leg that should have caught this cannot, and that is measured rather than assumed.**
# `[tool.pyright]` sets `pythonVersion = "3.9"` for exactly this class, with a comment saying so --
# *a checker at 3.12 accepts what the oldest supported interpreter refuses*. Ran by hand against the
# offending line at `--pythonversion 3.9`: **0 errors**. Typeshed does not version-gate `newline` on
# `Path.write_text`, so the Types leg is structurally blind here and was green on the broken commit.
# A guard that duplicates a working checker would be waste; this one covers a hole in it.
_KEYWORDS_YOUNGER_THAN_THE_FLOOR = {
    ("write_text", "newline"): (3, 10),
    ("read_text", "newline"): (3, 13),
}


def _declared_floor() -> tuple:
    """The `requires-python` floor, read out of pyproject rather than restated here.

    A version written into this file would be a number in prose that nothing can falsify -- the
    failure mode CLAUDE.md names twice. Raising the floor must relax this guard on its own, in the
    same commit that raises it, with nobody remembering to come here."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^requires-python\s*=\s*"[><=~^ ]*(\d+)\.(\d+)', text, re.MULTILINE)
    assert m, (
        "could not read requires-python out of pyproject.toml -- this guard derives the floor from "
        "it, and a floor it cannot read is not a floor it may guess at")
    return (int(m.group(1)), int(m.group(2)))


def floor_violations(path: Path, floor: tuple) -> list:
    """Every text call in `path` passing a keyword the declared floor's interpreter does not accept.

    Attribute calls only, and by method name: the same deliberate blindness `encoding_violations`
    already states for an aliased or dynamically built call. What this reaches is the shape the
    defect actually took."""
    out: list = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in _EXEMPT_RECEIVERS:
            continue
        for kw in node.keywords:
            if kw.arg is None:
                continue
            added = _KEYWORDS_YOUNGER_THAN_THE_FLOOR.get((node.func.attr, kw.arg))
            if added is not None and added > floor:
                out.append(
                    f"line {node.lineno}: .{node.func.attr}({kw.arg}=...) -- that keyword reached "
                    f"{node.func.attr} in Python {added[0]}.{added[1]}, and this project's floor is "
                    f"{floor[0]}.{floor[1]}, where it is a TypeError on every call. Write it through "
                    f".open(..., {kw.arg}=...) instead, which has always taken it."
                )
    return sorted(out)


def test_no_text_call_passes_a_keyword_the_declared_floor_rejects():
    """#469. `_atomic_write` wrote `tmp.write_text(content, encoding="utf-8", newline="")`; the
    `newline=` keyword reached `Path.write_text` in 3.10 and `requires-python` is `>=3.9`, so the
    write raised `TypeError` on the three 3.9 legs -- 518 failures each, from one keyword on the one
    function every persistence path goes through."""
    floor = _declared_floor()
    offenders: dict = {}
    for relative in sorted(SCAN_ROOTS):
        for path in scan(REPO_ROOT / relative):
            found = floor_violations(path, floor)
            if found:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        f"a text call passes a keyword the declared {floor[0]}.{floor[1]} floor does not accept, "
        f"which is a TypeError there and invisible on a newer interpreter -- and invisible to the "
        f"Types leg too, since typeshed does not version-gate these. Offenders: " + repr(offenders)
    )


def test_the_floor_guard_fires_on_the_exact_call_that_shipped_red(tmp_path):
    """The must-fire control, written as the line #469 actually shipped rather than as a synthetic
    one. Its must-not-fire twin is below: the same intent expressed through `.open()`, which is the
    fix, must stay silent."""
    root = tmp_path / "src"
    _write_tree(root, {"as_shipped.py": """
        from pathlib import Path

        def write(tmp: Path, content: str) -> None:
            tmp.write_text(content, encoding="utf-8", newline="")
    """})
    found = floor_violations(scan(root)[0], (3, 9))
    assert len(found) == 1 and "write_text(newline=...)" in found[0], found


def test_the_floor_guard_spares_the_fix_and_the_read_side_twin(tmp_path):
    """Two must-not-fire cases in one. `.open(..., newline="")` is the fix and has taken the keyword
    since the method existed; and a floor that has caught up must relax the rule rather than keep
    reporting it -- the same call at a 3.10 floor is legal, which is why the floor is read from
    pyproject and not written down here."""
    root = tmp_path / "src"
    _write_tree(root, {"fixed.py": """
        from pathlib import Path

        def write(tmp: Path, content: str) -> None:
            with tmp.open("w", encoding="utf-8", newline="") as fh:
                fh.write(content)
    """})
    assert floor_violations(scan(root)[0], (3, 9)) == []

    raised = tmp_path / "raised"
    _write_tree(raised, {"as_shipped.py": """
        from pathlib import Path

        def write(tmp: Path, content: str) -> None:
            tmp.write_text(content, encoding="utf-8", newline="")
    """})
    assert floor_violations(scan(raised)[0], (3, 10)) == []


def test_the_new_3_14_leg_is_declared_consistently():
    """#298 (step 1 of 2): a `Test (py3.14)` leg was added to the CI compatibility-axis matrix as a
    new job name beside the existing five, without touching the floor -- `requires-python`, ruff's
    `target-version`, pyright's `pythonVersion` and the platform matrix's low end are all untouched
    (that is step 2, deferred pending pypistats download data). Pin the two places that must agree
    with each other so a leg added to CI and not to the package's own advertised versions -- or the
    reverse -- goes red instead of silently under- or over-stating what is actually tested."""
    matrix = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert '"3.14"' in matrix, "ci.yml's compatibility-axis matrix does not list 3.14"
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "Programming Language :: Python :: 3.14" in pyproject, (
        "pyproject.toml's classifiers do not name 3.14, though CI now tests it")


def test_the_floor_is_read_from_pyproject_and_matches_what_ci_runs():
    """The lever control. A floor this guard could not read would make every case above vacuous, and
    a floor that disagreed with the CI matrix would guard a version nothing runs."""
    floor = _declared_floor()
    matrix = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f'"{floor[0]}.{floor[1]}"' in matrix or f"'{floor[0]}.{floor[1]}'" in matrix, (
        f"pyproject declares a {floor[0]}.{floor[1]} floor and no CI leg runs it, so nothing "
        f"observes what that interpreter actually accepts")


# --------------------------------------------------------------------------------------------------
# The runtime half. #29 measured that this suite captures to io.StringIO, so nothing in-process can
# reach the console encoder even once a Windows leg exists. A subprocess with a forced narrow
# encoder reaches the real one, on every platform.
# --------------------------------------------------------------------------------------------------

def _clean_env(extra: dict) -> dict:
    env = dict(os.environ)
    # Any of these, inherited from uv or a parent shell, would override the levers below and turn
    # every control in this section green for the wrong reason.
    for name in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONWARNDEFAULTENCODING"):
        env.pop(name, None)
    env.update(extra)
    return env


def _cli(args: list, extra_env: dict, cwd: Path, python_args: list = None):
    env = _clean_env(extra_env)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, *(python_args or []), "-m", "requivo", *args],
        cwd=str(cwd), env=env, capture_output=True, timeout=300,
    )


_ASCII_CONSOLE = {"PYTHONIOENCODING": "ascii"}

# The two glyphs doctor leads with, named by codepoint so this source file stays pure ASCII: a
# console whose codepage cannot represent a check mark must not be able to kill a failure report.
_CHECK_MARK = chr(0x2705)
_WARNING_SIGN = chr(0x26A0)


def test_the_ascii_console_lever_actually_bites():
    """The positive control for the console tests below. `PYTHONIOENCODING=ascii` is a *narrower*
    console than Windows cp1252 -- cp1252 encodes an em dash and ascii does not -- so a process that
    survives it survives cp1252 for these glyphs. But an environment variable that silently did
    nothing would make "the process did not crash" pass for entirely the wrong reason, so whether
    the lever bites is measured rather than assumed."""
    probe = subprocess.run(
        [sys.executable, "-c", "print(chr(0x2705))"],
        env=_clean_env(_ASCII_CONSOLE), capture_output=True, text=True, timeout=60,
    )
    assert probe.returncode != 0, (
        "PYTHONIOENCODING=ascii did not take on this interpreter; the console controls below cannot "
        "fire and must not be read as evidence."
    )
    assert "UnicodeEncodeError" in probe.stderr, probe.stderr


@pytest.mark.parametrize("verb", [["doctor"], ["schema"], ["demo"]])
def test_the_cli_survives_a_console_that_cannot_encode_its_glyphs(verb, tmp_path):
    """#29. `doctor` prints a check mark on its very first line and dies there on a console that
    cannot encode it -- after the whole diagnosis it exists to report has already been computed, so
    the exit code describes the crash rather than the finding.

    The ordering is what makes this a correctness bug rather than a cosmetic one, and it is worst on
    the write verbs: `requivo brief` dies in the renderer after the paid provider call has already
    landed a revision, so the operator re-runs and pays twice."""
    r = _cli(verb, _ASCII_CONSOLE, tmp_path)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"UnicodeEncodeError" not in r.stderr, (
        f"`requivo {' '.join(verb)}` died in its own renderer on a console that cannot encode its "
        f"glyphs: {detail}"
    )
    assert r.returncode == 0, detail
    assert r.stdout.strip(), "the command exited 0 and printed nothing at all"


def test_the_console_chokepoint_degrades_rather_than_dropping_the_glyph(tmp_path):
    """What survival must *not* mean: silently printing nothing where a glyph was. A renderer that
    swallows the character it could not encode trades the loud failure for the quiet one -- the
    reader sees a well-formed line with a word missing from it, and nothing says so. An escape is
    ugly and honest; a hole is neither."""
    r = _cli(["doctor"], _ASCII_CONSOLE, tmp_path)
    assert r.returncode == 0, r.stderr.decode("ascii", "backslashreplace")
    text = r.stdout.decode("ascii", "strict")  # it must be pure ascii on an ascii console
    escaped = [g.encode("ascii", "backslashreplace").decode("ascii") for g in (_CHECK_MARK, _WARNING_SIGN)]
    assert any(e in text for e in escaped), (
        "the glyphs were dropped rather than escaped; a reader cannot tell a missing character from "
        "a character that was never there. Expected one of " + repr(escaped) + " in: " + text
    )


def _seed_a_usage_bearing_session(tmp_path, monkeypatch, *, priced_as_of):
    """A session with one revision carrying token/rate provenance (#292), written directly through
    `core.persistence` -- no provider, no ledger, just the persisted shape `render_session_cost`
    reads. `priced_as_of` is a list: one entry leaves `render_session_cost`'s "rates as of" stamp a
    single date (no separator to join), two distinct entries make it a middle-dot-joined list, and
    `None` leaves the revision unpriced entirely, which is the renderer's *other* non-ASCII branch
    (an em dash on "no price on file"). Returns the slug."""
    from _fakes import out as build_model
    from _fakes import slot

    from requivo.core import persistence as store

    monkeypatch.chdir(tmp_path)
    slug = "usage-bearing"
    store.create_session(slug, "a leave approval system")
    engine_output = build_model({"problem": slot(80, "explicit", "high")})
    if priced_as_of is None:
        store.save_revision(slug, engine_output, provenance={
            "usage_input_tokens": 1000, "usage_output_tokens": 200,
            "usage_cache_read_tokens": 0, "usage_cache_write_tokens": 0,
        })
    else:
        for i, date in enumerate(priced_as_of):
            tokens = ({"usage_input_tokens": 1000, "usage_output_tokens": 200} if i == 0
                     else {"usage_input_tokens": 500, "usage_output_tokens": 100})
            store.save_revision(slug, engine_output, provenance={
                **tokens, "usage_rate_per_mtok": [2.0, 10.0], "usage_priced_as_of": date,
            })
    return slug


@pytest.mark.parametrize("scenario,priced_as_of,glyph", [
    ("unpriced", None, "—"),                          # em dash: "n/a — no price on file"
    ("multi_dated", ["2026-01-01", "2026-06-01"], "·"),  # middle dot: "... as of X · Y"
])
def test_requivo_status_survives_a_console_that_cannot_encode_the_session_cost_line(
    scenario, priced_as_of, glyph, tmp_path, monkeypatch
):
    """#292, found in audit on this branch. `render_session_cost` prints an em dash (a revision with
    no price on file) or a middle dot (revisions priced under two different rate-table dates) -- the
    identical non-ASCII pattern that needed this whole file's guard for `render_usage`. Its one call
    site is dispatched inside `_cmd_status`, through `app()`'s own `configure_streams()`-protected
    entry point, same as `doctor`/`schema`/`demo` above -- so this is that same sweep, extended to the
    one verb whose crash-worthy content only exists once a session actually carries usage provenance.

    **Discriminating, not merely surviving**: asserting only `returncode == 0` would also pass if the
    session were never usage-bearing at all (`render_session_cost` returns immediately on an
    unpriced-free session, per its own early return) -- exactly the vacuous case this repo keeps
    finding in itself. The escaped glyph is asserted present in stdout, which only happens if
    `render_session_cost` actually ran and actually emitted the non-ASCII content -- see
    `test_render_session_cost_alone_raises_on_a_stream_nothing_can_fix` immediately below for the
    positive control proving that content really is unencodable outside this guarded dispatch."""
    slug = _seed_a_usage_bearing_session(tmp_path, monkeypatch, priced_as_of=priced_as_of)
    r = _cli(["status", slug], _ASCII_CONSOLE, tmp_path)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"UnicodeEncodeError" not in r.stderr, (
        f"`requivo status` died rendering the SESSION COST line ({scenario}) on a console that "
        f"cannot encode its glyphs: {detail}"
    )
    assert r.returncode == 0, detail
    text = r.stdout.decode("ascii", "strict")  # it must be pure ascii on an ascii console
    assert "SESSION COST" in text, (
        f"render_session_cost never ran for the {scenario} scenario -- the sweep proved nothing "
        f"about it. stdout: {text}"
    )
    escaped = glyph.encode("ascii", "backslashreplace").decode("ascii")
    assert escaped in text, (
        f"the {scenario} glyph was dropped rather than escaped, or the guarded path never reached "
        f"it. Expected {escaped!r} in: {text}"
    )


def test_render_session_cost_alone_raises_on_a_stream_nothing_can_fix(tmp_path, monkeypatch):
    """The positive control the test above depends on: called directly -- outside `_cmd_status`,
    outside `app()`'s `configure_streams()` reconfigure, outside every guard -- against a stream that
    genuinely cannot be made safe (`_unconfigurable_stdout`, this file's own fixture for "the last
    line of defence has nothing left to stand on"), `render_session_cost`'s em dash really does raise
    `UnicodeEncodeError`. Without this, the sweep test above proving "no crash" would be unfalsifiable
    -- passing whether or not the renderer's content is actually dangerous."""
    slug = _seed_a_usage_bearing_session(tmp_path, monkeypatch, priced_as_of=None)
    from requivo.render.terminal import render_session_cost
    from requivo.services.sessions import SessionService

    revisions = SessionService().repo.read_meta(slug).revisions
    stream = _unconfigurable_stdout()
    monkeypatch.setattr(sys, "stdout", stream)
    with pytest.raises(UnicodeEncodeError):
        render_session_cost(revisions)


# --------------------------------------------------------------------------------------------------
# The chokepoint's own three states. `doctor` reports these, and a state that is only ever produced
# by an environment no test can reach is a state nobody has checked.
# --------------------------------------------------------------------------------------------------

def _wrapper(encoding: str, errors: str):
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors)


def test_describe_stream_separates_safe_from_will_crash_from_unknown():
    """Three answers, not two. `will_crash` and `unknown` are different findings with different
    remedies, and both differ from `safe` — a check that returned the same value for *I looked and it
    is fine* and *I could not look* would put an absence into `doctor`'s own report."""
    safe = streams.describe_stream(_wrapper("ascii", "backslashreplace"), "stdout")
    assert safe["state"] == "safe" and safe["detail"] is None

    crashy = streams.describe_stream(_wrapper("ascii", "strict"), "stdout")
    assert crashy["state"] == "will_crash"
    assert "UnicodeEncodeError" in crashy["detail"], crashy

    blind = streams.describe_stream(io.BytesIO(), "stdout")  # no .encoding at all
    assert blind["state"] == "unknown"
    assert "cannot look" in blind["detail"], blind

    assert streams.describe_stream(None, "stdout")["state"] == "unknown"


def test_the_published_stream_states_are_all_underscore_spelled():
    """`output.streams[].state` is a published `--json` enum, and every other enum in a `--json`
    payload — `context.status`, `NonSessionEntry.kind`, `UpdateResult.status` — is one word or
    underscore-joined. `will-crash` was the one hyphen, and a consumer that splits on `-` or maps a
    state onto an identifier had one value it had to special-case (#88). This drives all four arms,
    which is also the first coverage `lossy` has had: a state produced only by an environment no
    test reaches is a state nobody has checked."""
    observed = {
        streams.describe_stream(_wrapper("ascii", "backslashreplace"), "stdout")["state"],
        streams.describe_stream(_wrapper("ascii", "replace"), "stdout")["state"],
        streams.describe_stream(_wrapper("ascii", "ignore"), "stdout")["state"],
        streams.describe_stream(_wrapper("ascii", "strict"), "stdout")["state"],
        streams.describe_stream(io.BytesIO(), "stdout")["state"],
    }
    assert observed == {"safe", "lossy", "will_crash", "unknown"}, observed
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", s) for s in observed), observed


def test_configure_stream_reports_a_stream_it_could_not_reach():
    """The third state on the *configuring* side. A stream that refused to be configured is exactly
    the one that can still kill the process later, so it has to be nameable rather than silent."""
    reached = streams.configure_stream(_wrapper("ascii", "strict"), "stdout")
    assert reached["state"] in ("configured", "unchanged"), reached
    assert reached["errors"] == streams.ERRORS

    unreachable = streams.configure_stream(io.BytesIO(), "stdout")  # no .reconfigure
    assert unreachable["state"] == "could-not"
    assert "reconfigure" in unreachable["reason"], unreachable

    closed = _wrapper("utf-8", "strict")
    closed.close()
    assert streams.configure_stream(closed, "stdout")["state"] == "could-not"


def test_configure_stream_does_not_overrule_an_operator_who_named_a_codec(monkeypatch):
    """`PYTHONIOENCODING` is a decision about somebody's pipeline. This module guarantees their
    stream cannot crash; it does not get to decide what their stream is for."""
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    stream = _wrapper("ascii", "strict")
    report = streams.configure_stream(stream, "stdout")
    assert stream.encoding == "ascii", "the operator's codec was overruled"
    assert stream.errors == streams.ERRORS, "the no-crash guarantee was not applied"
    assert report["state"] == "unchanged" and "PYTHONIOENCODING" in report["reason"]


def test_safe_write_never_raises_on_a_character_it_cannot_encode():
    """The belt to `configure_streams`' braces. The message this writes is usually an error report,
    and an error report that dies on its own em dash is the whole failure being fixed here."""
    stream = _wrapper("ascii", "strict")
    streams.safe_write(stream, "verdict " + _CHECK_MARK + " done")
    stream.seek(0)
    written = stream.buffer.getvalue().decode("ascii")
    assert "verdict" in written and "done" in written, written
    assert _CHECK_MARK.encode("ascii", "backslashreplace").decode("ascii") in written, written


def test_safe_write_gives_up_quietly_on_a_stream_that_is_gone():
    """A closed stream is not a reason to raise from inside an error handler: there is nowhere left
    to report to, and raising here would replace the message with a traceback about the message."""
    closed = _wrapper("utf-8", "strict")
    closed.close()
    streams.safe_write(closed, "anything")  # must not raise


# --------------------------------------------------------------------------------------------------
# The last resort: a stream that could not be configured at all. `configure_streams` covers every
# stream it can reach, so this arm only fires on the ones it cannot -- which means nothing else in
# this file exercises it, and an untested last resort is a last resort nobody has checked.
# --------------------------------------------------------------------------------------------------

class _Unconfigurable(io.TextIOWrapper):
    """A stream `configure_streams` cannot fix: `reconfigure` refuses, so it stays strict/ascii.

    Not a mock of the failure — a real `TextIOWrapper` on a real ascii codec whose `reconfigure`
    raises the way a detached or substituted stream's does. The `UnicodeEncodeError` below is raised
    by the actual encoder, not by the test.
    """

    def reconfigure(self, **kwargs):
        raise ValueError("underlying buffer has been detached")


def _unconfigurable_stdout():
    return _Unconfigurable(io.BytesIO(), encoding="ascii", errors="strict", write_through=True)


def test_configure_streams_reports_a_stream_it_could_not_fix(monkeypatch):
    """The precondition for everything below: this stream really is one Requivo cannot save."""
    stream = _unconfigurable_stdout()
    monkeypatch.setattr(sys, "stdout", stream)
    report = {r["stream"]: r for r in streams.configure_streams()}["stdout"]
    assert report["state"] == "could-not", report
    assert streams.describe_stream(stream, "stdout")["state"] == "will_crash"
    with pytest.raises(UnicodeEncodeError):
        stream.write(_CHECK_MARK)          # the failure is the encoder's, not the test's


def _run_app_on_an_unconfigurable_stdout(monkeypatch, argv, ledger_calls=()):
    """Drive `cli.app()` with a stdout that cannot be made safe, and return (exit code, stderr)."""
    out, err = _unconfigurable_stdout(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    class _Ledger:
        calls = list(ledger_calls)

    monkeypatch.setattr(cli, "track_usage", lambda: contextlib.nullcontext(_Ledger()))
    # `render_usage` moved to `cli_support.py` by #550, along with `_render_usage_safely` -- the
    # function that reads it. `cli.py` no longer imports the name at all, so patching it there would
    # be a second binding reaching nothing.
    monkeypatch.setattr(cli_support, "render_usage", lambda ledger: None)
    with pytest.raises(SystemExit) as ei:
        cli.app(argv)
    return ei.value.code, err.getvalue()


def test_a_glyph_that_cannot_be_encoded_exits_three_rather_than_a_traceback(monkeypatch, tmp_path):
    """#29's ordering rule, at the last line of defence. The command has already done its work by the
    time anything is printed, so a traceback here reports a failure that did not happen."""
    monkeypatch.chdir(tmp_path)
    code, err = _run_app_on_an_unconfigurable_stdout(monkeypatch, ["doctor"])
    assert code == cli.EXIT_RENDER_FAILED == 3, (code, err)
    assert "could not encode its output" in err, err
    assert "Traceback" not in err, err
    assert "requivo doctor" in err, "the message does not say how to find out which stream: " + err


def test_the_render_failure_message_does_not_claim_a_call_was_billed_when_none_was(monkeypatch, tmp_path):
    """`app()` wraps the whole handler, and several verbs print before they mutate — `discover` echoes
    its context cards before the provider call, and `doctor`/`status`/`schema` never mutate at all. A
    single message asserting *whatever this changed HAS been applied* is false for those, which is
    the same misreporting this branch exists to remove, one layer up. So the arm reads the usage
    ledger instead of assuming."""
    monkeypatch.chdir(tmp_path)
    _, err = _run_app_on_an_unconfigurable_stdout(monkeypatch, ["doctor"], ledger_calls=())
    assert "No provider call was made" in err, err
    assert "HAS completed and been billed" not in err, (
        "`doctor` makes no provider call, and telling the user one was billed is a false statement "
        "in the message that exists to stop a false statement: " + err)


def test_the_render_failure_message_does_say_so_when_a_call_was_billed(monkeypatch, tmp_path):
    """The must-fire half. The warning that matters is the one on the verbs that cost money, and a
    message that never fires it is as useless as one that always does."""
    monkeypatch.chdir(tmp_path)
    _, err = _run_app_on_an_unconfigurable_stdout(monkeypatch, ["doctor"], ledger_calls=("one call",))
    assert "HAS completed and been billed" in err, err
    assert "Do not re-run" in err, err


def test_the_usage_line_cannot_kill_a_run_that_already_paid_for_its_call(monkeypatch, tmp_path):
    """Found by the audit on this branch, and it is #29 one call further out.

    `render_usage` prints a middle dot and an em dash, and two of its three call sites sit *outside*
    the `UnicodeEncodeError` arm -- including the one that runs after a wholly successful command. So
    a successful `requivo brief` on an unreachable stream still died at the usage line, after the
    provider call had been billed and the revision applied. That is the exact ordering this branch
    exists to close, surviving on the one route where nothing had gone wrong.

    Driven through `_render_usage_safely` with a real ledger and a real ascii encoder, so the
    exception under test is the encoder's."""
    from requivo.providers.anthropic.pricing import price_call
    from requivo.usage import CallRecord, UsageLedger

    # Priced through `price_call`, the way the provider files a real call (#167): a bare CallRecord
    # carries no rate, and an unpriced ledger renders the *other* branch -- so without this the test
    # would still pass, on a line it was not written about.
    ledger = UsageLedger()
    ledger.record(price_call(CallRecord(model="claude-sonnet-5", input_tokens=10, output_tokens=20,
                                        cache_read_tokens=0, cache_write_tokens=0, latency_ms=5)))

    stream = _unconfigurable_stdout()
    monkeypatch.setattr(sys, "stdout", stream)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)

    # The control: unwrapped, this really does raise on this ledger and this stream. Through
    # `cli_support`, not `cli` (#550) -- `cli.py` no longer imports the name at all.
    with pytest.raises(UnicodeEncodeError):
        cli_support.render_usage(ledger)

    cli._render_usage_safely(ledger)      # the wrapper must not
    assert "could not be encoded" in err.getvalue(), (
        "the usage line vanished without a word; a line nobody can read is not the same as a run "
        "that made no calls, and they must not print the same way: " + repr(err.getvalue()))


# --------------------------------------------------------------------------------------------------
# A file the *user* named. The one read whose bytes this project did not write.
# --------------------------------------------------------------------------------------------------

def test_a_user_file_that_is_not_utf8_is_refused_by_name_not_by_traceback(tmp_path):
    """Refusing is right — mojibake validates, and half a request reads exactly like a whole one. But
    refusing with a bare `UnicodeDecodeError` would trade a silently wrong answer for an unexplained
    crash, which is the same trade one step along. The refusal has to be an answer."""
    brief = tmp_path / "brief.md"
    # A French sentence saved as cp1252 by a Windows editor: the realistic input, not an exotic one.
    brief.write_bytes("Système de validation des congés.".encode("cp1252"))

    with pytest.raises(InvalidModelError) as ei:
        read_user_text(brief)

    message = str(ei.value)
    assert "not valid UTF-8" in message, message
    assert "0xe8" in message, ("the offending byte is not named, so the user cannot tell which "
                              f"character to look for: {message}")
    assert ei.value.details["path"] == str(brief)
    assert ei.value.details["expected_encoding"] == "utf-8"
    assert isinstance(ei.value.details["position"], int)


def test_a_user_file_that_is_utf8_is_read_unchanged(tmp_path):
    """The must-not-fire half. A refusal that fires on correct input is worse than no refusal, and
    this is the case that matters most on this project: French client prose, correctly encoded."""
    brief = tmp_path / "brief.md"
    original = "Système de validation des congés — 5 000 salariés."
    brief.write_bytes(original.encode("utf-8"))
    assert read_user_text(brief) == original


def test_the_refusal_does_not_let_a_path_forge_a_line_of_output(tmp_path):
    """The message interpolates a path the user supplied. A path carrying a newline must not be able
    to write what looks like a second, authoritative line of Requivo's own output -- the shape #40
    found in `doctor`."""
    sneaky = tmp_path / "brief\nERROR: session verified OK.md"
    try:
        sneaky.write_bytes(b"\xe8")
    except (OSError, ValueError):
        pytest.skip("this filesystem refuses a newline in a filename; the forging path is untested here")
    with pytest.raises(InvalidModelError) as ei:
        read_user_text(sneaky)
    body = str(ei.value)
    assert not any(line.startswith("ERROR:") for line in body.splitlines()), (
        "a user-supplied path forged a line at column 0 of Requivo's own message: " + repr(body))


_WARN_DEFAULT_ENCODING = {"PYTHONWARNDEFAULTENCODING": "1"}
_ERROR_ON_DEFAULT_ENCODING = ["-W", "error::EncodingWarning"]

_NO_LEVER_ON_39 = (
    "EncodingWarning and PYTHONWARNDEFAULTENCODING are 3.10+, so this lever cannot fire on 3.9. "
    "UNTESTED ON THIS INTERPRETER: that the CLI reads its bundled assets with an explicit codec "
    "rather than the locale's. The static guard above covers the same claim on every interpreter, "
    "and the 3.10-3.13 legs of the CI matrix do run this one."
)


def test_the_default_encoding_lever_actually_bites(tmp_path):
    """Positive control for the read-side test below, on the same reasoning as the console one."""
    if sys.version_info < (3, 10):
        pytest.skip(_NO_LEVER_ON_39)
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent("""
        import sys
        from pathlib import Path

        Path(sys.argv[1]).read_text()
    """), encoding="utf-8")
    target = tmp_path / "data.txt"
    target.write_text("plain", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, *_ERROR_ON_DEFAULT_ENCODING, str(probe), str(target)],
        env=_clean_env(_WARN_DEFAULT_ENCODING), capture_output=True, text=True, timeout=60,
    )
    assert r.returncode != 0 and "EncodingWarning" in r.stderr, (
        "the default-encoding lever did not take; the read-side control below cannot fire and must "
        "not be read as evidence: " + r.stderr
    )


@pytest.mark.parametrize("verb", [["schema"], ["schema", "--framework"], ["context"], ["demo"], ["doctor"]])
def test_the_cli_reads_its_assets_with_an_explicit_encoding(verb, tmp_path):
    """#11's read half, at runtime rather than statically.

    `-W error::EncodingWarning` turns *any* locale-default text read into an exception, identically
    on every platform and in every locale -- a sharper lever than forcing a locale, because it fires
    on a read that happens to succeed today. All five bundled context cards are cp1252-decodable,
    which is exactly why that path corrupts silently on Windows instead of crashing, and why a
    crash-based control would miss it."""
    if sys.version_info < (3, 10):
        pytest.skip(_NO_LEVER_ON_39)
    r = _cli(verb, {**_WARN_DEFAULT_ENCODING, **_ASCII_CONSOLE}, tmp_path,
             python_args=_ERROR_ON_DEFAULT_ENCODING)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"EncodingWarning" not in r.stderr, (
        f"`requivo {' '.join(verb)}` read text with the locale's codec rather than an explicit "
        f"one: {detail}"
    )
    assert r.returncode == 0, detail
