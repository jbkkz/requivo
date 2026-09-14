"""The canonical session store (`.requivo/sessions/<slug>/`) -- package entry point.

Split out of the former `core/persistence.py` by #550 (the lean pass, #548) into a package of six
modules, each one concern: `atomic.py` (the atomic-write + transient-rename retry),
`identifiers.py` (slug/filename shape, reserved-name refusal, `derive_slug`), `lock.py` (the OS
lock + `is_contained`, invariant 17), `scan.py` (the report-only diagnostics tier, frozen per
CLAUDE.md's "The persistence diagnostics tier is frozen"), `models.py` (the session metadata
schema + model I/O) and `store.py` (`Store`, the composition root, and the ambient-default
module-level wrappers every prior caller used).

Every name this module used to define at the top level is re-exported here unchanged, so
`from requivo.core.persistence import X`, `from requivo.core import persistence as store` +
`store.X`, and `import requivo.core.persistence as p` + `p.X` all resolve exactly as they did
before the split -- this is the *only* thing that changed, not any of the twelve call sites across
the repo that reach into this module by one of those three forms.
"""
from __future__ import annotations

# This package-entry file's whole job is re-export -- see its own docstring -- so every
# import below is "unused" by the letter of F401 and used by the point of the file.
# ruff: noqa: F401
# Re-exported for callers that reach the stdlib/vendor names off this module directly (rare, but a
# handful of tests patch `store.fcntl` -- see `lock.py`, whose `fcntl`/`msvcrt` this is the same
# module object as, since Python caches `sys.modules` regardless of which file imports it first).
import hashlib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from requivo import __version__
from requivo.core.contracts import EngineOutput, PersistedEngineOutput
from requivo.core.errors import (
    ArtifactRevisionOutOfRangeError,
    InvalidFilenameError,
    InvalidSlugError,
    ModelUnreadableError,
    RevisionConflictError,
    SessionExistsError,
    SessionLockedError,
    SessionNotFoundError,
    SessionUnreadableError,
    UnsupportedFormatVersionError,
    UnsupportedSchemaVersionError,
)
from requivo.core.persistence.atomic import _REPLACE_ATTEMPTS, _REPLACE_BACKOFF_S, _atomic_write, _replace_with_retry
from requivo.core.persistence.identifiers import (
    _FILENAME_RE,
    _LATIN_EXPANSIONS,
    _RESERVED_DEVICE_NAMES,
    _SLUG_BASE_LENGTH,
    _SLUG_RE,
    _SLUG_STOPWORDS,
    MAX_FILENAME_LENGTH,
    MAX_SLUG_LENGTH,
    _is_lock_stem,
    _probe,
    _raise_reserved_slug,
    _refuse_new_reserved_slug,
    _reserved_stem,
    _shape_only,
    _slug_shape,
    derive_slug,
    is_slug,
    validate_filename,
    validate_slug,
)
from requivo.core.persistence.lock import (
    _LOCK_POLL_INTERVAL_S,
    _LOCK_TIMEOUT_SECONDS,
    _acquire,
    _held_locks,
    _LockHandle,
    _release,
    _resolve,
    fcntl,
    is_contained,
    msvcrt,
)
from requivo.core.persistence.models import (
    _RETIRED_KEYS,
    SCHEMA_VERSION,
    SESSION_FORMAT_VERSION,
    ArtifactStatus,
    RevisionRecord,
    SessionMeta,
    _now,
    _read_model,
    content_hash,
    load_model,
    migrate_session,
)
from requivo.core.persistence.scan import _NON_SESSION_SAMPLE, NonSessionEntry, UnexaminableEntry, _describe_non_session
from requivo.core.persistence.store import (
    _STORE_GITIGNORE,
    Store,
    _child_of,
    _default_store,
    _scan_session_root,
    artifact_path,
    canonical_dir,
    create_session,
    debug_root,
    delete_session,
    ensure_store_dir,
    legacy_dir,
    legacy_exists,
    list_session_slugs,
    list_unexaminable_entries,
    load_revision_model,
    load_session_model,
    lock_path,
    lock_root,
    migrate_legacy,
    no_session_message,
    output_root,
    read_artifact_file,
    read_meta,
    save_revision,
    save_session_artifact,
    scan_lock_root,
    scan_session_root,
    session_exists,
    session_lock,
    session_request,
    session_root,
    store_root,
    write_artifact_file,
    write_meta,
)
from requivo.core.selectors import display_token
from requivo.paths import output_root as _ambient_output_root
from requivo.paths import workspace_root

__all__ = [
    "ArtifactRevisionOutOfRangeError", "ArtifactStatus", "InvalidFilenameError", "InvalidSlugError",
    "MAX_FILENAME_LENGTH", "MAX_SLUG_LENGTH", "ModelUnreadableError", "NonSessionEntry",
    "PersistedEngineOutput", "RevisionConflictError", "RevisionRecord", "SCHEMA_VERSION",
    "SESSION_FORMAT_VERSION", "SessionExistsError", "SessionLockedError", "SessionMeta",
    "SessionNotFoundError", "SessionUnreadableError", "Store", "UnexaminableEntry",
    "UnsupportedFormatVersionError", "UnsupportedSchemaVersionError", "artifact_path",
    "canonical_dir", "content_hash", "create_session", "debug_root", "delete_session",
    "derive_slug", "display_token", "ensure_store_dir", "is_contained", "is_slug", "legacy_dir",
    "legacy_exists", "list_session_slugs", "list_unexaminable_entries", "load_model",
    "load_revision_model", "load_session_model", "lock_path", "lock_root", "migrate_legacy",
    "migrate_session", "no_session_message", "output_root", "read_artifact_file", "read_meta",
    "save_revision", "save_session_artifact", "scan_lock_root", "scan_session_root",
    "session_exists", "session_lock", "session_request", "session_root", "store_root",
    "validate_filename", "validate_slug", "workspace_root", "write_artifact_file", "write_meta",
]
