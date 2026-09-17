"""Atomic writes and the transient-rename retry (invariant 18), shared by the store and `session export`/`restore` (#550).
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


def _atomic_write(path: Path, content: str) -> Path:
    """Write via a unique temp file and an atomic rename, so an interruption never leaves a half-written
    file (`test_concurrent_atomic_writes_do_not_collide_on_a_temp_file`); scratch is never left behind
    (`test_a_failed_atomic_write_leaves_no_scratch_file`)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # `newline=""` disables translation, or a lone CR reaches Windows disk as '\r\r\n' (#464,
        # `test_atomic_write_passes_newline_empty_to_disable_translation`); through `.open()`, since
        # `write_text(newline=)` is 3.10+ (`test_no_text_call_passes_a_keyword_the_declared_floor_rejects`).
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        _replace_with_retry(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave scratch behind on a failed write
        raise
    return path


# On Windows `rename` fails with `PermissionError` whenever a scanner holds the destination; retried
# briefly, `PermissionError` only, then re-raised (invariant 18). `test_atomic_write_survives_a_transient_permission_error`,
# `test_atomic_write_still_gives_up_on_a_permanent_permission_error`.
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF_S = 0.01


def _replace_with_retry(tmp: Path, path: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))

