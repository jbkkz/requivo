"""Offline fakes, model builders and the CLI harness every test module shares (#72, #555, #627)."""
from __future__ import annotations

import io
import json
import os
import shutil
import threading
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput, _schema_order, schema_slot_ids
from requivo.core.errors import RevisionConflictError, SessionExistsError, SessionNotFoundError
from requivo.core.persistence import ArtifactStatus, RevisionRecord, SessionMeta
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


class _FakeStream:
    """What `messages.stream(...)` returns: the request is sent on `__enter__`, as the SDK sends it, and the
    reply is read through `get_final_message()`."""

    def __init__(self, send):
        self._send, self._final = send, None

    def __enter__(self):
        self._final = self._send()
        return self

    def __exit__(self, *exc):
        return None

    def get_final_message(self):
        return self._final


class FakeMessages:
    """`client.messages` with `stream()` alone, so a call regressed to the unstreamed `create()` fails every
    fake (#638). `reply(**kwargs)` is the server's answer, or raises its failure."""

    def __init__(self, reply):
        self._reply = reply

    def stream(self, **kwargs):
        return _FakeStream(lambda: self._reply(**kwargs))


class FakeClient:
    """Returns canned JSON replies in order; records each request's kwargs."""

    def __init__(self, *replies, spend=None, stop_reason="end_turn"):
        self._replies = list(replies)
        self._spend, self._stop_reason = spend, stop_reason
        self.calls = []
        self.messages = FakeMessages(self.reply)

    def reply(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._replies.pop(0), self._spend, self._stop_reason)


class RaisingClient:
    """Every request raises `exc` (a transport error by default) and counts how often it was reached."""

    def __init__(self, exc=None):
        self._exc = exc
        self.messages = FakeMessages(self.reply)
        self.calls = 0

    def reply(self, **kwargs):
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
                                    "only": only, "perimeter": perimeter, "reuse_system": reuse_system})
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


def engine_reply(*, converged: bool = False, questions: list[dict] | None = None, objective: str = OBJECTIVE,
                 **slot_overrides) -> str:
    """One discovery reply as the SDK would carry it: a full model plus one question unless `converged`."""
    if questions is None:
        questions = [] if converged else [
            {"q": "How are exceptions handled?", "slot": "business_rules", "why": "uncertainty × impact"}]
    return json.dumps({"model": full_slots(**slot_overrides), "questions": questions,
                       "summary": {"objective": objective}})


# ── sessions and the CLI ──────────────────────────────────────────────────────────


def seeded(sessions: SessionService, request: str = "A leave approval system.", **slots) -> str:
    """A session at revision 1 through the given service; returns its slug."""
    meta = sessions.create_session(request)
    sessions.update_model(meta.slug, out({"problem": slot(80, "explicit", "high"), **slots}).model_dump_json())
    return meta.slug


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


def run_cli_fails(argv, client=None) -> tuple[int, str]:
    """`app()` under `SystemExit`: the exit code and what reached stderr."""
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err), pytest.raises(SystemExit) as e:
        app(argv, client=client)
    return int(e.value.code or 0), err.getvalue()


def run_cli_stdin(argv, text, monkeypatch, client=None) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    return run_cli(argv, client)


def blind_to_session(monkeypatch, slug: str) -> None:
    """Metadata access fails on this session's `session.json` alone: existence undeterminable (#589, #636)."""
    marker, original = store.canonical_dir(slug) / "session.json", Path.stat

    def stat(path, *args, **kwargs):
        if path == marker:
            raise PermissionError(13, "permission denied", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)


def tree_bytes(root: Path) -> dict:
    # Bypass the patched Path.stat in blind_to_session while still comparing every file's bytes.
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if os.path.isfile(p)}


def forge_meta(slug: str, fields: dict) -> None:
    """Write arbitrary values into a session's `session.json`, the way an imported archive can."""
    p = store.canonical_dir(slug) / "session.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta.update(fields)
    p.write_text(json.dumps(meta), encoding="utf-8")


def printed(fn, *args, **kwargs) -> str:
    """What `fn(*args, **kwargs)` wrote to stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


# ── the filesystem saying no ──────────────────────────────────────────────────────


def replace_fails(monkeypatch, denials: int | None) -> dict:
    """`Path.replace` raises `PermissionError` on the first `denials` calls (every call when None); returns the counter."""
    attempts = {"n": 0}
    real_replace = Path.replace

    def flaky(self, dst):
        attempts["n"] += 1
        if denials is None or attempts["n"] <= denials:
            raise PermissionError(13, "Access is denied")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky)
    return attempts


def simulate_py314_denied_path(monkeypatch, denied: Path) -> None:
    """Model 3.14's false-returning pathlib queries while metadata still raises."""
    original_stat = Path.stat
    original_exists = Path.exists
    original_is_file = Path.is_file
    original_is_dir = Path.is_dir
    original_is_symlink = Path.is_symlink

    def stat(self, *args, **kwargs):
        if self == denied:
            raise PermissionError(13, "Permission denied", str(denied))
        return original_stat(self, *args, **kwargs)

    def exists(self, *args, **kwargs):
        return False if self == denied else original_exists(self, *args, **kwargs)

    def is_file(self, *args, **kwargs):
        return False if self == denied else original_is_file(self, *args, **kwargs)

    def is_dir(self, *args, **kwargs):
        return False if self == denied else original_is_dir(self, *args, **kwargs)

    def is_symlink(self, *args, **kwargs):
        return False if self == denied else original_is_symlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)


