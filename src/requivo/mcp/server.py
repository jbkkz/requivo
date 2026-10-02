"""The MCP stdio server (#438): one tool per row of `docs/api.md`'s route table, each calling the
service method that row names, in-process. No tool composes core calls a service does not own, so
the paid ones keep every pre-payment gate the services enforce (invariant 14) and a keyless
tool never builds a provider client. `TOOLS` states the projection; `tests/mcp/test_mcp_server.py`
holds it to the table, both directions.
"""

from __future__ import annotations

import dataclasses
import json
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO, TypeVar

from pydantic import BaseModel

from requivo import __version__
from requivo.api.dependencies import safe_slug
from requivo.api.usage import track_api_usage, usage_view
from requivo.core.errors import RequivoError
from requivo.services.artifacts import ArtifactService, UnknownArtifactTypeError
from requivo.services.discovery import GENERATABLE, DiscoveryService
from requivo.services.sessions import SessionService

_T = TypeVar("_T")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class InvalidArguments(RequivoError):
    """A tool call whose arguments do not fit its schema: the API's own `invalid_request`."""

    code = "invalid_request"


def _check(a: dict, key: str, kind: type, nullable: bool = False) -> bool:
    """Type-check one present argument, never coercing: a bool is not an integer, a number is not a
    string, a list holds strings. True when it is `None` and allowed to be."""
    v = a[key]
    if v is None and nullable:
        return True
    if not isinstance(v, kind) or (kind is int and isinstance(v, bool)):
        raise InvalidArguments(f"argument {key!r} must be {kind.__name__}", details={"argument": key})
    if isinstance(v, list) and not all(isinstance(x, str) for x in v):
        raise InvalidArguments(f"argument {key!r} must be a list of strings", details={"argument": key})
    return False


def _req(a: dict, key: str, kind: type[_T]) -> _T:
    if key not in a:
        raise InvalidArguments(f"missing argument {key!r}", details={"argument": key})
    _check(a, key, kind)
    return a[key]


def _opt(a: dict, key: str, kind: type[_T]) -> _T | None:
    if a.get(key) is None:
        return None
    _check(a, key, kind)
    return a[key]


def _req_or_null(a: dict, key: str, kind: type[_T]) -> _T | None:
    """A required argument whose value may be an explicit null (`rescope_context_cards`)."""
    if key not in a:
        raise InvalidArguments(f"missing argument {key!r}", details={"argument": key})
    return None if _check(a, key, kind, nullable=True) else a[key]


def _slug(a: dict) -> str:
    return safe_slug(_req(a, "slug", str))


