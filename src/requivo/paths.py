"""Filesystem anchors — the single source of truth for path resolution.

Two roots, deliberately separate:

- **`ASSETS`** — read-only bundled data (prompts, framework, context, the demo payload). It lives
  *inside* the package (`src/requivo/assets/`), so it resolves identically from an editable
  checkout and from an installed wheel, and setuptools ships it as package data. Resolving it from
  this module's own location (not the repo root) is what makes a `pip install` work outside the clone.
- **`output_root()`** — where generated models/artifacts are written. This must never be inside the
  package (site-packages is often read-only, and writing there would be wrong anyway), so it defaults
  to `./out` under the current working directory and can be redirected with `REQUIVO_OUTPUT_DIR`.

Every module resolves assets and outputs through here, never through its own `__file__`.
"""

from __future__ import annotations

import os
from pathlib import Path

# src/requivo/paths.py → assets sit next to this file, inside the package. A normal wheel
# install unpacks the package to the filesystem, so a plain Path (with .glob/.read_text) works; we
# don't need importlib.resources' zip handling, and the wheel-install CI job proves this resolves.
ASSETS = Path(__file__).resolve().parent / "assets"

PROMPTS = ASSETS / "prompts"
FRAMEWORK = ASSETS / "framework"
CONTEXT = ASSETS / "context"
DEMO = ASSETS / "demo"


def workspace_root() -> Path:
    """The user's working area — where sessions are written. Defaults to the current working
    directory; override with `REQUIVO_WORKSPACE` (the CLI's `--workspace` sets this env for the run).
    Never inside the installed package. Evaluated per call so a `cd`/env change takes effect without
    reimporting. Distinct from `ASSETS` (read-only package data): assets are read, the workspace is
    written."""
    override = os.getenv("REQUIVO_WORKSPACE")
    return Path(override) if override else Path.cwd()


def store_root() -> Path:
    """The whole on-disk store: `<workspace>/.requivo/`. `session_root()` and `lock_root()` are its
    two children, and both derive from here rather than restating the directory name.

    It exists as a name of its own because something has to own *the moment this directory comes into
    existence* — that is where the privacy `.gitignore` is written (`ensure_store_dir` in
    `core/persistence/store.py`, #211, #550). Evaluated per call, like every other root."""
    return workspace_root() / ".requivo"


def session_root() -> Path:
    """Canonical home for sessions: `<workspace>/.requivo/sessions/`. Each session is a `<slug>/`
    directory under here (session.json + model.json + revisions/ + request.md + artifacts/). This is
    where every session lives; the retired `output_root()` (`./out`) is read only by
    `requivo session migrate`."""
    return store_root() / "sessions"


def lock_root() -> Path:
    """Where the per-session write locks live: `<workspace>/.requivo/locks/`.

    **A sibling of `session_root()`, not a child of it, and not inside the session it locks** (#113).

    The lock used to be `<slug>/.lock`. `flock` is held by the *open file description*, so it is a
    claim on an **inode**, while every writer under it resolves `canonical_dir(slug)` and writes by
    **pathname** — two descriptions that agree only while nothing renames the directory.
    `session import --force` renames it. A writer inside `save_revision` therefore went on writing
    into the *freshly imported* directory, and a third process opening the lock found a different
    inode and acquired at once: two writers holding one slug's lock, which is invariant 9's own
    failure mode. `_swap_in` could not simply take the lock either — an open handle inside a
    directory is exactly what Windows refuses to rename, which is how #112's four Windows legs died.

    Moving the file out of the renamed directory is what makes both true at once: the swap runs
    under the same lock every writer takes, and `os.replace` sees no open handle. Pinned by
    `test_a_forced_import_serialises_against_a_concurrent_writer`.

    **A sibling rather than `session_root()/.locks/`**, which was the other candidate. The session
    root's contract is that everything under it is a session or is reported as not being one —
    `_scan_session_root` partitions exactly that, and its dot-prefix skip is justified there as
    "`create_session`'s staging areas: a session in flight". A permanent dot directory is not a
    session in flight, so putting locks there would make that sentence false and leave every future
    change to the skip one step from exposing them. Out here there is no coupling to undo.

    Evaluated per call, like every other root."""
    return store_root() / "locks"


def debug_root() -> Path:
    """Where a provider reply that never validated is written for a bug report to attach:
    `<workspace>/.requivo/debug/` (#283). A sibling of `session_root()` and `lock_root()`, under the
    same `store_root()` -- so the privacy `.gitignore` `ensure_store_dir` writes the first time
    `.requivo/` is created (core/persistence/store.py, #211, #550) already covers it with `*`, the same way it
    already covers `sessions/` and `locks/`. A raw provider reply can contain the client's request
    text verbatim, which is exactly the confidential material that marker exists to keep out of a
    `git add .`. Evaluated per call, like every other root."""
    return store_root() / "debug"


def output_root() -> Path:
    """The **retired** `./out` layout, from before the versioned `.requivo/sessions/` store.

    Nothing writes here, and since 0.9.8 nothing reads it implicitly either: `requivo session migrate`
    converts these sessions into the store, and that is the only path that opens them. It kept
    existing because deleting the migrator would strand anyone who still has one. Override with
    `REQUIVO_OUTPUT_DIR`. Evaluated per call."""
    override = os.getenv("REQUIVO_OUTPUT_DIR")
    return Path(override) if override else Path.cwd() / "out"


def user_context_dir() -> Path:
    """Where a user drops their own context cards, so a pip-installed setup can be extended without a
    source checkout (the bundled cards in `CONTEXT` are inside the read-only package). Defaults to
    `~/.config/requivo/context`; override with `REQUIVO_CONTEXT_DIR`. May not exist — callers check.
    A user card whose stem matches a bundled one overrides it (see `load_context`)."""
    override = os.getenv("REQUIVO_CONTEXT_DIR")
    return Path(override) if override else Path.home() / ".config" / "requivo" / "context"
