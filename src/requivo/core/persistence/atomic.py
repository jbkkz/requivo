"""Atomic writes and the transient-rename retry.

Split out of `core/persistence.py` by #550 (the lean pass, #548): `_atomic_write` and the
invariant-18 retry it depends on, plus `_replace_with_retry` itself -- moved here from
`deterministic/sessions.py` so `session export`/`restore` and the store share one helper instead of
two copies of the same retry loop.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


def _atomic_write(path: Path, content: str) -> Path:
    """Write via a temp file + atomic rename, so an interruption can never leave a half-written file
    where a good one was. model.json is the durable product — a truncated JSON would be unrecoverable,
    and `os.replace` (via Path.replace) is atomic on the same filesystem.

    The temp name is unique per writer: a fixed one made concurrent writers share a scratch file, and
    the second `replace()` then raised `FileNotFoundError` where the caller should have seen a clean
    write or a `RevisionConflictError` — `test_concurrent_atomic_writes_do_not_collide_on_a_temp_file`.
    Scratch is never left behind on a failure: `test_a_failed_atomic_write_leaves_no_scratch_file`."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # newline="" disables universal-newline translation on write -- the direct analogue of the
        # explicit encoding= beside it (invariant 16). Without it a lone CR already in `content`
        # reaches Windows disk as '\r\r\n', a line the document never had (#464).
        # `test_atomic_write_passes_newline_empty_to_disable_translation`.
        #
        # Through `.open()` and not `write_text(..., newline="")`: that keyword is 3.10+, and on this
        # project's 3.9 floor it is a TypeError on every single write -- which it was, on three CI
        # legs. Guarded as a class rather than at this one site, by
        # `test_no_text_call_passes_a_keyword_the_declared_floor_rejects`.
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        _replace_with_retry(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave scratch behind on a failed write
        raise
    return path


# On Windows `rename` is `MoveFileEx`, which fails with `PermissionError(13)` whenever anything holds
# a handle to the destination — an antivirus scanner or the Search Indexer, neither of which this
# process can serialise against — and losing a completed write to the durable product is not an
# acceptable outcome (invariant 18). Deliberately bounded and narrow: `PermissionError` only, then
# the original is re-raised, because a slow permanent error helps nobody. This is the one place in
# the store where retrying is right rather than a way of hiding something — the operation is
# idempotent and the cause is external. `test_atomic_write_survives_a_transient_permission_error`,
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