def _json(value: Any) -> Any:
    """What a service returned, as plain JSON: a contract, a dataclass (`SavedEstimate`) or already plain."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


# -- the handlers: each calls exactly the service method its route's row names ------------------------


def _list_sessions(a: dict):
    entries = SessionService().list_entries()
    rows = []
    for e in entries:
        m = e.meta
        rows.append({"slug": e.slug, "revision": None, "provider": None, "updated_at": None,
                     "readable": False, "error": e.error or "no further detail"} if m is None else
                    {"slug": m.slug, "revision": m.current_revision, "provider": m.provider,
                     "updated_at": m.updated_at, "readable": True, "error": None})
    return {"sessions": rows, "degraded": sum(1 for e in entries if not e.readable)}


def _create_session(a: dict):
    meta, created = SessionService().create_session_report(
        _req(a, "request", str), context_cards=_opt(a, "context_cards", list),
        slug=_opt(a, "slug", str), strict_slug=True)
    return {**meta.model_dump(), "created": created}


def _get_session(a: dict):
    return SessionService().meta(_slug(a)).model_dump()


def _get_model(a: dict):
    return SessionService().load_model(_slug(a)).model_dump()


def _list_revisions(a: dict):
    return {"revisions": SessionService().meta(_slug(a)).model_dump()["revisions"]}


def _apply_revision(a: dict):
    return SessionService().update_model(
        _slug(a), _req(a, "proposal", dict), expected_revision=_opt(a, "expected_revision", int),
        provenance={"surface": "mcp-apply"}).to_dict()


def _get_revision(a: dict):
    return SessionService().load_revision(_slug(a), _req(a, "revision", int)).model_dump()


def _preview_revision(a: dict):
    return SessionService().diff(_slug(a), _req(a, "proposal", dict)).to_dict()


def _get_status(a: dict):
    return SessionService().status(_slug(a))


def _get_impact(a: dict):
    slots = _req(a, "slots", str)
    return SessionService().impact(_slug(a), [] if not slots.strip() else slots.split(",")).to_dict()


def _rescope(a: dict):
    return SessionService().rescope(_slug(a), _req_or_null(a, "context_cards", list)).to_dict()


def _run_discovery(a: dict):
    slug = _slug(a)
    with track_api_usage("mcp-discover") as ledger:
        result = DiscoveryService().run_discovery(slug, surface="mcp-discover")
        usage = usage_view(ledger)
    return {**result.to_dict(), "usage": usage}


def _submit_answers(a: dict):
    slug, answers, rev = _slug(a), _req(a, "answers", str), _req(a, "expected_revision", int)
    with track_api_usage("mcp-answer") as ledger:
        result = DiscoveryService().answer(slug, answers, expected_revision=rev, surface="mcp-answer")
        usage = usage_view(ledger)
    return {**result.to_dict(), "usage": usage}


def _list_artifacts(a: dict):
    return ArtifactService().list(_slug(a))


def _show_artifact(a: dict):
    kind = _req(a, "artifact_type", str)
    content, row = ArtifactService().show_with_status(_slug(a), kind)
    return {"type": kind, "filename": row.get("filename"), "source_revision": row.get("revision"),
            "updated_at": row.get("updated_at"), "stale": row.get("stale"), "content": content}


def _generate_artifact(a: dict):
    slug, kind = _slug(a), _req(a, "artifact_type", str)
    if kind not in GENERATABLE:
        raise UnknownArtifactTypeError(
            f"{kind!r} is not a generated artifact; supported: {', '.join(GENERATABLE)}",
            details={"type": kind})
    with track_api_usage(f"mcp-{kind}") as ledger:
        result = DiscoveryService().generate(slug, kind, surface=f"mcp-{kind}")
        usage = usage_view(ledger)
    return {"type": kind, "status": result.status.model_dump(), "artifact": result.artifact, "usage": usage}


def _save_artifact(a: dict):
    return ArtifactService().save(_slug(a), _req(a, "artifact_type", str), _req(a, "content", str),
                                  source_revision=_opt(a, "source_revision", int)).model_dump()


# -- the projection ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """One resource operation: `route` is the `docs/api.md` row it projects, `backing` the service
    method that row names, `paid` whether a provider call can happen (an Anthropic key is then needed)."""

    name: str
    route: tuple[str, str]
    backing: str
    description: str
    run: Callable[[dict], Any]
    properties: dict
    required: tuple[str, ...] = ()
    paid: bool = False

    def listing(self) -> dict:
        return {"name": self.name,
                "description": self.description + (" PAID: calls the Anthropic API on the configured key." if self.paid
                                                   else ""),
                "inputSchema": {"type": "object", "properties": self.properties, "required": list(self.required),
                                "additionalProperties": False}}


_SLUG = {"slug": {"type": "string", "description": "The session slug."}}
_TYPE = {"artifact_type": {"type": "string", "description": "The artifact type, e.g. brief, prd, epic."}}
_PROPOSAL = {"proposal": {"type": "object", "description": "A model proposal, as `requivo model apply` takes it."}}
_REV = {"type": "integer", "description": "The revision this call was reasoned from; a stale one is refused."}
_P = "/api/v1/sessions"

TOOLS: tuple[Tool, ...] = (
    Tool("list_sessions", ("GET", _P), "SessionService.list_entries",
         "List every session; an unreadable one is a row, not a failure.", _list_sessions, {}),
    Tool("create_session", ("POST", _P), "SessionService.create_session_report",
         "Create a session from a request, no provider call; `created` is false for an idempotent repeat.",
         _create_session, {"request": {"type": "string"}, "context_cards": {"type": "array", "items": {"type": "string"}},
                           "slug": {"type": "string"}}, ("request",)),
    Tool("get_session", ("GET", f"{_P}/{{slug}}"), "SessionService.meta", "One session's metadata.",
         _get_session, _SLUG, ("slug",)),
    Tool("get_model", ("GET", f"{_P}/{{slug}}/model"), "SessionService.load_model",
         "The current validated model.", _get_model, _SLUG, ("slug",)),
    Tool("list_revisions", ("GET", f"{_P}/{{slug}}/revisions"), "SessionService.meta",
         "The revision log, provenance and usage included.", _list_revisions, _SLUG, ("slug",)),
    Tool("apply_revision", ("POST", f"{_P}/{{slug}}/revisions"), "SessionService.update_model",
         "Apply a model proposal as a new revision (the external-reasoner write).", _apply_revision,
         {**_SLUG, **_PROPOSAL, "expected_revision": _REV}, ("slug", "proposal")),
    Tool("get_revision", ("GET", f"{_P}/{{slug}}/revisions/{{revision}}"), "SessionService.load_revision",
         "One historical model revision.", _get_revision, {**_SLUG, "revision": {"type": "integer"}},
         ("slug", "revision")),
    Tool("preview_revision", ("POST", f"{_P}/{{slug}}/revisions/preview"), "SessionService.diff",
         "The apply as a dry run: nothing is written.", _preview_revision, {**_SLUG, **_PROPOSAL},
         ("slug", "proposal")),
    Tool("get_status", ("GET", f"{_P}/{{slug}}/status"), "SessionService.status",
         "The understanding checklist, open questions and readiness.", _get_status, _SLUG, ("slug",)),
    Tool("get_impact", ("GET", f"{_P}/{{slug}}/impact"), "SessionService.impact",
         "What rests on the named slots.", _get_impact,
         {**_SLUG, "slots": {"type": "string", "description": "Comma-separated slot ids or label words."}},
         ("slug", "slots")),
    Tool("rescope_context_cards", ("PUT", f"{_P}/{{slug}}/context-cards"), "SessionService.rescope",
         "Re-scope the session's context cards; null selects every card.", _rescope,
         {**_SLUG, "context_cards": {"type": ["array", "null"], "items": {"type": "string"}}},
         ("slug", "context_cards")),
    Tool("run_discovery", ("POST", f"{_P}/{{slug}}/discover"), "DiscoveryService.run_discovery",
         "Run the first discovery turn on a session created without one.", _run_discovery, _SLUG,
         ("slug",), paid=True),
    Tool("submit_answers", ("POST", f"{_P}/{{slug}}/answers"), "DiscoveryService.answer",
         "Fold answers into the model as a new revision; `expected_revision` is required.", _submit_answers,
         {**_SLUG, "answers": {"type": "string"}, "expected_revision": _REV}, ("slug", "answers", "expected_revision"),
         paid=True),
    Tool("list_artifacts", ("GET", f"{_P}/{{slug}}/artifacts"), "ArtifactService.list",
         "Every saved artifact and its stale flag.", _list_artifacts, _SLUG, ("slug",)),
    Tool("get_artifact", ("GET", f"{_P}/{{slug}}/artifacts/{{artifact_type}}"), "ArtifactService.show_with_status",
         "One saved artifact with its freshness.", _show_artifact, {**_SLUG, **_TYPE}, ("slug", "artifact_type")),
    Tool("generate_artifact", ("POST", f"{_P}/{{slug}}/artifacts/{{artifact_type}}"), "DiscoveryService.generate",
         "Generate an artifact and save it; not idempotent: each call pays and overwrites.", _generate_artifact,
         {**_SLUG, **_TYPE}, ("slug", "artifact_type"), paid=True),
    Tool("save_artifact", ("PUT", f"{_P}/{{slug}}/artifacts/{{artifact_type}}"), "ArtifactService.save",
         "Save an artifact produced elsewhere against its source revision; no provider call.", _save_artifact,
         {**_SLUG, **_TYPE, "content": {"type": "string"}, "source_revision": _REV},
         ("slug", "artifact_type", "content", "source_revision")),
)
_BY_NAME = {t.name: t for t in TOOLS}


# -- the JSON-RPC 2.0 loop --------------------------------------------------------------------------


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _call(params: dict) -> dict:
    name = params.get("name")
    tool = _BY_NAME.get(name) if isinstance(name, str) else None
    if tool is None:
        return {"error": (-32602, f"unknown tool {name!r}"[:200])}
    args = params.get("arguments") or {}
    try:
        if not isinstance(args, dict):
            raise InvalidArguments("arguments must be an object")
        unknown = sorted(set(args) - set(tool.properties))
        if unknown:  # the listing says additionalProperties: false, so this enforces it
            raise InvalidArguments(f"unknown argument(s): {', '.join(unknown)}"[:200], details={"unknown": unknown})
        result, failed = _json(tool.run(args)), False
    except RequivoError as e:
        result, failed = e.to_dict(), True
    except Exception:  # the API's own generic 500: a traceback is a server log, never a tool result
        print(f"requivo mcp: unhandled error in {tool.name}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        result, failed = {"code": "internal_error", "message": "Something went wrong on the server."}, True
    return {"result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                       "isError": failed}}


def handle(message: Any) -> dict | None:
    """One inbound JSON-RPC message to its response, or `None` for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "not a JSON-RPC 2.0 request object")
    id_, method = message.get("id"), message.get("method")
    params = message.get("params") or {}
    if "id" not in message:
        return None
    if isinstance(id_, bool) or not isinstance(id_, (str, int, float)) or (
            isinstance(id_, float) and not math.isfinite(id_)):  # NaN parses, but is not JSON on the way out
        return _error(None, -32600, "id must be a string or a number")
    if not isinstance(method, str) or not isinstance(params, dict):
        return _error(id_, -32600, "invalid request")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}}, "serverInfo": {"name": "requivo", "version": __version__}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": [t.listing() for t in TOOLS]}}
    if method == "tools/call":
        out = _call(params)
        if "error" in out:
            return _error(id_, *out["error"])
        return {"jsonrpc": "2.0", "id": id_, **out}
    return _error(id_, -32601, f"method not found: {method}")


