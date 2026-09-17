"""Shared plumbing for the source-scanning guards (#288): a missing or empty scan root is refused (#10)."""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path


def parse_utf8(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def list_python_files(root: Path, *, label: str) -> list[Path]:
    """Every `.py` file under `root`, recursively; refuses rather than answers over what it could not see."""
    if not root.is_dir():
        raise AssertionError(f"{label} could not scan {root}: no such directory -- fix the path, never the assertion.")
    found = sorted(root.rglob("*.py"))
    if not found:
        raise AssertionError(f"{label} scanned {root} and found no Python files. An empty scan set cannot support a 'no offenders' verdict.")
    return found


def list_files(roots: tuple[Path, ...], *, suffixes: tuple[str, ...], label: str, extra: tuple[Path, ...] = ()) -> list[Path]:
    """Every file under `roots` whose suffix is in `suffixes`, plus `extra`; the same refusal over several roots."""
    found = [p for root in roots for p in sorted(root.rglob("*"))
             if p.suffix in suffixes and p.is_file() and "__pycache__" not in p.parts]
    found.extend(p for p in extra if p.is_file())
    if not found:
        raise AssertionError(f"{label} found no files under {roots}. This is 'could not look' -- fix the path, never the assertion.")
    return found


def write_tree(root: Path, sources: dict) -> None:
    """Materialise a small fixture tree under `root`, for a guard's own positive-control tests."""
    for name, source in sources.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source), encoding="utf-8")
