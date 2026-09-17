"""The public payload shapes: what a `--json` output actually promises (#267)."""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from dataclasses import dataclass
from typing import Any

import pytest

from conftest import slot as _slot
from requivo.cli import _build_parser, app
from requivo.core.adapters import EPIC_EXPORT_FORMAT, EPIC_EXPORT_VERSION, epic_export
from requivo.core.contracts import Epic, _schema_order, schema_slot_ids


def _json_type(value: Any) -> str:
    """The JSON type name of a decoded value. `bool` is tested before `int` on purpose."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _json_verbs(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """Every verb path that accepts `--json`, read off the built parser rather than off a grep of the source."""
    found = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                path = f"{prefix} {name}".strip()
                if any("--json" in (a.option_strings or []) for a in sub._actions):
                    found.append(path)
                found += _json_verbs(sub, path)
    return found


@dataclass(frozen=True)
class _Case:
    """One observable invocation of one `--json` verb."""

    label: str
    argv: tuple[str, ...]
    keys: dict[str, str]
    # The documented exit code for this invocation.
    exits: int = 0


# The recorded shapes.
#
# `context_cards` is `list|null` in three places for one reason worth stating once.
_PAYLOAD_SHAPES: dict[str, tuple[_Case, ...]] = {
    "doctor": (
        _Case("doctor --json", ("doctor", "--json"), {
            "requivo_version": "str", "python_version": "str", "os": "str", "model": "dict",
            "assets": "dict", "output": "dict", "schema": "dict", "context_cards": "list",
            "context": "dict", "perimeters": "dict", "provider_anthropic": "dict", "workspace": "dict",
            "sessions": "dict", "locks": "dict"}),
    ),
    "session init": (
        _Case("session init --json",
              ("session", "init", "Build a leave approval system.", "--slug", "s",
               "--context", "b2b-platform", "--json"),
              {"slug": "str", "session_id": "str", "path": "str",
               "context_cards": "list|null", "revision": "int"}),
    ),
    "model validate": (
        _Case("model validate --json", ("model", "validate", "{proposal}", "--json"),
              {"status": "str", "slots": "int"}),
    ),
    "model apply": (
        # #599 added `changed_exclusions`/`invalidated_exclusions`.
        _Case("model apply --json", ("model", "apply", "s", "{proposal}", "--json"), {
            "status": "str", "revision": "int", "changed_slots": "list",
            "changed_decisions": "list", "changed_challenges": "list",
            "changed_opportunities": "list", "changed_exclusions": "list",
            "changed_thresholds": "list",
            "invalidated_decisions": "list", "invalidated_challenges": "list",
            "invalidated_exclusions": "list", "invalidated_thresholds": "list",
            "stale_artifacts": "list", "readiness": "dict"}),
    ),
    "model diff": (
        # The same `UpdateResult.to_dict()` as `model apply`, which is the point.
        _Case("model diff --json", ("model", "diff", "s", "{proposal}", "--json"), {
            "status": "str", "revision": "int", "changed_slots": "list",
            "changed_decisions": "list", "changed_challenges": "list",
            "changed_opportunities": "list", "changed_exclusions": "list",
            "changed_thresholds": "list",
            "invalidated_decisions": "list", "invalidated_challenges": "list",
            "invalidated_exclusions": "list", "invalidated_thresholds": "list",
            "stale_artifacts": "list", "readiness": "dict"}),
    ),
    "artifact save": (
        _Case("artifact save --json",
              ("artifact", "save", "s", "--type", "prd", "--file", "{prd}",
               "--revision", "1", "--json"),
              {"type": "str", "filename": "str", "revision": "int", "stale": "bool"}),
    ),
    "artifact list": (
        _Case("artifact list --json", ("artifact", "list", "s", "--json"),
              {"slug": "str", "artifacts": "dict"}),
    ),
    "status": (
        # Two cases, because this payload is genuinely conditional and nothing said so before.
        _Case("status <slug> --json", ("status", "s", "--json"), {
            "slug": "str", "readiness": "dict", "understanding": "dict", "questions": "list",
            "summary": "dict", "remaining_gaps": "list", "revision": "int",
            "context_cards": "list|null", "artifacts": "dict", "perimeter": "str"}),
        _Case("status <a bare model.json> --json", ("status", "{bare_model}", "--json"), {
            "slug": "str", "readiness": "dict", "understanding": "dict", "questions": "list",
            "summary": "dict", "remaining_gaps": "list"}),
    ),
    "session show": (
        _Case("session show --json", ("session", "show", "s", "--json"), {
            "format_version": "int", "requivo_version": "str", "session_id": "str", "slug": "str",
            "created_at": "str", "updated_at": "str", "provider": "str|null",
            "model_name": "str|null", "context_cards": "list|null", "request_hash": "str",
            "schema_version": "int", "current_revision": "int", "revisions": "list",
            "artifact_status": "dict", "perimeter": "str|null"}),
    ),
    "session verify": (
        # `notes` is additive, from #260: an artifact type this build has no generator for is reported rather than counted as a defect, so it is a sibling of `problems` that moves neither `ok` nor the exit code.
        _Case("session verify --json", ("session", "verify", "s", "--json"), {
            "slug": "str", "ok": "bool", "session": "dict", "problems": "list",
            "notes": "list", "context_cards": "dict"}),
    ),
    "session list": (
        _Case("session list --json", ("session", "list", "--json"),
              {"sessions": "list", "degraded": "int", "session_root": "str"}),
    ),
    "session rescope": (
        _Case("session rescope --json",
              ("session", "rescope", "s", "--context", "event-ops", "--json"),
              {"slug": "str", "previous_context_cards": "list|null", "context_cards": "list|null",
               "revision": "int", "changed": "bool"}),
    ),
    "session export": (
        _Case("session export --json",
              ("session", "export", "s", "-o", "{archive}", "--json"),
              {"slug": "str", "archive": "str"}),
    ),
    "session import": (
        _Case("session import --json", ("session", "import", "{archive}", "--force", "--json"),
              {"slug": "str", "path": "str", "replaced": "bool"}),
    ),
    "session migrate": (
        _Case("session migrate --json", ("session", "migrate", "--json"),
              {"migrated": "list", "skipped_already_present": "list", "interrupted": "list",
               "errors": "list", "unreadable": "list", "source": "str"}),
    ),
    "session delete": (
        # Last by necessity, not alphabetically: every case above operates on slug "s", and this one removes it.
        _Case("session delete --json", ("session", "delete", "s", "--json"),
              {"slug": "str", "deleted": "bool"}),
    ),
}


# The neutral epic export is versioned in the payload itself (`EPIC_EXPORT_VERSION`).
#
# The second is a one-line diff a reviewer can see and no test can refuse.
_EPIC_EXPORT_SKELETONS: dict[int, dict[str, dict[str, str]]] = {
    1: {
        "envelope": {"format": "str", "version": "int", "epic": "dict", "issues": "list",
                     "open_questions": "list"},
        "epic": {"title": "str", "description": "str", "labels": "list", "milestone": "str"},
        "issue": {"ref": "str", "title": "str", "description": "str", "labels": "list",
                  "milestone": "str", "depends_on": "list"},
    },
    # v2 (#274): `slug` and `source_revision` are the provenance stamp epic.json was missing.
    2: {
        "envelope": {"format": "str", "version": "int", "slug": "str", "source_revision": "int",
                     "epic": "dict", "issues": "list", "open_questions": "list"},
        "epic": {"title": "str", "description": "str", "labels": "list", "milestone": "str"},
        "issue": {"ref": "str", "title": "str", "description": "str", "labels": "list",
                  "milestone": "str", "depends_on": "list"},
    },
}


def _bullets(lines: list[str]) -> str:
    return "\n  " + "\n  ".join(lines) + "\n\n"


def _compare(label: str, payload: dict, recorded: dict[str, str]) -> tuple[list[str], list[str]]:
    """(breaking, additive). Two lists rather than one, because the two are different events with different
    remedies, and a single list of differences makes the reader do that sort by hand."""
    breaking, additive = [], []
    for key, allowed in recorded.items():
        if key not in payload:
            breaking.append(f"{label}: `{key}` is gone (recorded as {allowed})")
            continue
        got = _json_type(payload[key])
        if got not in allowed.split("|"):
            breaking.append(f"{label}: `{key}` is now {got}, recorded as {allowed}")
    for key in payload:
        if key not in recorded:
            additive.append(f"{label}: `{key}` ({_json_type(payload[key])}) is not recorded")
    return breaking, additive


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> dict[str, str]:
    """A workspace and the documents the recorded invocations need."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    _, required = schema_slot_ids()
    proposal = {
        "model": {sid: _slot() for sid in _schema_order() if sid in required},
        "questions": [],
        "summary": {"objective": "A leave approval system"},
    }
    body = json.dumps(proposal)
    (tmp_path / "proposal.json").write_text(body, encoding="utf-8")
    # Deliberately *outside* any session directory.
    bare = tmp_path / "bare-model" / "model.json"
    bare.parent.mkdir()
    bare.write_text(body, encoding="utf-8")
    (tmp_path / "prd.md").write_text("# PRD", encoding="utf-8")
    return {
        "proposal": str(tmp_path / "proposal.json"),
        "bare_model": str(bare),
        "prd": str(tmp_path / "prd.md"),
        "archive": str(tmp_path / "s.zip"),
    }