def serve(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> None:
    """Read one JSON message per line until EOF, answer each on one line. Bytes, UTF-8 both ways
    (invariant 16): stdout carries protocol and nothing else, diagnostics go to stderr."""
    src: BinaryIO = stdin if stdin is not None else sys.stdin.buffer
    dst: BinaryIO = stdout if stdout is not None else sys.stdout.buffer
    for raw in src:
        if not raw.strip():
            continue
        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):  # JSONDecodeError is a ValueError
            reply = _error(None, -32700, "parse error")
        else:
            try:
                reply = handle(message)
            except Exception:  # one bad message must never end the session: the client would see EOF
                import traceback
                traceback.print_exc(file=sys.stderr)
                reply = _error(message.get("id") if isinstance(message, dict) else None, -32603, "internal error")
        if reply is not None:
            dst.write(_encode(reply) + b"\n")
            dst.flush()


def _encode(reply: dict) -> bytes:
    """One reply, one ASCII line. `\\ud800` parses to a lone surrogate that UTF-8 cannot encode, and a
    reply echoing it (a method, an id, a slug) would end the session: escapes are valid JSON, so
    ASCII cannot fail. A reply JSON cannot carry at all becomes the internal error (#676 review)."""
    try:
        return json.dumps(reply, ensure_ascii=True, allow_nan=False).encode("ascii")
    except ValueError:
        return json.dumps(_error(None, -32603, "internal error"), ensure_ascii=True).encode("ascii")
