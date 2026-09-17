"""The shared harness for the deterministic-CLI test modules (#141)."""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids


def _slot(c=0, cf="empty", im="low", v=""):
    return {"completeness": c, "confidence": cf, "impact": im, "value": v}


def _full_model(**overrides):
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    # A complete model owes an objective as much as it owes its slots (see `completeness_gap`).
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        # client=None is "build the default client", NOT a poison pill (#419).
        app(argv, client=None)
    return buf.getvalue()


def _run_json(argv):
    return json.loads(_run(argv))


_SESSIONS_ROW = re.compile(r"^  [✅❌🟡] sessions\b")


def _forge_meta(slug: str, fields: dict) -> None:
    """Write arbitrary values into a session's persisted metadata, the way an imported archive or a
    hand-edited `session.json` can."""
    p = store.canonical_dir(slug) / "session.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta.update(fields)
    p.write_text(json.dumps(meta), encoding="utf-8")


def _run_stdin(argv, text, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    return _run(argv)