def _observe(case: _Case, paths: dict[str, str]) -> dict:
    """Run one recorded invocation and return its payload."""
    argv = [part.format(**paths) for part in case.argv]
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf):
            app(argv, client=None)  # client=None -> any accidental API use would blow up
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    raw = buf.getvalue()
    # Parse first, then judge the exit code -- the same ordering the provider runner uses for a truncated reply, and for the same reason.
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"`{case.label}` exited {code} and printed no parseable JSON on stdout, so no payload "
            f"was observed at all -- that is a broken fixture rather than a clean verb. stdout was "
            f"{raw[:400]!r}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(
            f"`{case.label}` printed a {type(payload).__name__}, not an object. Every `--json` "
            f"payload has a top level -- #87 and #107 are the two that did not, and both were "
            f"breaking changes made to give them one.")
    if "code" in payload and "message" in payload and "code" not in case.keys:
        # `!r`, like the two branches above: `message` is assembled from values read back off disk
        # -- a slug, a card name, a filename -- and this repository has already had a persisted
        # value forge a line of a verb own output (#40).
        raise AssertionError(
            f"`{case.label}` answered the structured error envelope, not its payload: "
            f"{payload['code']!r} -- {payload['message']!r}. The fixture is wrong, or the verb is.")
    if code != case.exits:
        raise AssertionError(
            f"`{case.label}` printed a well-formed payload and then exited {code}, where {case.exits} "
            f"is recorded. The exit code is part of what this page promises -- 4 is degraded, 1 is a "
            f"firm negative -- so this is a change of answer, not a change of shape.")
    return payload


