"""Offline fakes and model builders shared by the files split out of `test_engine.py` (#72)."""
import io
import json
import shutil
from contextlib import contextmanager, redirect_stdout

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput


def slot(completeness, confidence, impact, test_plan=""):
    # `test_plan` only for a `testable` slot, which the contract refuses without one (#610).
    d = {"completeness": completeness, "confidence": confidence, "impact": impact}
    return {**d, "test_plan": test_plan} if test_plan else d


def full_slots(**overrides):
    """A complete required-slot model (every required slot present, empty/low by default) with per-slot
    overrides."""
    from requivo.core.contracts import _schema_order, schema_slot_ids

    _, required = schema_slot_ids()
    # Schema order, mirroring a real reply (the LLM emits slots in schema order; Pydantic preserves it).
    model = {sid: slot(0, "empty", "low") for sid in _schema_order() if sid in required}
    model.update(overrides)
    return model


def out(model):
    # Pad to the full required slot set: a real EngineOutput always carries every slot.
    return EngineOutput.model_validate(
        {"model": full_slots(**model), "questions": [],
         # A complete model owes an objective as much as it owes its slots (`completeness_gap`).
         "summary": {"objective": "A leave approval system"}}
    )


# ── Characterization harness (commit 0: safety net before the refactor) ───────
# A stub Anthropic client so generator functions run offline.


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class FakeClient:
    """Returns canned JSON replies in order; records each create() call's kwargs."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.calls = []
        self.messages = self  # so client.messages.create resolves to self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._replies.pop(0))


_ENGINE_REPLY = json.dumps(
    {
        # A run() reply must carry the whole required slot set (the completeness invariant).
        "model": full_slots(problem=slot(80, "explicit", "high")),
        "questions": [],
        "summary": {"objective": "o"},
    }
)


# Since #593 a first `discover` with no `--context` judges its grounding *before* the discovery turn.
_JUDGMENT_REPLY = json.dumps({"decision": "none", "reason": "ordinary software, nothing special"})


# #601: a first `discover` with no explicit `--perimeter` routes *before* it judges its grounding.
_ROUTING_REPLY = json.dumps({"decision": "none", "reason": "an ordinary request, not a go-to-market plan"})


# ── The `requivo` subcommand CLI ──────────────────────────────────────────────
# The modern surface is a thin layer over the same core.


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


def _run_app(argv, client=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        app(argv, client=client)
    return buf.getvalue()
