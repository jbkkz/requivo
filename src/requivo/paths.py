"""Filesystem anchors: `ASSETS`, the read-only bundled data inside the package (so a wheel install
works outside the clone), and the workspace roots, which are never inside the package. Every module
resolves paths through here, never through its own `__file__`.
"""

from __future__ import annotations

import os
from pathlib import Path

# Assets sit next to this file, inside the package; a wheel unpacks to the filesystem, so a plain Path works.
ASSETS = Path(__file__).resolve().parent / "assets"

PROMPTS = ASSETS / "prompts"
CONTEXT = ASSETS / "context"
DEMO = ASSETS / "demo"

# Per-perimeter data (#608): each installed perimeter owns a directory here; `core/perimeters.py` resolves the id.
PERIMETERS = ASSETS / "perimeters"


def workspace_root() -> Path:
    """The user's working area, where sessions are written: cwd, or `REQUIVO_WORKSPACE`. Evaluated per call."""
    override = os.getenv("REQUIVO_WORKSPACE")
    return Path(override) if override else Path.cwd()


def store_root() -> Path:
    """The whole on-disk store, `<workspace>/.requivo/`; its creation is where the privacy `.gitignore` is written (#211)."""
    return workspace_root() / ".requivo"


def session_root() -> Path:
    """Canonical home for sessions: `<workspace>/.requivo/sessions/<slug>/`."""
    return store_root() / "sessions"


def lock_root() -> Path:
    """The per-session write locks, `<workspace>/.requivo/locks/`: a sibling of `session_root()`, not
    inside the session it locks (#113), since `flock` claims an inode and `session import --force`
    renames the directory. `test_a_forced_import_serialises_against_a_concurrent_writer`."""
    return store_root() / "locks"


def debug_root() -> Path:
    """Where a provider reply that never validated is written for a bug report (#283), under
    `store_root()` so the privacy `.gitignore` covers it."""
    return store_root() / "debug"


def output_root() -> Path:
    """The retired `./out` layout, read only by `requivo session migrate`; `REQUIVO_OUTPUT_DIR` overrides."""
    override = os.getenv("REQUIVO_OUTPUT_DIR")
    return Path(override) if override else Path.cwd() / "out"


def user_context_dir() -> Path:
    """Where a user drops their own context cards: `~/.config/requivo/context`, or `REQUIVO_CONTEXT_DIR`.
    May not exist; a user card overrides a bundled one with the same stem."""
    override = os.getenv("REQUIVO_CONTEXT_DIR")
    return Path(override) if override else Path.home() / ".config" / "requivo" / "context"
