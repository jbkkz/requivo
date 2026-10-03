"""The Anthropic provider call: JSON extraction, retry classification, the debug capture on give-up, the
context-card threading, which model id a call uses, and the streamed call (#72, #201, #268, #283, #434, #638)."""
import importlib
import io
import json
from contextlib import redirect_stdout

import anthropic
import httpx
import pytest
from _fakes import _ENGINE_REPLY, FakeClient, RaisingClient, full_slots, out, slot

from requivo.core.contracts import PRD, EngineOutput, Stories
from requivo.core.errors import ProviderOutputError
from requivo.core.persistence import load_model
from requivo.providers.anthropic import (
    AnthropicProvider,
    advise,
    answer_turn,
    current_model_name,
    derive_stories,
    generate_prd,
    run,
)
from requivo.providers.anthropic import completion as completion_module
from requivo.providers.anthropic.completion import _complete, _extract_json, _response_text
from requivo.providers.errors import EngineError
from requivo.render.markdown import prd_markdown
from requivo.render.terminal import render_stories
from requivo.usage import track_usage

_USER = [{"role": "user", "content": "leave approval"}]
_MODEL = out({"problem": slot(80, "explicit", "high")})
_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})
_INCOMPLETE_REPLY = json.dumps({"model": {"problem": slot(80, "explicit", "high")},
                                "questions": [], "summary": {"objective": "o"}})


class _MaxTokensClient(FakeClient):
    """`FakeClient`, every reply flagged as cut off at the token ceiling."""

    def reply(self, **kwargs):
        reply = super().reply(**kwargs)
        reply.stop_reason = "max_tokens"
        return reply


def _complete_failing_with(exc) -> str:
    with pytest.raises(EngineError) as ei:
        _complete(RaisingClient(exc), "sys", _USER, EngineOutput)
    return str(ei.value)


# ── the reply's text, and the JSON in it ────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ('```json\n{"a": 1}\n```', {"a": 1}), ('here it is: {"b": 2} — done', {"b": 2}), ("no json object anywhere", None),
])
def test_extract_json_strips_a_fence_slices_prose_and_refuses_garbage(raw, expected):
    if expected is None:
        with pytest.raises(ValueError):
            _extract_json(raw)
    else:
        assert _extract_json(raw) == expected


_F = "`" * 3


@pytest.mark.parametrize("raw, expected", [
    ('{"a": "see:\\n' + _F + 'sql\\nselect 1;\\n' + _F + '\\n"}', {"a": f"see:\n{_F}sql\nselect 1;\n{_F}\n"}),
    (_F + 'json\n{"a": "x ' + _F + 'py\\nprint(1)\\n' + _F + ' y"}\n' + _F, {"a": f"x {_F}py\nprint(1)\n{_F} y"}),
])
def test_extract_json_keeps_a_code_fence_inside_a_string_value(raw, expected):
    """#646: a fence inside a JSON string is content, not the reply's own fence."""
    assert _extract_json(raw) == expected


def test_response_text_concatenates_text_blocks_and_skips_others():
    class _Block:
        def __init__(self, type_, text=""):
            self.type, self.text = type_, text

    class _Resp:
        content = [_Block("thinking", "IGNORE"), _Block("text", "abc"), _Block("text", "def")]

    assert _response_text(_Resp()) == "abcdef"


# ── the discovery turn and the generators, against a canned client ──────────────


def test_run_returns_engine_output_and_wires_schema_and_context():
    fake = FakeClient(_ENGINE_REPLY)
    result = run(fake, _USER)
    assert isinstance(result, EngineOutput) and result.model["problem"].completeness == 80
    block = fake.calls[0]["system"][0]
    assert block["cache_control"] == {"type": "ephemeral"}
    assert "slots" in block["text"] and "## b2b-platform" in block["text"]  # {{SCHEMA}} and {{CONTEXT}}


def test_run_rejects_a_model_missing_required_slots(workspace):
    """A discovery reply missing a required slot is refused with a stable code, after every retry."""
    fake = FakeClient(_INCOMPLETE_REPLY, _INCOMPLETE_REPLY, _INCOMPLETE_REPLY)
    with pytest.raises(ProviderOutputError, match="missing required slots") as exc:
        run(fake, _USER)
    assert exc.value.to_dict()["code"] == "provider_output_invalid"
    assert exc.value.details["attempts"] == 3


def test_run_self_heals_when_a_retry_completes_the_model():
    fake = FakeClient(_INCOMPLETE_REPLY, _ENGINE_REPLY)
    assert run(fake, _USER).model["problem"].completeness == 80
    assert len(fake.calls) == 2
    assert "missing required slots" in fake.calls[1]["messages"][-1]["content"]  # the nudge names them


