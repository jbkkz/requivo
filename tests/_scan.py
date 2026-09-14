"""Shared plumbing for this suite's source-scanning guards (#288, #551).

Three tiers once each carried near-duplicate `scan`/`_parse`/`_write_tree` copies and one re-derived the empty-root refusal a third time; this module is that logic, once. Like `tests/_cli_harness.py` beside it, the underscore prefix keeps pytest's `test_*.py` collection from picking it up, so every property
it backs is still asserted from the guard file that imports it -- now `test_source_form.py` for all three (#551); `test_boundaries.py`, `test_encoding.py` and `test_narrative_references.py` all stay as name-only stubs so `src/` and `CLAUDE.md`'s citations keep resolving without a `src/` diff."""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path


def parse_utf8(path: Path) -> ast.Module:
    """Parse a source file as UTF-8, explicitly -- this repository's own prose is not ASCII (an em dash is enough), so a bare `read_text()` decodes with the *locale* codepage instead and a guard reading its own scan set can die instead of running, under exactly the locale it exists to protect against. Not
    hypothetical: it is what happened to the #10 guard's first version."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def list_python_files(root: Path, *, label: str) -> list[Path]:
    """Every `.py` file under `root`, recursively. Refuses rather than answers over what it could not see (#10): `Path.rglob` on a directory that does not exist returns `[]`, which is exactly the shape that let a guard pass green while checking nothing. `label` names the calling guard in the message ("boundary
    guard", "the encoding guard", ...); the two templates below are shared verbatim, so every caller's own `match="no such directory"` / `match="no Python files"` still matches regardless of which guard raised it."""
    if not root.is_dir():
        raise AssertionError(
            f"{label} could not scan {root}: no such directory. This is 'could not look', not " f"'looked and found nothing' -- fix the path, never the assertion."
        )
    found = sorted(root.rglob("*.py"))
    if not found:
        raise AssertionError(
            f"{label} scanned {root} and found no Python files. An empty scan set cannot " f"support a 'no offenders' verdict."
        )
    return found


def list_files(roots: tuple[Path, ...], *, suffixes: tuple[str, ...], label: str,
                extra: tuple[Path, ...] = ()) -> list[Path]:
    """Every file under `roots` whose suffix is in `suffixes`, plus `extra`. The same refusal as `list_python_files`, generalised past `.py` alone and over several roots at once, for `test_narrative_references.py`."""
    found = [p for root in roots for p in sorted(root.rglob("*"))
             if p.suffix in suffixes and p.is_file() and "__pycache__" not in p.parts]
    found.extend(p for p in extra if p.is_file())
    if not found:
        raise AssertionError(
            f"{label} found no files under {roots}. This is 'could not look', not 'looked and " f"found nothing' -- fix the path, never the assertion."
        )
    return found


def write_tree(root: Path, sources: dict) -> None:
    """Materialise a small fixture tree under `root`, for a guard's own positive-control tests."""
    for name, source in sources.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source), encoding="utf-8")
