"""Measures how much of this tree is prose, so a claim about its size is something a test can read
back rather than a number typed into an issue (#553).

**Why this exists rather than `wc -l` and a claim.** Four numbers were written in good faith during
the lean pass (#548) and believed weeks later: the meta-guard estate was said to be 10,500 lines
(#551, it was 6,432); the test suite was targeted at 1.2x the product's code (#555, it measured
2.74x); a duplication pass promised >=600 `src/` lines removed (#556, it delivered +47); and this
repository's own `CLAUDE.md` cited a `tests/lean_budget.toml` that did not exist. A number in a file
this script re-derives and a test reads cannot go stale in that way -- it is recomputed every run.

**What "prose" means here.** Every physical line of a `.py` file falls into exactly one of four
buckets: *blank* (whitespace only), *docstring* (spanned by a module-, class- or function-level
docstring, found via `ast` -- the first statement of a module/class/function body being a bare
string constant, the ordinary meaning of "docstring"), *comment* (a `tokenize.COMMENT` token that is
the only non-trivial token on its line -- a trailing `# note` on a code line does not count, so a
one-line `def f():  # noop` is not double-counted as both), and *code* (everything else). A blank
line inside a triple-quoted docstring counts as docstring, not blank, because it is part of the prose
block a docstring-length ceiling is measuring. Prose share is `(docstring + comment) / total`.

**Why `ast` and `tokenize` rather than a regex.** A regex that treats a line starting with `#` as a
comment is wrong the moment a triple-quoted string contains a line that happens to start with `#`
(this repository's own prompts and context-card excerpts do), and a regex that treats a triple-quoted
line as a docstring cannot tell a docstring from an ordinary string constant assigned to a variable.
`ast` resolves the first case correctly because it parses the language rather than pattern-matching
its text; `tokenize` resolves the second because a `COMMENT` token cannot appear inside a string.

**Python 3.9, stdlib only.** This runs on a clean checkout with nothing installed beyond what ships
with the interpreter (`ast`, `tokenize`, `pathlib`) -- CI's "did the tool even import" is a
`python3.9 -c "import scripts.prose_measure"` away from every dependency the rest of the suite needs.
PEP 604 `X | Y` runtime expressions (3.10+) are avoided on purpose in favour of
`Optional`, kept from `typing` even though `X | None` reads the same once
parsed -- see #553's own note about checking rather than assuming a version boundary.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# The three source trees this script measures by default; `docs/compatibility.md` is measured
# separately below since it is one file, not a tree of `.py` modules.
GROUPS = {
    "src": REPO_ROOT / "src" / "requivo",
    "tests": REPO_ROOT / "tests",
    "scripts": REPO_ROOT / "scripts",
}
COMPATIBILITY_DOC = REPO_ROOT / "docs" / "compatibility.md"


def _relative(path: Path) -> str:
    """`path` relative to the repo root, for a report meant to be pasted somewhere else -- an
    absolute path is only ever meaningful on the machine that produced it."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


@dataclass
class FileMeasurement:
    """One `.py` file's line counts. `docstrings` records each module/class/function docstring found
    in this file, as `(kind, lineno, length)`, for the caller that wants the fattest one rather than
    the file total."""

    path: Path
    total: int = 0
    code: int = 0
    docstring: int = 0
    comment: int = 0
    blank: int = 0
    docstrings: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass
class GroupMeasurement:
    """A tree's totals plus its fattest file -- the two figures #553 asks a ceiling for."""

    files: list[FileMeasurement] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(f.total for f in self.files)

    @property
    def code(self) -> int:
        return sum(f.code for f in self.files)

    @property
    def docstring(self) -> int:
        return sum(f.docstring for f in self.files)

    @property
    def comment(self) -> int:
        return sum(f.comment for f in self.files)

    @property
    def blank(self) -> int:
        return sum(f.blank for f in self.files)

    @property
    def prose_share(self) -> float:
        total = self.total
        return (self.docstring + self.comment) / total if total else 0.0

    @property
    def largest(self) -> Optional[FileMeasurement]:
        return max(self.files, key=lambda f: f.total, default=None)

    def docstring_max(self, kind: str) -> tuple[int, Optional[str]]:
        """The longest docstring of `kind` ("module" or "function" -- "function" also covers class
        and method bodies, since a test suite's own docstring budget does not distinguish them) across
        every file, as `(length, "path:lineno")`, or `(0, None)` if this group has none."""
        best_len, best_where = 0, None
        for f in self.files:
            for k, lineno, length in f.docstrings:
                matches = k == kind or (kind == "function" and k in ("function", "class"))
                if matches and length > best_len:
                    best_len, best_where = length, f"{_relative(f.path)}:{lineno}"
        return best_len, best_where


