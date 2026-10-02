"""The MCP stdio server (#438): its tools are the `docs/api.md` route table, held both ways, and the
paid ones keep the services' pre-payment gates. No `fastapi` here: the server has no extra."""

from __future__ import annotations

import inspect
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _fakes import FakeClient, seed_session

from requivo.mcp import server
from requivo.mcp.server import TOOLS, handle
from requivo.services.discovery import DiscoveryService

API_DOC = Path(__file__).resolve().parents[2] / "docs" / "api.md"
_DOC_ROW = re.compile(r"^\| (GET|POST|PUT|PATCH|DELETE) \| `([^`]+)` \| ([^|]+?) \| ([^|]*?) \|", re.MULTILINE)


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    return tmp_path


def call(name: str, **arguments) -> tuple[dict, bool]:
    reply = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
    assert reply is not None and "result" in reply, reply
    return json.loads(reply["result"]["content"][0]["text"]), reply["result"]["isError"]


# ── the projection: one tool per row of the route table ──────────────────────────


def test_the_tools_are_the_api_route_table_one_to_one_and_each_calls_the_method_its_row_names():
    rows = _DOC_ROW.findall(API_DOC.read_text(encoding="utf-8"))
    documented = {(m, p): backing for m, p, _, backing in rows if p != "/api/v1/health"}
    declared = {t.route: t for t in TOOLS}
    assert documented and len(declared) == len(TOOLS) == len({t.name for t in TOOLS})
    assert set(declared) == set(documented), "tool list and docs/api.md's route table disagree"
    for route, tool in declared.items():
        assert f"`{tool.backing}`" == documented[route].strip(), route
        assert f".{tool.backing.split('.')[1]}(" in inspect.getsource(tool.run), tool.name
    assert {t.name for t in TOOLS if t.paid} == {"run_discovery", "submit_answers", "generate_artifact"}


def test_every_tool_lists_a_schema_whose_required_arguments_are_declared():
    for t in TOOLS:
        listing = t.listing()
        assert set(t.required) <= set(listing["inputSchema"]["properties"]), t.name


# ── the protocol ─────────────────────────────────────────────────────────────────


def test_initialize_list_ping_and_the_refusals():
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["protocolVersion"] == "2024-11-05" and init["result"]["capabilities"] == {"tools": {}}
    assert handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999"}}
                  )["result"]["protocolVersion"] == server.PROTOCOL_VERSIONS[0]
    listed = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in listed] == [t.name for t in TOOLS]
    assert handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})["result"] == {}
    assert handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})["error"]["code"] == -32601
    assert handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope"}})["error"]["code"] == -32602
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle([1])["error"]["code"] == -32600


def test_stdio_answers_one_line_per_request_and_survives_garbage():
    lines = (b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n\xff\xfe\n'
             b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
    out = io.BytesIO()
    server.serve(io.BytesIO(lines), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, None, 2]
    assert [r.get("error", {}).get("code") for r in replies] == [None, -32700, -32700, None]


def test_the_cli_verb_serves_over_a_real_pipe_with_only_protocol_on_stdout(tmp_path):
    env = {**os.environ, "REQUIVO_WORKSPACE": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "requivo", "mcp", "serve"], env=env, capture_output=True, timeout=60,
                          input=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert proc.returncode == 0 and len(replies) == 1, proc.stderr
    assert len(replies[0]["result"]["tools"]) == len(TOOLS)


# ── behaviour through the services ───────────────────────────────────────────────


def test_a_keyless_tool_works_with_no_key_and_never_builds_the_provider(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(server, "DiscoveryService", lambda *a, **k: pytest.fail("a keyless tool built the provider"))
    seed_session("leave")
    status, failed = call("get_status", slug="leave")
    assert not failed and status["slug"] == "leave" and status["revision"] == 1, status
    listed, failed = call("list_sessions")
    assert not failed and [s["slug"] for s in listed["sessions"]] == ["leave"]


def test_a_service_refusal_is_a_tool_error_carrying_the_structured_envelope():
    err, failed = call("get_status", slug="missing")
    assert failed and err["code"] and err["message"]
    err, failed = call("get_status", slug="../etc")
    assert failed and err["code"]
    err, failed = call("submit_answers", slug="x", answers="a", expected_revision=True)
    assert failed and err["code"] == "invalid_request"
    err, failed = call("get_status")
    assert failed and err["code"] == "invalid_request"


def test_a_stale_revision_is_refused_before_the_provider_is_paid(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(server, "DiscoveryService", lambda: DiscoveryService(client=fake))
    seed_session("leave")
    err, failed = call("submit_answers", slug="leave", answers="Finance approves.", expected_revision=0)
    assert failed and err["code"] == "revision_conflict" and not fake.calls
    err, failed = call("run_discovery", slug="leave")
    assert failed and err["code"] == "revision_conflict" and not fake.calls
    err, failed = call("generate_artifact", slug="leave", artifact_type="nonsense")
    assert failed and not fake.calls


def test_create_apply_preview_and_save_round_trip_without_a_provider(monkeypatch):
    monkeypatch.setattr(server, "DiscoveryService", lambda *a, **k: pytest.fail("no provider on a keyless write"))
    made, failed = call("create_session", request="A leave approval system.", slug="leave")
    assert not failed and made["slug"] == "leave" and made["created"] is True
    again, _ = call("create_session", request="A leave approval system.", slug="leave")
    assert again["created"] is False
    seed_session("analysed")
    saved, failed = call("save_artifact", slug="analysed", artifact_type="brief", content="# B", source_revision=1)
    assert not failed, saved
    shown, failed = call("get_artifact", slug="analysed", artifact_type="brief")
    assert not failed and shown["content"] == "# B" and shown["stale"] is False
    assert "brief" in call("list_artifacts", slug="analysed")[0]
