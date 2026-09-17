"""Offline fakes, model builders and the CLI harness every test module shares (#72, #555, #627)."""
from __future__ import annotations

import io
import json
import shutil
from contextlib import contextmanager, redirect_stdout

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput, _schema_order, schema_slot_ids
from requivo.services.sessions import SessionService

OBJECTIVE = "A leave approval system"


def slot(completeness=0, confidence="empty", impact="low", value="", test_plan=""):
    # `test_plan` only for a `testable` slot, which the contract refuses without one (#610).
    d = {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}
    return {**d, "test_plan": test_plan} if test_plan else d


def full_slots(**overrides) -> dict:
    """Every required slot, empty/low by default and in schema order, with per-slot overrides."""
    _, required = schema_slot_ids()
    model = {sid: slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    return model


def full_model(**overrides) -> dict:
    """A complete proposal: `full_slots` plus the objective `completeness_gap` demands."""
    return {"model": full_slots(**overrides), "questions": [], "summary": {"objective": OBJECTIVE}}


def out(model) -> EngineOutput:
    """An `EngineOutput` over `full_slots(**model)`."""
    return EngineOutput.model_validate(full_model(**model))


# ── a raw Anthropic-SDK-shaped client, so `AnthropicProvider` -> `_complete()` runs unmodified ──


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text, usage=None, stop_reason="end_turn"):
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason
        self.usage = usage


class Spend:
    """The token counts the SDK reports on a response, under the SDK's own attribute names."""

    def __init__(self, input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                 cache_creation_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class FakeClient:
    """Returns canned JSON replies in order; records each create() call's kwargs."""

    def __init__(self, *replies, spend=None):
        self._replies = list(replies)
        self._spend = spend
        self.calls = []
        self.messages = self  # so client.messages.create resolves to self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._replies.pop(0), self._spend)


class RaisingClient:
    """`create()` always raises `exc` (a transport error by default) and counts how often it was reached."""

    def __init__(self, exc=None):
        self._exc = exc
        self.messages = self
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self._exc is None:
            import anthropic
            import httpx
            self._exc = anthropic.APIConnectionError(message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))
        raise self._exc


# ── a `ReasoningProvider` with no vendor behind it ───────────────────────────────


class StubProvider:
    """`analyze` returns the next of `turns` (else a full model) and `generate` returns `artifacts[type]`; both count."""

    name = "stub"

    def __init__(self, *turns: EngineOutput, artifacts: dict | None = None,
                 analyze_error: Exception | None = None, generate_error: Exception | None = None):
        self.turns = list(turns)
        self.artifacts = artifacts or {}
        self._analyze_error, self._generate_error = analyze_error, generate_error
        self.analyze_calls = 0
        self.generate_calls = 0
        self.analyze_kwargs: list[dict] = []

    @property
    def calls(self) -> int:
        return self.analyze_calls + self.generate_calls

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
                perimeter=None):
        self.analyze_calls += 1
        self.analyze_kwargs.append({"request": request, "current_model": current_model, "answers": answers,
                           "only": only, "perimeter": perimeter})
        if self._analyze_error is not None:
            raise self._analyze_error
        if self.turns:
            return self.turns.pop(0)
        return out({"problem": slot(80, "explicit", "high")})

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self.generate_calls += 1
        if self._generate_error is not None:
            raise self._generate_error
        if artifact_type not in self.artifacts:
            raise AssertionError(f"the provider was reached for {artifact_type!r} with model={model!r}")
        return self.artifacts[artifact_type]

    def model_name(self):
        return f"{self.name}-model-1"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": f"sha256:{self.name}"}


# ── canned replies for the `discover` path ───────────────────────────────────────

_ENGINE_REPLY = json.dumps({"model": full_slots(problem=slot(80, "explicit", "high")), "questions": [],
                            "summary": {"objective": "o"}})
# Since #593 a first `discover` with no `--context` judges its grounding before the discovery turn.
_JUDGMENT_REPLY = json.dumps({"decision": "none", "reason": "ordinary software, nothing special"})
# #601: a first `discover` with no explicit `--perimeter` routes before it judges its grounding.
_ROUTING_REPLY = json.dumps({"decision": "none", "reason": "an ordinary request, not a go-to-market plan"})


# ── sessions and the CLI ──────────────────────────────────────────────────────────


def seed_session(slug: str = "leave-approval", request: str | None = None, *, analysed: bool = True,
                 objective: str = OBJECTIVE, **slot_overrides) -> str:
    """A session through the service, with a complete model applied unless `analysed=False`."""
    svc = SessionService()
    svc.create_session(request or f"A request about {slug}", slug=slug)
    if analysed:
        model = {**full_model(**slot_overrides), "summary": {"objective": objective}}
        svc.update_model(slug, json.dumps(model))
    return slug


@contextmanager
def _model_in_out(slug):
    """A canonical .requivo/sessions/<slug>/ session with a model the subcommands can load and mutate (#402)."""
    store.create_session(slug, f"request for {slug}")
    store.save_revision(slug, out({"problem": slot(80, "explicit", "high")}))
    p = store.canonical_dir(slug) / "model.json"
    try:
        yield p
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def run_cli(argv, client=None) -> str:
    """`app()` with stdout captured. client=None is "build the default client", not a poison pill (#419)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        app(argv, client=client)
    return buf.getvalue()


def run_cli_json(argv, client=None):
    return json.loads(run_cli(argv, client))


def run_cli_exit(argv, client=None) -> tuple[str, int]:
    """`run_cli`, returning `(stdout, exit code)` instead of letting a `SystemExit` escape."""
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            app(argv, client=client)
        except SystemExit as e:
            code = int(e.code or 0)
    return buf.getvalue(), code


def run_cli_stdin(argv, text, monkeypatch, client=None) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    return run_cli(argv, client)


def forge_meta(slug: str, fields: dict) -> None:
    """Write arbitrary values into a session's `session.json`, the way an imported archive can."""
    p = store.canonical_dir(slug) / "session.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta.update(fields)
    p.write_text(json.dumps(meta), encoding="utf-8")


_run_app = run_cli
