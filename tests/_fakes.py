"""Offline fakes and model builders shared by the files split out of `test_engine.py` (#72).

Not a test module and not a `conftest.py`, and both halves of that are deliberate.

* **Never collected.** The name starts with `_` and not with `test_`, so pytest never imports it
  looking for tests. Its only job is to be imported by name.
* **Not a root `conftest.py`.** A fixture declared there applies to every file under `tests/`,
  including the ones this change does not touch — an autouse workspace fixture would silently reach
  the whole suite and change what several unrelated tests run against. Sharing *values* across files
  is safe; sharing *fixtures* repo-wide is not. So the `_isolate_workspace` fixture stays local to
  each file, which is also what every sibling test module already does.

`test_cli_flag_names.py` states the other half of this from the other side: a test that reaches into
*another test module* for a helper breaks when that module reorganises. Reaching into this one
cannot, because it has no other job to reorganise around.
"""
import io
import json
import shutil
from contextlib import contextmanager, redirect_stdout

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput


def slot(completeness, confidence, impact):
    return {"completeness": completeness, "confidence": confidence, "impact": impact}


def full_slots(**overrides):
    """A complete required-slot model (every required slot present, empty/low by default) with
    per-slot overrides — mirrors what a real discovery turn emits, so models that go through run()
    satisfy the completeness invariant."""
    from requivo.core.contracts import _schema_order, schema_slot_ids

    _, required = schema_slot_ids()
    # Schema order, mirroring a real reply (the LLM emits slots in schema order; Pydantic preserves
    # it). `required` is an unordered set, so iterate the ordered id list and keep the required ones.
    model = {sid: slot(0, "empty", "low") for sid in _schema_order() if sid in required}
    model.update(overrides)
    return model


def out(model):
    # Pad to the full required slot set: a real EngineOutput always carries every slot, and the
    # discovery boundary enforces it. Tests that care about one slot just override that one.
    return EngineOutput.model_validate(
        {"model": full_slots(**model), "questions": [],
         # A complete model owes an objective as much as it owes its slots (`completeness_gap`).
         "summary": {"objective": "A leave approval system"}}
    )


# ── Characterization harness (commit 0: safety net before the refactor) ───────
# A stub Anthropic client so generator functions run offline. It mimics the one
# call shape _complete() relies on: client.messages.create(...) returning
# resp.content = [block] where block.type == "text".


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
        # A run() reply must carry the whole required slot set (the completeness invariant) — a
        # single-slot reply would now be rejected and retried, exhausting the FakeClient.
        "model": full_slots(problem=slot(80, "explicit", "high")),
        "questions": [],
        "summary": {"objective": "o"},
    }
)


# Since #593 a first `discover` with no `--context` judges its grounding *before* the discovery
# turn, so every fixture that drives that path scripts this reply first. Written at the call sites
# rather than prepended inside `FakeClient`, because how many calls a first discovery costs is
# precisely what several of those tests are for -- hiding the extra one in the fake would make them
# pass while measuring nothing. `none` is the ordinary verdict and changes no card selection.
_JUDGMENT_REPLY = json.dumps({"decision": "none", "reason": "ordinary software, nothing special"})


# ── The `requivo` subcommand CLI ──────────────────────────────────────────────
# The modern surface is a thin layer over the same core; app() takes an injected
# client so API-backed verbs run offline against a FakeClient.


@contextmanager
def _model_in_out(slug):
    """A canonical .requivo/sessions/<slug>/ session with a model the subcommands can load and mutate.
    Yields the path to model.json. `status` and `impact` accept that path directly; every other
    verb writes back into a session and takes the slug instead (`p.parent.name`) -- see #402."""
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