def test_every_json_verb_has_a_recorded_payload_shape():
    """Both directions, so neither a new verb nor a dead record can pass quietly (#84)."""
    verbs = sorted(_json_verbs(_build_parser()))
    # must fire: the walk really found the surface.
    assert len(verbs) >= 15, f"the parser walk looks blind: {verbs}"
    assert "doctor" in verbs and "session list" in verbs

    unpinned = [v for v in verbs if v not in _PAYLOAD_SHAPES]
    assert not unpinned, (
        "these verbs accept `--json` and have no recorded payload shape, so their output is public "
        f"and unpinned: {unpinned}. Add a `_Case` for each -- run it once and record what it "
        "prints.")

    stale = [v for v in _PAYLOAD_SHAPES if v not in verbs]
    assert not stale, (
        f"these verbs have a recorded payload shape and no `--json`: {stale}. The record cannot "
        "fire, which reads as coverage this file does not have.")


def test_every_public_json_payload_keeps_its_recorded_top_level_shape(workspace):
    """The guard invariant 8 never had. Runs every recorded invocation against one workspace and compares it
    with what is recorded above."""
    cases = [case for verb in _PAYLOAD_SHAPES for case in _PAYLOAD_SHAPES[verb]]
    # must fire: an emptied table would make the loop below iterate over nothing and pass.
    assert len(cases) >= 16, f"the recorded-shape table looks empty: {len(cases)} cases"

    breaking: list[str] = []
    additive: list[str] = []
    for case in cases:
        payload = _observe(case, workspace)
        gone, extra = _compare(case.label, payload, case.keys)
        breaking += gone
        additive += extra

    assert not breaking, (
        "BREAKING change to a public `--json` payload -- a documented top-level key was removed or "
        "changed type:" + _bullets(breaking) +
        "docs/compatibility.md promises these payloads. If the change is intended it needs a "
        "ledger row on that page and a changelog entry, the way #87, #84, #88 and #107 each did. "
        "If it is not, revert it.")

    assert not additive, (
        "a public `--json` payload gained a top-level key that is not recorded:"
        + _bullets(additive) +
        "Adding a field is allowed -- invariant 8 says so, and this is not a refusal. Record it in "
        "`_PAYLOAD_SHAPES` in this file, in the same change, so the next rename of it goes red.")