def test_generate_prd_from_saved_model_roundtrip(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(_MODEL.model_dump_json(), encoding="utf-8")
    loaded = load_model(path)
    assert loaded.model["problem"].completeness == 80
    prd = generate_prd(FakeClient(json.dumps({"title": "Leave approval", "problem": "Approvals are lost in email."})), loaded)
    assert isinstance(prd, PRD) and prd.title == "Leave approval"
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval") and "generated by Requivo" in md


def test_derive_stories_returns_structured_stories():
    stories = derive_stories(FakeClient(json.dumps({"stories": [{"id": "S1", "title": "Submit a leave request"}]})), _MODEL)
    assert isinstance(stories, Stories) and [s.id for s in stories.stories] == ["S1"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_stories(stories)
    assert "=== USER STORIES ===" in buf.getvalue() and "[S1] Submit a leave request" in buf.getvalue()


@pytest.mark.parametrize("call, only, absent", [
    (lambda fake: run(fake, _USER, only=["b2b-platform"]), "b2b-platform", "financial-reporting"),
    (lambda fake: answer_turn(fake, _MODEL, "req", "answers", only=["event-ops"]), "event-ops", "financial-reporting"),
    (lambda fake: advise(fake, _MODEL, only=["financial-reporting"]), "financial-reporting", "b2b-platform"),
], ids=["run", "answer_turn", "generator"])
def test_the_context_card_selection_threads_through_every_call(call, only, absent):
    """`--context` reaches `load_context()` from the discovery, a refinement turn and a generator alike."""
    fake = FakeClient(_ENGINE_REPLY if only != "financial-reporting" else _BRIEF_REPLY)
    call(fake)
    system = fake.calls[0]["system"][0]["text"]
    assert f"## {only}" in system and f"## {absent}" not in system


# ── #201: which transport failures are worth retrying, and which are not ─────────


def _api_status(cls, status: int):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message="boom", response=httpx.Response(status, request=request), body=None)


def test_complete_wraps_api_errors_as_a_clean_engine_error():
    assert "not modified" in _complete_failing_with(None)  # the reassurance that nothing was written


@pytest.mark.parametrize("cls, status", [(anthropic.AuthenticationError, 401), (anthropic.PermissionDeniedError, 403)])
def test_an_auth_failure_names_the_key_and_does_not_advise_retry(cls, status):
    """The remedy is the message's whole job; retrying a rejected credential never helps (#201)."""
    msg = _complete_failing_with(_api_status(cls, status))
    assert "ANTHROPIC_API_KEY" in msg and "requivo doctor" in msg and cls.__name__ in msg
    assert "Retry the command in a moment" not in msg and "not modified" in msg


def test_a_rate_limit_says_so_and_does_not_send_the_operator_straight_back():
    msg = _complete_failing_with(_api_status(anthropic.RateLimitError, 429))
    assert "rate-limited" in msg and "Wait for the limit to reset" in msg
    assert "ANTHROPIC_API_KEY" not in msg, "a rate limit is not a credential problem"


def test_a_connection_failure_keeps_the_wording_that_was_right_for_it():
    msg = _complete_failing_with(None)
    assert "Anthropic API unavailable" in msg and "Retry the command in a moment" in msg
    assert "ANTHROPIC_API_KEY" not in msg


def test_a_typeerror_out_of_the_sdk_is_not_a_traceback():
    """The belt against the SDK's own `TypeError` on an unresolvable auth method (#201)."""
    msg = _complete_failing_with(TypeError("Could not resolve authentication method."))
    assert "TypeError" in msg and "ANTHROPIC_API_KEY" in msg and "not modified" in msg


# ── #638: a reply slower than an idle-cutting proxy, through the real SDK over a fake network path ──

_IDLE_CUT_S, _GENERATION_S, _EVENT_EVERY_S = 60, 90, 5
_SERVER_USAGE = {"input_tokens": 1000, "output_tokens": 1, "cache_read_input_tokens": 500,
                 "cache_creation_input_tokens": 200}


def _sse(kind: str, data: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()


def _idle_cutting_client(reply: str, *, cut_after_events=None):
    """A real `Anthropic` client whose path drops a connection silent for over `_IDLE_CUT_S`, in simulated time:
    unstreamed, nothing crosses until the `_GENERATION_S` reply is written; streamed, headers go at once and an
    event every `_EVENT_EVERY_S`. `cut_after_events` drops the connection mid-stream instead."""
    http = importlib.import_module(anthropic.DefaultHttpxClient.__mro__[1].__module__.split(".")[0])
    step = len(reply) // (_GENERATION_S // _EVENT_EVERY_S) + 1
    events = [
        _sse("message_start", {"type": "message_start", "message": {
            "id": "msg_638", "type": "message", "role": "assistant", "model": "claude-sonnet-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": _SERVER_USAGE}}),
        _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
        *(_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "text_delta", "text": reply[i:i + step]}})
          for i in range(0, len(reply), step)),
        _sse("ping", {"type": "ping"}),
        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 4200}}),
        _sse("message_stop", {"type": "message_stop"}),
    ]

    def body():
        for n, event in enumerate(events):
            if n == cut_after_events:
                raise http.RemoteProtocolError("peer closed connection without sending complete message body")
            yield event

    def handle(request):
        streamed = json.loads(request.content).get("stream") is True
        if (_EVENT_EVERY_S if streamed else _GENERATION_S) > _IDLE_CUT_S:
            raise http.RemoteProtocolError("Server disconnected without sending a response.")
        return http.Response(200, headers={"content-type": "text/event-stream"}, content=body())

    return anthropic.Anthropic(api_key="sk-test-638", base_url="https://api.anthropic.test", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=http.MockTransport(handle)))