def _docstring_spans(tree: ast.Module) -> list[tuple[str, int, int, int]]:
    """Every module/class/function docstring in `tree`, as `(kind, first_line, last_line, length)`.

    A docstring is the first statement of a module/class/function body when that statement is a bare
    string-constant expression -- the language's own definition (what `ast.get_docstring` finds), not
    "any triple-quoted string", which would also catch a string constant used as ordinary data.
    """
    spans: list[tuple[str, int, int, int]] = []

    def _body_docstring(kind: str, body: list[ast.stmt]) -> None:
        if not body:
            return
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            end = getattr(first, "end_lineno", first.lineno)
            spans.append((kind, first.lineno, end, end - first.lineno + 1))

    _body_docstring("module", tree.body)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            _body_docstring("class", node.body)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _body_docstring("function", node.body)
    return spans


def measure_file(path: Path) -> FileMeasurement:
    """One file's line breakdown. Raises rather than skipping on a decode or a syntax error -- a file
    this script cannot parse is "could not look", the same refusal `tests/_scan.py` applies to an
    empty scan root, not a silent zero folded into a total nobody can then trust."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    total = len(lines)
    tree = ast.parse(source, filename=str(path))
    docstrings = _docstring_spans(tree)

    category = ["code"] * (total + 1)  # 1-indexed; index 0 unused
    for _kind, first, last, _length in docstrings:
        for lineno in range(first, last + 1):
            if 1 <= lineno <= total:
                category[lineno] = "docstring"

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            lineno = tok.start[0]
            if 1 <= lineno <= total and category[lineno] == "code":
                line_text = lines[lineno - 1]
                if line_text[: tok.start[1]].strip() == "":
                    category[lineno] = "comment"

    for lineno in range(1, total + 1):
        if category[lineno] == "code" and lines[lineno - 1].strip() == "":
            category[lineno] = "blank"

    counts = {"code": 0, "docstring": 0, "comment": 0, "blank": 0}
    for lineno in range(1, total + 1):
        counts[category[lineno]] += 1

    return FileMeasurement(
        path=path,
        total=total,
        code=counts["code"],
        docstring=counts["docstring"],
        comment=counts["comment"],
        blank=counts["blank"],
        docstrings=[(kind, first, length) for kind, first, _last, length in docstrings],
    )


def measure_group(root: Path) -> GroupMeasurement:
    """Every `.py` file under `root`, recursively, skipping `__pycache__` the way `tests/_scan.py`
    does for the same reason: bytecode is not source and has nothing to say about prose share."""
    group = GroupMeasurement()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        group.files.append(measure_file(path))
    return group


def line_count(path: Path) -> int:
    """A plain line count for a non-Python file (`docs/compatibility.md`) or for a caller (a test
    naming the meta-guard estate's own file list) that wants totals without the code/prose split."""
    return len(path.read_text(encoding="utf-8").splitlines())


def code_ratio(tests: GroupMeasurement, src: GroupMeasurement) -> float:
    """Test *code* lines per line of product *code* -- `code`, not `total`, on both sides, matching
    how #555 stated it (21,999 test code lines against 8,042): a docstring-heavy test file should not
    inflate the ratio a prose-reduction pass is trying to bring down."""
    return tests.code / src.code if src.code else 0.0


def _format_group(name: str, group: GroupMeasurement) -> str:
    lines = [
        f"== {name} ==",
        f"  files:            {len(group.files)}",
        f"  total lines:      {group.total}",
        f"  code lines:       {group.code}",
        f"  docstring lines:  {group.docstring}",
        f"  comment lines:    {group.comment}",
        f"  blank lines:      {group.blank}",
        f"  prose share:      {group.prose_share:.1%}",
    ]
    largest = group.largest
    if largest is not None:
        lines.append(f"  largest module:   {_relative(largest.path)} ({largest.total} lines)")
    return "\n".join(lines)


def render_report() -> str:
    """The human-readable report `main()` prints -- stable enough to paste into a release audit or a
    pull request body, which is why every field is labelled rather than left as a bare number."""
    groups = {name: measure_group(root) for name, root in GROUPS.items()}
    sections = [_format_group(name, groups[name]) for name in ("src", "tests", "scripts")]

    ratio = code_ratio(groups["tests"], groups["src"])
    mod_len, mod_where = groups["tests"].docstring_max("module")
    fn_len, fn_where = groups["tests"].docstring_max("function")
    sections.append(
        "\n".join(
            [
                "== tests (derived) ==",
                f"  test:product code ratio:     {ratio:.2f}x",
                "  largest module docstring:    {} lines{}".format(
                    mod_len, f" ({mod_where})" if mod_where else ""
                ),
                "  largest function docstring:  {} lines{}".format(
                    fn_len, f" ({fn_where})" if fn_where else ""
                ),
            ]
        )
    )

    if COMPATIBILITY_DOC.is_file():
        sections.append(
            "\n".join(
                [
                    "== docs/compatibility.md ==",
                    f"  total lines:      {line_count(COMPATIBILITY_DOC)}",
                ]
            )
        )

    return "\n\n".join(sections) + "\n"


def main() -> int:
    print(render_report(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
