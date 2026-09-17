"""One call to the model: the request, the retry loop, the JSON extraction, the truncation check.

Every exit of `_complete()` records the spend before it surfaces a failure (the success return, the
two clean failures through `_stop()`, and the retry give-up): `test_a_failed_call_is_still_recorded_on_every_exit`.
Attempts and latency are logged here, at DEBUG/WARNING, silent unless a handler is attached (#435, invariant 7).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from requivo.core.context import SystemPrompt
from requivo.core.errors import ProviderOutputError
from requivo.core.persistence import _atomic_write, ensure_store_dir
from requivo.paths import debug_root
from requivo.providers.anthropic.client import (
    _NO_KEY_MESSAGE,
    APIError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    current_model_name,
)
from requivo.providers.anthropic.pricing import price_call
from requivo.providers.errors import EngineError
from requivo.usage import CallRecord, record_call

logger = logging.getLogger(__name__)

# How many failed replies `.requivo/debug/` keeps before the oldest are pruned (#283).
_DEBUG_RETENTION = 20


def _save_failed_reply(raw: str, contract: str) -> Path | None:
    """Write the final raw reply that never validated, so a bug report has something to attach
    (#283). Best-effort: every failure returns `None` rather than shadowing the `ProviderOutputError`.
    The retention bound is a soft cap, not lock-guarded, and a prune failure must not discard the
    path of a reply just saved: `test_a_prune_failure_does_not_discard_an_already_saved_reply`."""
    try:
        # Ambient on purpose, not the triggering session's repository (#272). `decision: debug-dump-ambient-root`
        root = debug_root()
        ensure_store_dir(root)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = root / f"{stamp}-{contract}-{uuid.uuid4().hex[:8]}.txt"
        _atomic_write(path, raw)
    except Exception:
        return None
    try:
        _prune_debug_dir(root)
    except Exception:
        pass  # best-effort, and secondary to the write above -- see the docstring
    return path


def _prune_debug_dir(root: Path) -> None:
    """Keep the newest `_DEBUG_RETENTION` files; the leading UTC timestamp makes name order chronological."""
    files = sorted(root.glob("*.txt"))
    for stale in files[: max(0, len(files) - _DEBUG_RETENTION)]:
        stale.unlink(missing_ok=True)


# Output-token ceiling per call: a rich discovery exceeds 8k, and this non-streaming call risks HTTP
# timeouts above ~16k. Going higher needs streaming.
MAX_OUTPUT_TOKENS = 16000


def _record(rec: CallRecord) -> None:
    """Price the call at the vendor's rates, then file it against the active ledger: the one place this
    provider meets `requivo.usage` (#167). Called exactly once per `CallRecord`."""
    record_call(price_call(rec))


def _log_completed(rec: CallRecord) -> None:
    """DEBUG: a call returned a validated result (#435)."""
    logger.debug("provider call completed: operation=%s model=%s attempts=%d latency_ms=%d",
                rec.operation, rec.model, rec.attempts, rec.latency_ms)


def _log_gave_up(rec: CallRecord, reason: str) -> None:
    """WARNING: a call will not be retried again; `reason` is a short label for an operator (#435)."""
    logger.warning("provider call gave up: operation=%s model=%s attempts=%d latency_ms=%d reason=%s",
                  rec.operation, rec.model, rec.attempts, rec.latency_ms, reason)


def _response_text(resp) -> str:
    """All text blocks of the response, concatenated, so a reply split across blocks is not truncated."""
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def _extract_json(text: str) -> dict:
    """Best-effort JSON extraction: strip a ```json fence, else slice { … }."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found in the reply")
        text = text[start : end + 1]
    return json.loads(text)


_EPHEMERAL = {"type": "ephemeral"}


def _system_blocks(system: str | SystemPrompt, reuse_system: bool) -> list[dict]:
    """The `system` argument for one request: the shared leading block behind a cache breakpoint on
    every call (#258, `test_a_one_call_verb_caches_the_shared_block_and_only_that`), and the
    operation's remainder behind one only under `reuse_system=True`, when the caller's own loop will
    re-send it (`test_cache_breakpoint_rides_a_reused_prefix_and_not_a_single_call`). A breakpoint
    costs 1.25x to write and 0.1x to read; the retry under `reuse_system=False` is an accepted cost,
    `decision: retry-regression-under-reuse-system-false`. A plain `str` is one block:
    `test_a_bare_string_system_has_no_shared_block_to_cache`."""
    # `dict[str, object]`: the values mix `str` and a nested dict, which pyright refused inferred (#271).
    if isinstance(system, str):
        block: dict[str, object] = {"type": "text", "text": system}
        if reuse_system:
            block["cache_control"] = dict(_EPHEMERAL)
        return [block]
    shared: dict[str, object] = {"type": "text", "text": system.shared,
                                 "cache_control": dict(_EPHEMERAL)}
    specific: dict[str, object] = {"type": "text", "text": system.specific}
    if reuse_system:
        specific["cache_control"] = dict(_EPHEMERAL)
    return [shared, specific]


def _transport_message(e: Exception) -> str:
    """What to tell the operator about a transport failure, in three outcomes: a credential failure
    is not transient, a rate limit has its own remedy, everything else keeps the retry advice. The
    SDK's class name rides every branch."""
    detail = f"({type(e).__name__}: {e})"
    if isinstance(e, (AuthenticationError, PermissionDeniedError)):
        return (
            f"Anthropic rejected the credential {detail}.\n"
            f"{_NO_KEY_MESSAGE}\n"
            "If a key is set, it may be expired, revoked, or lacking access to this model. "
            "Retrying will not help until it is replaced. The model on disk was not modified."
        )
    if isinstance(e, RateLimitError):
        return (
            f"Anthropic rate-limited this request {detail}.\n"
            "The model on disk was not modified. Wait for the limit to reset and retry; "
            "running the command again immediately will be rejected the same way."
        )
    return (
        f"Anthropic API unavailable — the request could not be completed {detail}.\n"
        "The model on disk was not modified. Retry the command in a moment."
    )


def _complete(client, system: str | SystemPrompt, messages: list[dict], out_model, retries: int = 2,
              validate=None, *, reuse_system: bool = True, model: str | None = None,
              operation: str | None = None, context: dict | None = None):
    """One call → validated `out_model`, retrying with a nudge in a local message copy on malformed
    or non-conformant JSON. `system` is the split prompt (#258). `operation` is provenance only
    (#435). `validate` is a semantic post-check raising `ValueError` to ride the retry loop.
    `reuse_system` decides the breakpoint on the remainder; True is the safe default. `context`
    (#608) is pydantic validation context. `model` is resolved once, here, with zero env reads for
    an explicit id (#434, `test_a_constructed_model_makes_no_env_read`). Every exit records the spend first."""
    attempt = messages
    last_err = None
    model = model if model is not None else current_model_name()
    rec = CallRecord(model=model, attempts=0, operation=operation)
    started = time.perf_counter()

    def _stop(msg: str) -> EngineError:
        # Record the spend and stamp latency before surfacing a clean failure; both clean exits come
        # through here. `test_a_failed_call_is_still_recorded_on_every_exit`.
        rec.latency_ms = int((time.perf_counter() - started) * 1000)
        _record(rec)
        _log_gave_up(rec, msg.splitlines()[0] if msg else "(no message)")
        return EngineError(msg)

    for _ in range(retries + 1):
        rec.attempts += 1
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=_system_blocks(system, reuse_system),
                messages=attempt,
            )
        except APIError as e:
            # Transport failures become a clean message, branched: a rejected key must not be told to
            # retry (#201). `test_an_auth_failure_names_the_key_and_does_not_advise_retry`.
            raise _stop(_transport_message(e)) from e
        except TypeError as e:
            # The belt: the SDK once raised a bare builtin out of its own auth resolution (#201), and
            # this is not narrowed on its wording. `test_a_typeerror_out_of_the_sdk_is_not_a_traceback`.
            raise _stop(
                f"The Anthropic client could not build the request ({type(e).__name__}: {e}).\n"
                "This is usually an unresolved credential -- check ANTHROPIC_API_KEY, and see "
                "`requivo doctor`. If a working key is set, it is a provider-SDK incompatibility "
                "worth reporting. The model on disk was not modified."
            ) from e
        # Accumulate usage across every attempt; the fields are absent on the test fake.
        u = getattr(resp, "usage", None)
        if u is not None:
            rec.input_tokens += getattr(u, "input_tokens", 0) or 0
            rec.output_tokens += getattr(u, "output_tokens", 0) or 0
            rec.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
            rec.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        raw = _response_text(resp)
        truncated = getattr(resp, "stop_reason", None) == "max_tokens"
        try:
            result = out_model.model_validate(_extract_json(raw), context=context)
            if validate is not None:
                validate(result)
            rec.latency_ms = int((time.perf_counter() - started) * 1000)
            _record(rec)
            _log_completed(rec)
            return result
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
            # A reply cut at the ceiling cannot be salvaged by retrying; checked parse-first, since a
            # reply flagged `max_tokens` can still carry complete JSON.
            if truncated:
                raise _stop(
                    "The model's reply was cut off at the output limit "
                    f"(max_tokens={MAX_OUTPUT_TOKENS}) — the result would be incomplete, so it was "
                    "discarded. Narrow the request, or split it into fewer features per run."
                ) from e
            attempt = attempt + [
                {"role": "assistant", "content": raw or "(empty)"},
                {"role": "user", "content": f"Your reply did not match the required schema ({e}). Reply with ONLY the JSON object, no prose, no code fence."},
            ]
    rec.latency_ms = int((time.perf_counter() - started) * 1000)
    _record(rec)  # record the spend even on give-up — those tokens were still billed
    _log_gave_up(rec, f"retries exhausted against the {out_model.__name__} contract")
    # The give-up is the one exit where the raw reply is worth keeping (#283); `raw` is assigned by
    # every iteration that reaches here. Best-effort.
    debug_path = _save_failed_reply(raw, out_model.__name__)
    details = {"contract": out_model.__name__, "attempts": retries + 1, "last_error": str(last_err)}
    saved_note = ""
    if debug_path is not None:
        details["raw_reply_path"] = str(debug_path)
        saved_note = f" — the reply that failed validation was saved to {debug_path}"
    # A structured error: exhausting the retry loop is a known condition every surface catches.
    raise ProviderOutputError(
        f"the provider returned output that did not match the {out_model.__name__} contract after "
        f"{retries + 1} attempts — the last failure was: {last_err}{saved_note}",
        details=details,
    ) from last_err