def test_a_reply_slower_than_an_idle_cut_still_arrives():
    """Streamed, the reply arrives and its usage is filed as the server reported it (#638)."""
    client = _idle_cutting_client(_ENGINE_REPLY)
    with pytest.raises(anthropic.APIConnectionError):  # MUST FIRE: the same path cuts an unstreamed call
        client.messages.create(model="claude-sonnet-5", max_tokens=16, messages=_USER)
    with track_usage() as ledger:
        result = _complete(client, "sys", _USER, EngineOutput, model="claude-sonnet-5")
    assert result.model["problem"].completeness == 80
    assert (ledger.input_tokens, ledger.output_tokens, ledger.cache_read_tokens, ledger.cache_write_tokens) == (
        1000, 4200, 500, 200)


def test_a_connection_cut_mid_stream_is_a_clean_recorded_failure():
    """The SDK does not wrap a transport error raised once a stream has started (#638)."""
    with track_usage() as ledger, pytest.raises(EngineError) as ei:
        _complete(_idle_cutting_client(_ENGINE_REPLY, cut_after_events=3), "sys", _USER, EngineOutput)
    assert "Anthropic API unavailable" in str(ei.value) and "RemoteProtocolError" in str(ei.value)
    assert len(ledger.calls) == 1 and ledger.calls[0].attempts == 1


def test_complete_rejects_a_truncated_reply_that_fails_to_parse():
    with pytest.raises(EngineError, match="max_tokens"):
        _complete(_MaxTokensClient('{"model": {"problem":'), "sys", _USER, EngineOutput)


def test_complete_accepts_a_max_tokens_reply_whose_json_is_complete():
    """Parse-first: a reply flagged `max_tokens` whose JSON is nonetheless complete still succeeds."""
    complete = json.dumps({"model": full_slots(problem=slot(80, "explicit", "high")), "questions": [], "summary": {}})
    assert _complete(_MaxTokensClient(complete), "sys", _USER, EngineOutput).model["problem"].completeness == 80


def test_a_give_up_with_no_attempt_is_a_structured_error_not_an_unbound_local():
    """`retries=-1` runs no attempt, so no `raw` reply was ever bound (#667)."""
    with pytest.raises(ProviderOutputError):
        _complete(FakeClient(_ENGINE_REPLY), "sys", _USER, EngineOutput, retries=-1)


# ── #283: the malformed reply survives retry give-up ──────────────────────────────


def test_a_retry_give_up_saves_the_final_raw_reply_and_names_it_in_the_error(workspace):
    bad_reply = '{"not": "an engine output"}'
    with pytest.raises(ProviderOutputError) as exc:
        run(FakeClient(bad_reply, bad_reply, bad_reply), _USER)
    saved = list((workspace / ".requivo" / "debug").glob("*.txt"))
    assert len(saved) == 1, "the give-up exit must write exactly one debug file"
    assert saved[0].read_text(encoding="utf-8") == bad_reply, "the exact final raw reply, byte for byte"
    assert str(saved[0]) in str(exc.value) and exc.value.details.get("raw_reply_path") == str(saved[0])


@pytest.mark.parametrize("client", [lambda: FakeClient(_ENGINE_REPLY), RaisingClient], ids=["success", "transport-failure"])
def test_only_a_retry_give_up_writes_a_debug_file(workspace, client):
    """A success has nothing to debug; a transport failure has no raw reply to save (#283)."""
    try:
        run(client(), _USER)
    except EngineError:
        pass
    assert not (workspace / ".requivo" / "debug").exists()