def _epic() -> Epic:
    """Two issues and a real `depends_on` edge, so `issues` and every key on an issue object are observed
    populated rather than defaulted away."""
    return Epic(
        title="Leave approval",
        milestone="Pilot",
        goal="Employees request leave, managers approve.",
        business_value="Removes email and spreadsheet churn.",
        in_scope=["Submission"],
        open_questions=["Who approves for the approver?"],
        issues=[
            {"id": "#1", "title": "Model the leave object", "description": "Fields.",
             "labels": ["backend"]},
            {"id": "#2", "title": "Approval circuit", "labels": ["feature"], "depends_on": ["#1"]},
        ],
    )


def test_the_epic_export_skeleton_is_pinned_to_its_version():
    """`EPIC_EXPORT_VERSION` was asserted only against itself before."""
    assert EPIC_EXPORT_VERSION in _EPIC_EXPORT_SKELETONS, (
        f"EPIC_EXPORT_VERSION is {EPIC_EXPORT_VERSION} and no skeleton is recorded for it. A bump "
        "is a new entry in `_EPIC_EXPORT_SKELETONS`, beside the ones already there -- the older "
        "entries are what an importer pinned to an older number still receives.")
    skeleton = _EPIC_EXPORT_SKELETONS[EPIC_EXPORT_VERSION]

    payload = epic_export(_epic(), "leave-approval", 3)
    assert payload["format"] == EPIC_EXPORT_FORMAT
    assert payload["version"] == EPIC_EXPORT_VERSION
    # must fire: an empty `issues` would make the per-issue comparison below iterate over nothing.
    assert len(payload["issues"]) == 2, payload["issues"]

    breaking, additive = _compare("epic export envelope", payload, skeleton["envelope"])
    b, a = _compare("epic export `epic` object", payload["epic"], skeleton["epic"])
    breaking += b
    additive += a
    # Indexed, never keyed on `ref`: the label must not read a key the comparison is about to report as missing, or a removed `ref` becomes a KeyError from the guard instead of a finding.
    for n, issue in enumerate(payload["issues"]):
        b, a = _compare(f"epic export issues[{n}]", issue, skeleton["issue"])
        breaking += b
        additive += a

    assert not (breaking or additive), (
        f"the epic export key skeleton changed while EPIC_EXPORT_VERSION is still "
        f"{EPIC_EXPORT_VERSION}:" + _bullets(breaking + additive) +
        "This envelope is versioned so an importer can validate it, and the stated consumer is an "
        "n8n flow outside this repository. Bump `EPIC_EXPORT_VERSION` and record a skeleton for "
        "the new number beside the old one; editing the existing entry records a shape that never "
        "shipped under that number.")
