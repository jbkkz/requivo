"""Which transport failures are worth retrying and which are not, and the parse-first rule for a
reply flagged `max_tokens`: a reply whose JSON is nonetheless complete still succeeds.

Split out of `test_provider.py` (#555) once that file grew past the module ceiling.
"""
import json

import anthropic
import httpx
import pytest
from _fakes import _FakeBlock, full_slots, slot

from requivo.core.contracts import EngineOutput
from requivo.providers.anthropic.completion import _complete
from requivo.providers.errors import EngineError


class _RaisingClient:
    """A client whose create() raises -- to exercise the API-error boundary in _complete()."""

    def __init__(self, exc):
        self._exc = exc
        self.messages = self

    def create(self, **kwargs):
        raise self._exc


# ── #201: which transport failures are worth retrying, and which are not ─────


def _api_status(cls, status: int):
    """An SDK status error of `cls`, built the way the SDK builds one."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return cls(message="boom", response=response, body=None)


def _complete_failing_with(exc):
    with pytest.raises(EngineError) as ei:
        _complete(_RaisingClient(exc), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    return str(ei.value)


def test_an_auth_failure_names_the_key_and_does_not_advise_retry():
    """The message that was actively wrong, and the assertion that keeps it from coming back.

    `AuthenticationError`, `PermissionDeniedError` and `RateLimitError` are all `APIError`
    subclasses, so one `except APIError` arm answered a rejected key with "Retry the command in a
    moment" -- advice that never works on a 401, given to the single most likely failure of a fresh
    install, with the real cause reduced to a parenthetical class name (#201).

    The negative half is the load-bearing half: it is easy to add the key remedy and leave the retry
    sentence sitting underneath it, which reads as "your key is wrong, try again".
    """
    for cls in (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        msg = _complete_failing_with(_api_status(cls, 401 if cls is anthropic.AuthenticationError else 403))
        assert "ANTHROPIC_API_KEY" in msg, "the remedy is the message's whole job"
        assert "requivo doctor" in msg
        assert cls.__name__ in msg, "the SDK class is what a bug report is diagnosed from"
        assert "Retry the command in a moment" not in msg, (
            "retrying a rejected credential never helps, and saying so wastes the one line the "
            "operator reads"
        )
        assert "not modified" in msg


def test_a_rate_limit_says_so_and_does_not_send_the_operator_straight_back():
    msg = _complete_failing_with(_api_status(anthropic.RateLimitError, 429))
    assert "rate-limited" in msg
    assert "ANTHROPIC_API_KEY" not in msg, "a rate limit is not a credential problem"
    assert "Wait for the limit to reset" in msg


def test_a_connection_failure_keeps_the_wording_that_was_right_for_it():
    """The third branch exists to leave something alone.

    Splitting an over-general message is only an improvement if the case it was actually correct
    for still gets it: a connection drop, a timeout or a 5xx *is* transient, and "retry in a moment"
    is the right thing to say.
    """
    exc = anthropic.APIConnectionError(message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))
    msg = _complete_failing_with(exc)
    assert "Anthropic API unavailable" in msg
    assert "Retry the command in a moment" in msg
    assert "ANTHROPIC_API_KEY" not in msg


def test_a_typeerror_out_of_the_sdk_is_not_a_traceback():
    """The belt, and the shape of the defect it is a belt against.

    #201 was an SDK raising a bare `TypeError` out of its own auth resolution: not an `APIError`, so
    `_complete`'s transport arm did not see it, and not a `RequivoError`, so `cli.app()` did not
    either. It threaded through every handler in the product and reached the operator as twenty-five
    lines of stack. `new_client()` refuses upfront now, so this arm should be unreachable -- which is
    the point of testing it, because an unreachable arm nobody exercises is one that rots.
    """
    msg = _complete_failing_with(TypeError("Could not resolve authentication method."))
    assert "TypeError" in msg
    assert "ANTHROPIC_API_KEY" in msg
    assert "not modified" in msg


class _MaxTokensClient:
    """Returns a reply flagged as cut off at the token ceiling (stop_reason == 'max_tokens'),
    carrying whatever text it is given — so we can exercise both the broken- and complete-JSON cases."""

    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        text = self._text

        class _Resp:
            stop_reason = "max_tokens"
            content = [_FakeBlock(text)]
        return _Resp()


def test_complete_rejects_a_truncated_reply_that_fails_to_parse():
    # Genuine truncation: the JSON is cut off mid-object, so parsing fails and the ceiling is the
    # named cause — retrying at the same ceiling wouldn't help, so it fails fast and cleanly.
    client = _MaxTokensClient('{"model": {"problem":')
    with pytest.raises(EngineError) as ei:
        _complete(client, "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert "max_tokens" in str(ei.value)


def test_complete_accepts_a_max_tokens_reply_whose_json_is_complete():
    # Parse-first: rich discovery outputs run right against the ceiling and can be flagged max_tokens
    # while still carrying complete, valid JSON. That must succeed — not be rejected as truncated.
    complete = json.dumps({"model": full_slots(problem=slot(80, "explicit", "high")),
                           "questions": [], "summary": {}})
    result = _complete(_MaxTokensClient(complete), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert result.model["problem"].completeness == 80