def test_a_prune_failure_does_not_discard_an_already_saved_reply(workspace, monkeypatch):
    """`_prune_debug_dir`'s `unlink` calls carried no exception handling of their own (#283 review)."""
    def _raise(root):
        raise PermissionError("locked by another process")

    monkeypatch.setattr(completion_module, "_prune_debug_dir", _raise)
    path = completion_module._save_failed_reply("some raw reply text", "EngineOutput")
    assert path is not None and path.read_text(encoding="utf-8") == "some raw reply text"


@pytest.mark.parametrize("off", ["0", "false", "FALSE", "off", "no"])
def test_failed_reply_dumps_can_be_turned_off_and_leave_no_path(workspace, monkeypatch, off):
    """#693: an embedder serving several workspaces must be able to keep request text out of any dump."""
    monkeypatch.setenv("REQUIVO_DEBUG_DUMPS", off)
    bad_reply = '{"not": "an engine output"}'
    with pytest.raises(ProviderOutputError) as exc:
        run(FakeClient(bad_reply, bad_reply, bad_reply), _USER)
    assert not (workspace / ".requivo" / "debug").exists()
    assert not list(workspace.rglob("debug"))
    assert "raw_reply_path" not in exc.value.details
    assert "saved to" not in str(exc.value)


@pytest.mark.parametrize("on", ["", "1", "true", "anything-else"])
def test_failed_reply_dumps_stay_on_unless_explicitly_turned_off(workspace, monkeypatch, on):
    """Positive control for the opt-out: only a recognised 'off' value disables the dump (#693)."""
    monkeypatch.setenv("REQUIVO_DEBUG_DUMPS", on)
    assert completion_module._save_failed_reply("raw", "EngineOutput") is not None


# ── #268: which model id a call uses, and #434: a constructor-level override ─────


def test_current_model_name_reads_env_override(monkeypatch):
    monkeypatch.delenv("REQUIVO_MODEL", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    assert current_model_name() == "claude-sonnet-5"


def test_current_model_name_prefers_requivo_model_over_the_default(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"


def test_current_model_name_falls_back_to_bare_model(monkeypatch):
    """The bare `MODEL` name predates `REQUIVO_MODEL` and existing setups keep working (#268)."""
    monkeypatch.delenv("REQUIVO_MODEL", raising=False)
    monkeypatch.setenv("MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"


def test_current_model_name_prefers_requivo_model_when_both_are_set(monkeypatch):
    """A shell exporting a generic `MODEL` for another tool must not steer Requivo once `REQUIVO_MODEL` is set (#268)."""
    monkeypatch.setenv("MODEL", "some-other-tools-model")
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"


def test_two_constructed_providers_record_and_price_independently(monkeypatch):
    """Two `AnthropicProvider`s in one process, each billed at its own model's rate (#434)."""
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")  # ambient value neither provider should use
    sonnet = AnthropicProvider(FakeClient(_ENGINE_REPLY), model="claude-sonnet-5")
    haiku = AnthropicProvider(FakeClient(_ENGINE_REPLY), model="claude-haiku-4-5")
    with track_usage() as ledger:
        sonnet.analyze("leave approval")
        haiku.analyze("leave approval")
    assert [c.model for c in ledger.calls] == ["claude-sonnet-5", "claude-haiku-4-5"]
    sonnet_rate, haiku_rate = ledger.calls[0].rate_per_mtok, ledger.calls[1].rate_per_mtok
    assert sonnet_rate is not None and haiku_rate is not None and sonnet_rate != haiku_rate


def test_a_constructed_model_makes_no_env_read(monkeypatch):
    """A constructed id wins over the ambient `REQUIVO_MODEL`; default construction still reads it (#434)."""
    monkeypatch.setenv("REQUIVO_MODEL", "claude-opus-4-8")
    constructed = AnthropicProvider(FakeClient(_ENGINE_REPLY), model="claude-haiku-4-5")
    constructed.analyze("leave approval")
    assert constructed.client.calls[0]["model"] == "claude-haiku-4-5" == constructed.model_name()
    ambient = AnthropicProvider(FakeClient(_ENGINE_REPLY))  # MUST FIRE: the positive control
    ambient.analyze("leave approval")
    assert ambient.client.calls[0]["model"] == "claude-opus-4-8" == ambient.model_name()


def test_default_construction_is_byte_identical_to_before_434(monkeypatch):
    monkeypatch.setenv("REQUIVO_MODEL", "claude-haiku-4-5")
    fake = FakeClient(_ENGINE_REPLY)
    provider = AnthropicProvider(fake)
    provider.analyze("leave approval")
    assert fake.calls[0]["model"] == current_model_name() == "claude-haiku-4-5" == provider.model_name()


def test_generate_threads_the_constructed_model_too():
    fake = FakeClient(_BRIEF_REPLY)
    AnthropicProvider(fake, model="claude-haiku-4-5").generate("brief", _MODEL)
    assert fake.calls[0]["model"] == "claude-haiku-4-5"
