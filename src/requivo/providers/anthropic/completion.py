"""One call to the model: the request, the retry loop, the JSON extraction, the truncation check.

Everything here is about a single `_complete()` — what is sent, what comes back, and what is billed
for it. The discovery turn and the generators are `generators.py`; they all funnel through here.

**The one constraint this module owes another one.** `_complete` records the spend into
`requivo.usage`, and it must do so **before** it surfaces a clean failure, on every exit — a failed
call is still billed for whatever it consumed. That used to be two adjacent lines in one file and is
now a cross-module contract, which is exactly what #74 flagged as the thing most likely to break
quietly in the split. There are four exits and all four record: the success return, the transport
failure and the truncation refusal (both through `_stop()`, which exists so the two clean failures
have one place to get it right), and the retry give-up. Only `_stop()` reads as obviously about
billing, which is why the give-up carries its own line saying so.
`test_a_failed_call_is_still_recorded_on_every_exit` goes red when any exit stops recording.

**`requivo.providers.anthropic.completion`, silent by default (#435).** This is the one place every
provider call's attempts and latency are actually known — every other layer only sees the typed
result or a raised error — so it is where "a call completed, or gave up, after N attempts in M ms"
is logged, at DEBUG (success) and WARNING (any of the three failure exits). A caller who wants the
per-verb view (which *operation* — discovery vs. a named generator) attaches a handler here or to
its `requivo.providers` parent; nobody does by default, so this prints nothing on its own
(invariant 7, and see `requivo/__init__.py`'s `NullHandler`).
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

# How many failed replies `.requivo/debug/` keeps before the oldest are pruned -- documented here
# because #283's acceptance criteria calls for a stated retention, not just a bounded one. Kept
# generous: these are debugging artifacts a user attaches to a bug report, and a directory holding
# the last 20 malformed replies costs at most a few hundred KB.
_DEBUG_RETENTION = 20


def _save_failed_reply(raw: str, contract: str) -> Path | None:
    """Write the final raw reply that never validated, so a bug report has something to attach
    (#283). Called only from the retry loop's give-up exit, and only there.

    **Best-effort.** A failure here -- a read-only workspace, a full disk -- must not shadow the
    `ProviderOutputError` this exists to make debuggable; every exception is caught and `None` is
    returned, so the error message simply doesn't name a file rather than replacing a clean failure
    with an unrelated traceback. `ensure_store_dir` is the same call every other writer under
    `.requivo/` goes through, so this directory gets the privacy `.gitignore` for free the first time
    the store root is created -- see `debug_root()`'s own docstring in `paths.py`.

    **The retention bound below is a soft cap, not a lock-guarded invariant, and that is deliberate.**
    A strict "exactly N files" guarantee needs the list-then-prune sequence serialised against every
    other writer, and the only lock this codebase has (`session_lock`) is keyed by session slug --
    this directory is not one, and taking a new global lock just for a debugging aid is a cost this
    feature does not justify. Every unlink below is `missing_ok=True`, so two processes pruning at
    once at worst transiently retain a few more than `_DEBUG_RETENTION` files -- corrected on the very
    next write, by whichever process happens to run it -- and never race destructively, because
    nothing here is ever read back by a running process; a human reads these by hand.

    **The write and the prune are two separate failure domains, deliberately** (found in review). A
    prune failure -- this codebase already knows a `PermissionError` on an open handle is real and
    platform-specific, invariant 18's own `_replace_with_retry` exists because of it -- must not
    discard the path of a reply the write just saved successfully: that would report "nothing was
    captured" about a reply that genuinely was, which is worse than the soft cap being late for one
    cycle. `test_a_prune_failure_does_not_discard_an_already_saved_reply` pins it.
    """
    try:
        # Ambient on purpose, not threaded from a triggering session's repository: this path may
        # land its debug dump under the wrong workspace on a process serving more than one (#272).
        # `decision: debug-dump-ambient-root`
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
    """Keep the newest `_DEBUG_RETENTION` files, oldest-first by name -- the leading UTC timestamp in
    every filename makes lexicographic and chronological order the same sort. See `_save_failed_reply`
    for why this is a soft cap rather than a locked one."""
    files = sorted(root.glob("*.txt"))
    for stale in files[: max(0, len(files) - _DEBUG_RETENTION)]:
        stale.unlink(missing_ok=True)


# Output-token ceiling per call. Discovery emits a full slot model + questions + summary; on a rich
# multi-feature request that JSON exceeds 8k output tokens and the whole reply is discarded as
# truncated (observed: a messy 5-feature request truncated at 8k). claude-sonnet-5 actually caps at
# 128k output, but this path is a *non-streaming* client.messages.create(), and the SDK raises / risks
# HTTP timeouts above ~16k without streaming — so 16k is the safe ceiling here. It fits a rich
# discovery run with headroom, and you pay only for tokens generated, so raising it costs nothing on
# smaller outputs (and never changes an output that already fit — golden baselines are unaffected).
# Going higher (32k–128k) needs the call switched to streaming; a per-generator budget (the assessment
# needs less than an epic) is a further refinement. One safe ceiling first.
MAX_OUTPUT_TOKENS = 16000


def _record(rec: CallRecord) -> None:
    """Price the call at Anthropic's rates, then file it against whatever ledger is active.

    The one place this provider meets `requivo.usage`, so the vendor's price table is consulted here
    and the ledger stays neutral (#167). Called exactly once per `CallRecord` -- `_complete` builds
    one and reaches one exit with it -- which is why `price_call` needs no first-write-wins guard and
    deliberately has none.
    """
    record_call(price_call(rec))


def _log_completed(rec: CallRecord) -> None:
    """DEBUG: a call returned a validated result. See the module docstring (#435)."""
    logger.debug("provider call completed: operation=%s model=%s attempts=%d latency_ms=%d",
                rec.operation, rec.model, rec.attempts, rec.latency_ms)


def _log_gave_up(rec: CallRecord, reason: str) -> None:
    """WARNING: a call will not be retried again — a transport failure, a truncated reply, or the
    retry loop exhausted. `reason` is a short label, not the full user-facing message: the latter is
    already raised as a structured error, and this line is for an operator scanning logs, not a
    duplicate of that error text (#435)."""
    logger.warning("provider call gave up: operation=%s model=%s attempts=%d latency_ms=%d reason=%s",
                  rec.operation, rec.model, rec.attempts, rec.latency_ms, reason)


def _response_text(resp) -> str:
    """All text blocks of the response, concatenated — skips thinking/tool_use blocks. Joining
    (rather than taking only the first) means a reply split across text blocks isn't silently
    truncated to its opening fragment before JSON extraction."""
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


def _system_blocks(system: str, reuse_system: bool) -> list[dict]:
    """The `system` argument for one request — carrying a cache breakpoint only when something will
    read it.

    A `cache_control` breakpoint bills the block at **1.25x** input to write and **0.1x** to read, so
    it saves money from the second send of a byte-identical prefix and loses ~25% if there is never a
    second send. Which of those it is depends entirely on the caller's loop, and this is a fixed cost
    the caller alone can predict — hence the parameter rather than a rule here.

    It genuinely pays *within* one operation: a JSON retry re-sends the identical system, `converse()`
    runs up to 8 discovery turns off one prompt, and a golden capture runs K of them.

    **The retry case is an accepted cost of `reuse_system=False`, not an oversight.**
    `decision: retry-regression-under-reuse-system-false`

    It cannot pay *across* operations: `build_prompt()` substitutes the shared schema + context cards
    into a **per-operation** template with `{{SCHEMA}}`/`{{CONTEXT}}` near its end, so the shared bulk
    is a cache *suffix* and no breakpoint placement turns it into the *prefix* match caching needs
    (#9). Pinned by `test_cache_breakpoint_rides_a_reused_prefix_and_not_a_single_call` and
    `test_every_generator_drives_a_real_call_without_a_cache_write`.

    Making it pay across operations means moving the shared bulk to the **front** of all eight
    templates. That is a change to what the model reads, so it owes the golden harness a cycle
    (`docs/evaluations.md`) and is deliberately not bundled here.
    """
    # `dict[str, object]`, not the inferred `dict[str, str]` an untyped literal would give this
    # (#271): the value at `"text"` is a `str`, the value at `"cache_control"` is a nested `dict`, and
    # a dict's value type is invariant across every key once inferred -- assigning the nested dict
    # into a `dict[str, str]`-inferred `block` is what pyright refused. `list[dict]` is a wide return
    # annotation on this function already; this is the same looseness stated one level down, at the
    # one dict literal that actually mixes value shapes.
    block: dict[str, object] = {"type": "text", "text": system}
    if reuse_system:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _transport_message(e: Exception) -> str:
    """What to tell the operator about a transport failure, and whether retrying is worth advising.

    Three outcomes rather than one. A credential failure is not transient and must not suggest it
    is; a rate limit is transient but has its own remedy (wait, do not hammer); everything else --
    a connection drop, a timeout, a 5xx -- keeps the original wording, which was right for it all
    along. The SDK's own class name is still included in every branch: it is the only part of this
    that a bug report can be diagnosed from.
    """
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


def _complete(client, system: str, messages: list[dict], out_model, retries: int = 2,
              validate=None, *, reuse_system: bool = True, model: str | None = None,
              operation: str | None = None):
    """One call → validated `out_model`. Retries with a nudge on malformed/non-conformant JSON.
    The nudge lives in a local copy so the caller's clean history is never polluted.

    `operation` is the verb this call is for — `"analyze"`, `"brief"`, `"stories"`, ... the same
    vocabulary `_OP_PROMPTS` already uses — stamped onto the `CallRecord` purely as provenance:
    nothing here branches on it, and `None` (the default) is a legitimate value, not a missing one
    (#435). Pinned by `test_run_stamps_the_analyze_operation_onto_the_call_record` and
    `test_call_record_operation_defaults_to_none`.

    `validate` is an optional semantic post-check `(instance) -> None` that raises `ValueError` to
    reject an output Pydantic accepted but the caller still considers incomplete (e.g. a discovery
    model missing required slots). It rides the same retry loop, so the model self-corrects.

    `reuse_system` is the caller's answer to "will this exact system prompt be sent again inside the
    cache TTL?" — see `_system_blocks`. It defaults to True because that is the safe answer to an
    unknown: mistakenly caching costs 25% once, mistakenly not caching costs the full price of every
    repeat. Only a caller that *knows* it makes one call should say False.

    `model` is the id to call and to bill against — threaded down from `AnthropicProvider(model=...)`,
    or `None` to fall back to `current_model_name()`'s env-chain resolution exactly as before.
    Resolved **once, here**: an explicit id must make zero env reads, so a caller pinning a model can
    share a process with another one without racing the other's `REQUIVO_MODEL` (#434). Pinned by
    `test_a_constructed_model_makes_no_env_read` and
    `test_two_constructed_providers_record_and_price_independently`.

    Every exit records the spend first — see this module's docstring, and `_stop()` below."""
    attempt = messages
    last_err = None
    model = model if model is not None else current_model_name()
    rec = CallRecord(model=model, attempts=0, operation=operation)
    started = time.perf_counter()

    def _stop(msg: str) -> EngineError:
        # Record the spend and stamp latency before surfacing a clean failure — a failed call still
        # billed for whatever it consumed, and the ledger should reflect it. Both *clean* failure
        # exits go through here so there is one place to get it right; the retry give-up below is
        # the third and records inline. See the module docstring, pinned by
        # `test_a_failed_call_is_still_recorded_on_every_exit`.
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
            # Anything from the transport, turned into a clean message instead of a raw traceback.
            # The saved model is untouched (nothing was written yet) on every branch.
            #
            # Branched, because one message for all of them was actively wrong for the most likely
            # failure: `AuthenticationError`, `PermissionDeniedError` and `RateLimitError` are all
            # `APIError` subclasses, so a rejected key was told to "retry the command in a moment"
            # -- advice that never works on a 401 -- with the real cause buried in a parenthetical
            # class name (#201). Retrying is only safe advice where retrying can help.
            # Pinned by `test_an_auth_failure_names_the_key_and_does_not_advise_retry`.
            raise _stop(_transport_message(e)) from e
        except TypeError as e:
            # The belt, and it is a belt rather than the fix: `new_client()` refuses upfront when no
            # credential is visible, so this arm should be unreachable. It exists because the shape
            # of #201 was an SDK raising a *bare builtin* out of its own auth resolution -- not an
            # `APIError`, so the arm above did not see it, and not a `RequivoError`, so `cli.app()`
            # did not either. Whatever the next such change looks like, it stops being a traceback
            # here. Deliberately not narrowed by matching the SDK's wording: a verdict that depends
            # on a foreign string is one that goes quiet when that string is reworded.
            # Pinned by `test_a_typeerror_out_of_the_sdk_is_not_a_traceback`.
            raise _stop(
                f"The Anthropic client could not build the request ({type(e).__name__}: {e}).\n"
                "This is usually an unresolved credential -- check ANTHROPIC_API_KEY, and see "
                "`requivo doctor`. If a working key is set, it is a provider-SDK incompatibility "
                "worth reporting. The model on disk was not modified."
            ) from e
        # Accumulate usage across every attempt — a retry spends tokens too (fields absent on the
        # test fake, so default to 0 and this stays a no-op offline).
        u = getattr(resp, "usage", None)
        if u is not None:
            rec.input_tokens += getattr(u, "input_tokens", 0) or 0
            rec.output_tokens += getattr(u, "output_tokens", 0) or 0
            rec.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
            rec.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        raw = _response_text(resp)
        truncated = getattr(resp, "stop_reason", None) == "max_tokens"
        try:
            result = out_model.model_validate(_extract_json(raw))
            if validate is not None:
                validate(result)
            rec.latency_ms = int((time.perf_counter() - started) * 1000)
            _record(rec)
            _log_completed(rec)
            return result
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
            # A reply cut off at the token ceiling can't be salvaged by retrying (the same ceiling
            # truncates again), so surface it as a clean, specific failure. We check this *only on a
            # parse/validation failure*: a response can hit the ceiling yet still contain complete,
            # valid JSON — those must succeed above, not be rejected. (Rich discovery outputs run
            # right up against the ceiling; MAX_OUTPUT_TOKENS gives them headroom so this stays rare.)
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
    # The final raw reply never validated, and give-up is the one exit where that reply is worth
    # keeping (#283): a user filing "the provider returned output that did not match the contract"
    # otherwise has nothing to attach, and the maintainer cannot tell a prompt regression from a
    # model-side change. `raw` is whatever the last loop iteration set it to -- every iteration that
    # reaches this point in the function has already assigned it, so it is always defined here.
    # Best-effort: `_save_failed_reply` swallows its own failures and returns None rather than ever
    # raising something that shadows the ProviderOutputError below.
    debug_path = _save_failed_reply(raw, out_model.__name__)
    details = {"contract": out_model.__name__, "attempts": retries + 1, "last_error": str(last_err)}
    saved_note = ""
    if debug_path is not None:
        details["raw_reply_path"] = str(debug_path)
        saved_note = f" — the reply that failed validation was saved to {debug_path}"
    # A structured Requivo error, not a bare RuntimeError: exhausting the retry loop is a *known*
    # provider condition with an actionable cause, and every surface catches RequivoError. Raised as
    # anything else, it escaped the CLI's handler and reached the user as a traceback.
    raise ProviderOutputError(
        f"the provider returned output that did not match the {out_model.__name__} contract after "
        f"{retries + 1} attempts — the last failure was: {last_err}{saved_note}",
        details=details,
    ) from last_err
