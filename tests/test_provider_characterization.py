"""The provider call's baseline behaviour: JSON extraction from a reply, and characterization of
discovery/generator/error/context-card plumbing against a canned client.

Split out of `test_engine.py` (#72), then split again out of `test_provider.py` (#555) once that
file grew past the module ceiling. No real network: `FakeClient` returns canned replies in order and
records the request that came out, so each test asserts on what would have been sent.
"""
import io
import json
from contextlib import redirect_stdout

import anthropic
import httpx
import pytest
from _fakes import _ENGINE_REPLY, FakeClient, out, slot

from requivo.core.contracts import PRD, EngineOutput, Stories
from requivo.core.persistence import load_model
from requivo.providers.anthropic import advise, answer_turn, derive_stories, generate_prd, run
from requivo.providers.anthropic.completion import _complete, _extract_json, _response_text
from requivo.providers.errors import EngineError
from requivo.render.markdown import prd_markdown
from requivo.render.terminal import render_stories

# ── JSON extraction ──────────────────────────────────────────────────────────


def test_extract_json_strips_fence():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_slices_surrounding_text():
    assert _extract_json('here it is: {"b": 2} — done') == {"b": 2}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        _extract_json("no json object anywhere")


# ── Characterization: discovery, generators, errors, context ─────────────────
# These pin CURRENT behavior (shapes, formats, error surfaces). They are not quality tests.


def test_run_returns_engine_output_and_wires_schema_and_context():
    # The --once discovery pass is a single run() call. Characterize its result
    # AND that the engine turn is driven by prompts/engine.md with schema + context injected.
    fake = FakeClient(_ENGINE_REPLY)
    result = run(fake, [{"role": "user", "content": "leave approval"}])
    assert isinstance(result, EngineOutput)
    assert result.model["problem"].completeness == 80
    # system is a cache-controlled text block so its stable prefix is cached across calls.
    block = fake.calls[0]["system"][0]
    assert block["cache_control"] == {"type": "ephemeral"}
    system = block["text"]
    assert "slots" in system              # assets/perimeters/software/model_schema.json injected ({{SCHEMA}})
    assert "## b2b-platform" in system    # context card injected ({{CONTEXT}})


def test_run_rejects_a_model_missing_required_slots(tmp_path, monkeypatch):
    # A discovery reply missing a required slot is refused: the completeness invariant is enforced at
    # the boundary. The FakeClient returns the same incomplete reply every retry, so run() gives up.
    from requivo.core.errors import ProviderOutputError

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    incomplete = json.dumps({
        "model": {"problem": slot(80, "explicit", "high")},  # 1 of 15 required
        "questions": [], "summary": {"objective": "o"},
    })
    fake = FakeClient(incomplete, incomplete, incomplete)  # every retry attempt
    # A RequivoError with a stable code, not a bare RuntimeError: the CLI's handler catches the former
    # and prints a clean message, and lets the latter through as a traceback.
    with pytest.raises(ProviderOutputError, match="missing required slots") as exc:
        run(fake, [{"role": "user", "content": "leave approval"}])
    assert exc.value.to_dict()["code"] == "provider_output_invalid"
    assert exc.value.details["attempts"] == 3


def test_run_self_heals_when_a_retry_completes_the_model():
    # The completeness check rides the existing retry loop: a first incomplete reply nudges the model,
    # and a complete reply on the next attempt is accepted. This is why the invariant is safe to
    # enforce on a non-deterministic model — an omission is corrected, not fatal.
    incomplete = json.dumps({
        "model": {"problem": slot(80, "explicit", "high")},
        "questions": [], "summary": {"objective": "o"},
    })
    fake = FakeClient(incomplete, _ENGINE_REPLY)  # 1st attempt short, 2nd complete
    result = run(fake, [{"role": "user", "content": "leave approval"}])
    assert result.model["problem"].completeness == 80
    assert len(fake.calls) == 2  # it took a retry
    # the nudge names the missing slots so the model knows what to add
    nudge = fake.calls[1]["messages"][-1]["content"]
    assert "missing required slots" in nudge


def test_generate_prd_from_saved_model_roundtrip(tmp_path):
    # The --from path: reload a saved model and regenerate an artifact, no discovery.
    model = out({"problem": slot(80, "explicit", "high")})
    path = tmp_path / "model.json"
    path.write_text(model.model_dump_json())

    loaded = load_model(path)
    assert loaded.model["problem"].completeness == 80

    prd = generate_prd(FakeClient(json.dumps({"title": "Leave approval", "problem": "Approvals are lost in email."})), loaded)
    assert isinstance(prd, PRD) and prd.title == "Leave approval"
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval")
    assert "generated by Requivo" in md


def test_derive_stories_returns_structured_stories():
    reply = json.dumps({"stories": [{"id": "S1", "title": "Submit a leave request"}]})
    stories = derive_stories(FakeClient(reply), out({"problem": slot(80, "explicit", "high")}))
    assert isinstance(stories, Stories)
    assert [s.id for s in stories.stories] == ["S1"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_stories(stories)
    text = buf.getvalue()
    assert "=== USER STORIES ===" in text and "[S1] Submit a leave request" in text


def test_run_restricts_context_cards_when_only_given():
    # The --context selection threads run() → build_prompt() → load_context(): the assembled system
    # carries only the chosen card, so it can't dilute impact estimation with the others.
    fake = FakeClient(_ENGINE_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}], only=["b2b-platform"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## b2b-platform" in system
    assert "## financial-reporting" not in system


def test_answer_turn_threads_the_discovery_context_cards():
    # A refinement turn must reason over the same cards the original discovery used, not silently all.
    fake = FakeClient(_ENGINE_REPLY)
    answer_turn(fake, out({"problem": slot(80, "explicit", "high")}), "req", "answers",
                only=["event-ops"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## event-ops" in system
    assert "## financial-reporting" not in system


def test_generators_thread_the_context_selection():
    # A generator grounds its artifact in the discovery's card subset, not the full set.
    fake = FakeClient(json.dumps({"complexity": "low", "solution": "S"}))
    advise(fake, out({"problem": slot(80, "explicit", "high")}), only=["financial-reporting"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## financial-reporting" in system
    assert "## b2b-platform" not in system


def test_response_text_concatenates_text_blocks_and_skips_others():
    class _Block:
        def __init__(self, type_, text=""):
            self.type = type_
            self.text = text

    class _Resp:
        content = [_Block("thinking", "IGNORE"), _Block("text", "abc"), _Block("text", "def")]

    assert _response_text(_Resp()) == "abcdef"


class _RaisingClient:
    """A client whose create() raises — to exercise the API-error boundary in _complete()."""

    def __init__(self, exc):
        self._exc = exc
        self.messages = self

    def create(self, **kwargs):
        raise self._exc


def test_complete_wraps_api_errors_as_a_clean_engine_error():
    exc = anthropic.APIConnectionError(message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(EngineError) as ei:
        _complete(_RaisingClient(exc), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert "not modified" in str(ei.value)  # the reassurance that nothing was written