def deny_access(d: Path, request, untested: str) -> Path:
    """`chmod 000` a directory so a probe into it raises, or skip naming what went untested."""
    request.addfinalizer(lambda: d.chmod(0o755))
    if os.name == "nt":
        pytest.skip(f"POSIX mode bits do not deny traversal on Windows. UNTESTED HERE: {untested}")
    d.chmod(0o000)
    try:
        (d / "session.json").stat()
    except PermissionError:
        return d
    pytest.skip(f"chmod 000 did not deny the probe on this run (running as root?). UNTESTED HERE: {untested}")


_run_app = run_cli


class InMemorySessionRepository:
    """A dict-backed SessionRepository — no filesystem, no `.requivo/` directory (#424)."""

    def __init__(self):
        self._meta: dict = {}
        self._model: dict = {}
        self._revs: dict = {}      # (slug, revision) → model, the history a file backing keeps on disk
        self._req: dict = {}
        self._art: dict = {}
        self._locks: dict = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, slug):
        # threading.RLock is re-entrant *per thread* by construction (#424).
        with self._locks_guard:
            return self._locks.setdefault(slug, threading.RLock())

    @contextmanager
    def lock(self, slug):
        with self._lock_for(slug):
            yield

    def _require(self, slug):
        if slug not in self._meta:
            raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})

    def exists(self, slug): return slug in self._meta
    def has_meta(self, slug): return slug in self._meta
    def ensure_writable(self, slug): self._require(slug)

    def create(self, slug, request, *, provider=None, model_name=None, context_cards=None, perimeter=None):
        # Invariant 11, at this backing's own layer (#424).
        if slug in self._meta:
            raise SessionExistsError(f"session '{slug}' already exists", details={"slug": slug})
        meta = SessionMeta(session_id="mem-" + slug, slug=slug, created_at="t", updated_at="t",
                           provider=provider, model_name=model_name, context_cards=context_cards,
                           perimeter=perimeter)
        self._meta[slug], self._req[slug], self._art[slug] = meta, request, {}
        return meta

    def read_meta(self, slug):
        self._require(slug)
        return self._meta[slug]

    def delete(self, slug):
        # The dict-backed analogue of the file backing's lock-then-remove (#238).
        self._require(slug)
        with self.lock(slug):
            for table in (self._meta, self._model, self._req, self._art):
                table.pop(slug, None)
            for key in [k for k in self._revs if k[0] == slug]:
                self._revs.pop(key, None)

    def write_meta(self, slug, meta): self._meta[slug] = meta
    def list_slugs(self): return sorted(self._meta)
    def list_unexaminable(self): return []  # a real answer rather than a stub (#80)

    def load_model(self, slug):
        if slug not in self._model:
            raise SessionNotFoundError(f"no model '{slug}'", details={"slug": slug})
        return self._model[slug]

    def load_revision(self, slug, revision):
        if (slug, revision) not in self._revs:
            raise SessionNotFoundError(f"no revision {revision}", details={"slug": slug, "revision": revision})
        return self._revs[(slug, revision)]

    def save_revision(self, slug, model, *, expected_revision=None, provenance=None):
        meta = self.read_meta(slug)
        if expected_revision is not None and meta.current_revision != expected_revision:
            raise RevisionConflictError("conflict", details={"expected": expected_revision,
                                                             "actual": meta.current_revision})
        rev = meta.current_revision + 1
        prov = dict(provenance or {})
        meta.revisions.append(RevisionRecord(
            revision=rev, created_at="t", previous_revision=meta.current_revision or None,
            model_hash="sha256:mem", provider=prov.get("provider"), model_name=prov.get("model_name"),
            surface=prov.get("surface"), prompt_version=prov.get("prompt_version")))
        meta.current_revision = rev
        self._model[slug] = self._revs[(slug, rev)] = model
        return rev, meta

    def request_text(self, slug): return self._req.get(slug, "")
    def context_cards(self, slug): return self._meta[slug].context_cards if slug in self._meta else None

    def save_artifact(self, slug, artifact_type, filename, content, *, source_revision, stale=False):
        self._art[slug][filename] = content
        st = ArtifactStatus(revision=source_revision, filename=filename, updated_at="t", stale=stale)
        self._meta[slug].artifact_status[artifact_type] = st
        return st

    def load_artifact(self, slug, filename): return self._art.get(slug, {}).get(filename)
