"""Measures how much of this tree is prose, so a size claim is re-derived by a test, not typed (#553).

Every physical line of a `.py` file is exactly one of: *blank*; *docstring* (spanned by a module,
class or function docstring, found via `ast`, blank lines inside included); *comment* (a
`tokenize.COMMENT` that is the only token on its line, so a trailing `# note` stays code); *code*.
Prose share is `(docstring + comment) / total`. `ast` and `tokenize` rather than a regex: a `#` line
inside a triple-quoted string is not a comment, and a string constant is not a docstring.

Python 3.9, stdlib only, so it imports on a clean checkout (hence `Optional`, not `X | None`)."""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# The source trees measured by default; `docs/compatibility.md` is one file, measured separately.
GROUPS = {
    "src": REPO_ROOT / "src" / "requivo",
    "tests": REPO_ROOT / "tests",
    "scripts": REPO_ROOT / "scripts",
}
COMPATIBILITY_DOC = REPO_ROOT / "docs" / "compatibility.md"


def _relative(path: Path) -> str:
    """`path` relative to the repo root, for a report pasted elsewhere."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


@dataclass
class FileMeasurement:
    """One `.py` file's line counts; `docstrings` holds each `(kind, lineno, length)` found."""

    path: Path
    total: int = 0
    code: int = 0
    docstring: int = 0
    comment: int = 0
    blank: int = 0
    docstrings: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass
class GroupMeasurement:
    """A tree's totals plus its fattest file."""

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
        """The longest docstring of `kind` ("module", or "function" for any def or class) as
                `(length, "path:lineno")`, or `(0, None)`."""
        best_len, best_where = 0, None
        for f in self.files:
            for k, lineno, length in f.docstrings:
                matches = k == kind or (kind == "function" and k in ("function", "class"))
                if matches and length > best_len:
                    best_len, best_where = length, f"{_relative(f.path)}:{lineno}"
        return best_len, best_where


def _docstring_spans(tree: ast.Module) -> list[tuple[str, int, int, int]]:
    """Every module/class/function docstring in `tree`, as `(kind, first_line, last_line, length)`."""
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
    """One file's line breakdown; a decode or syntax error raises rather than folding a zero into a total."""
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
    """Every `.py` file under `root`, recursively, skipping `__pycache__`."""
    group = GroupMeasurement()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        group.files.append(measure_file(path))
    return group


def line_count(path: Path) -> int:
    """A plain line count, for a non-Python file or a caller that wants totals only."""
    return len(path.read_text(encoding="utf-8").splitlines())


def code_ratio(tests: GroupMeasurement, src: GroupMeasurement) -> float:
    """Test *code* lines per line of product *code*, so a docstring-heavy test file does not inflate it (#555)."""
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
    """The labelled report `main()` prints, stable enough to paste into an audit or a PR body."""
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
