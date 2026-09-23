"""The public payload shapes: what a `--json` output actually promises (#267)."""
from __future__ import annotations

import argparse
import json
from typing import Any

import pytest
from _fakes import full_model, run_cli_exit

from requivo.cli import _build_parser
from requivo.core.adapters import EPIC_EXPORT_FORMAT, EPIC_EXPORT_VERSION, epic_export
from requivo.core.contracts import Epic

_JSON_TYPES = ((bool, "bool"), (int, "int"), (float, "float"), (str, "str"), (list, "list"), (dict, "dict"))  # bool before int


def _json_type(value: Any) -> str:
    return "null" if value is None else next((n for cls, n in _JSON_TYPES if isinstance(value, cls)), type(value).__name__)


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


def _keys(spec: str, **typed: str) -> dict[str, str]:
    """`"a b:int c"` reads as str keys unless typed; keyword overrides add or retype keys."""
    out = {}
    for token in spec.split():
        name, _, kind = token.partition(":")
        out[name] = kind or "str"
    return {**out, **typed}


_UPDATE_RESULT = _keys("status revision:int readiness:dict", **{k: "list" for k in (
    "changed_slots", "changed_decisions", "changed_challenges", "changed_opportunities", "changed_exclusions",
    "changed_thresholds", "invalidated_decisions", "invalidated_challenges", "invalidated_exclusions",
    "invalidated_thresholds", "stale_artifacts")})   # #599 added the exclusions; `model diff` is the same dict
_STATUS_BARE = _keys("slug readiness:dict understanding:dict questions:list summary:dict remaining_gaps:list")

# The recorded shapes, keyed by verb; `context_cards` is `list|null` wherever a session may select every card.
# `session delete` is last by necessity: every case above operates on slug "s", and it removes it.
_PAYLOAD_SHAPES: dict[str, list[tuple[str, tuple[str, ...], dict[str, str]]]] = {
    "doctor": [("doctor --json", ("doctor", "--json"), _keys(
        "requivo_version python_version os model:dict assets:dict output:dict schema:dict context_cards:list "
        "context:dict perimeters:dict provider_anthropic:dict workspace:dict sessions:dict locks:dict"))],
    "session init": [("session init --json",
                      ("session", "init", "Build a leave approval system.", "--slug", "s", "--context", "b2b-platform", "--json"),
                      _keys("slug session_id path context_cards:list|null revision:int"))],
    "model validate": [("model validate --json", ("model", "validate", "{proposal}", "--json"), _keys("status slots:int"))],
    "model apply": [("model apply --json", ("model", "apply", "s", "{proposal}", "--json"), _UPDATE_RESULT)],
    "model diff": [("model diff --json", ("model", "diff", "s", "{proposal}", "--json"), _UPDATE_RESULT)],
    "artifact save": [("artifact save --json",
                       ("artifact", "save", "s", "--type", "prd", "--file", "{prd}", "--revision", "1", "--json"),
                       _keys("type filename revision:int stale:bool"))],
    "artifact list": [("artifact list --json", ("artifact", "list", "s", "--json"), _keys("slug artifacts:dict"))],
    "status": [   # two cases: the payload is genuinely conditional
        ("status <slug> --json", ("status", "s", "--json"),
         _keys("revision:int context_cards:list|null artifacts:dict perimeter", **_STATUS_BARE)),
        ("status <a bare model.json> --json", ("status", "{bare_model}", "--json"), _STATUS_BARE)],
    "session show": [("session show --json", ("session", "show", "s", "--json"), _keys(
        "format_version:int requivo_version session_id slug created_at updated_at provider:str|null "
        "model_name:str|null context_cards:list|null request_hash schema_version:int current_revision:int "
        "revisions:list artifact_status:dict perimeter:str|null"))],
    # `notes` (#260) is a sibling of `problems` that moves neither `ok` nor the exit code.
    "session verify": [("session verify --json", ("session", "verify", "s", "--json"),
                        _keys("slug ok:bool session:dict problems:list notes:list context_cards:dict"))],
    "session list": [("session list --json", ("session", "list", "--json"), _keys("sessions:list degraded:int session_root"))],
    "session rescope": [("session rescope --json", ("session", "rescope", "s", "--context", "event-ops", "--json"),
                         _keys("slug previous_context_cards:list|null context_cards:list|null revision:int changed:bool"))],
    "session export": [("session export --json", ("session", "export", "s", "-o", "{archive}", "--json"), _keys("slug archive"))],
    "session import": [("session import --json", ("session", "import", "{archive}", "--force", "--json"), _keys("slug path replaced:bool"))],
    "session migrate": [("session migrate --json", ("session", "migrate", "--json"),
                         _keys("migrated:list skipped_already_present:list interrupted:list errors:list unreadable:list source"))],
    "session delete": [("session delete --json", ("session", "delete", "s", "--json"), _keys("slug deleted:bool"))],
}

# The neutral epic export is versioned in the payload itself; older entries stay. v2 (#274) adds the provenance stamp.
_EPIC_OBJECT = _keys("title description labels:list milestone")
_ISSUE_OBJECT = _keys("ref title description labels:list milestone depends_on:list")
_V1_ENVELOPE = _keys("format version:int epic:dict issues:list open_questions:list")
_EPIC_EXPORT_SKELETONS: dict[int, dict[str, dict[str, str]]] = {
    1: {"envelope": _V1_ENVELOPE, "epic": _EPIC_OBJECT, "issue": _ISSUE_OBJECT},
    2: {"envelope": _keys("slug source_revision:int", **_V1_ENVELOPE), "epic": _EPIC_OBJECT, "issue": _ISSUE_OBJECT},
}


def _compare(label: str, payload: dict, recorded: dict[str, str]) -> tuple[list[str], list[str]]:
    """(breaking, additive): two lists because the two are different events with different remedies."""
    breaking, additive = [], []
    for key, allowed in recorded.items():
        if key not in payload:
            breaking.append(f"{label}: `{key}` is gone (recorded as {allowed})")
        elif _json_type(payload[key]) not in allowed.split("|"):
            breaking.append(f"{label}: `{key}` is now {_json_type(payload[key])}, recorded as {allowed}")
    additive += [f"{label}: `{k}` ({_json_type(v)}) is not recorded" for k, v in payload.items() if k not in recorded]
    return breaking, additive


@pytest.fixture
def paths(workspace) -> dict[str, str]:
    """The documents the recorded invocations need, in an isolated workspace."""
    body = json.dumps(full_model())
    bare = workspace / "bare-model" / "model.json"   # deliberately outside any session directory
    bare.parent.mkdir()
    for p, text in ((workspace / "proposal.json", body), (bare, body), (workspace / "prd.md", "# PRD")):
        p.write_text(text, encoding="utf-8")
    return {"proposal": str(workspace / "proposal.json"), "bare_model": str(bare), "prd": str(workspace / "prd.md"),
            "archive": str(workspace / "s.zip")}


def _observe(label: str, argv: tuple[str, ...], keys: dict[str, str], paths: dict[str, str]) -> dict:
    """Run one recorded invocation; parse first, then judge the exit code, as the provider runner does."""
    raw, code = run_cli_exit([part.format(**paths) for part in argv])
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"`{label}` exited {code} with no parseable JSON on stdout (a broken fixture): {raw[:400]!r}") from exc
    assert isinstance(payload, dict), f"`{label}` printed a {type(payload).__name__}, not an object (#87, #107 were the two that did not)"
    if "code" in payload and "message" in payload and "code" not in keys:   # `!r`: `message` has forged a line before (#40)
        raise AssertionError(f"`{label}` answered the error envelope, not its payload: {payload['code']!r} -- {payload['message']!r}")
    assert code == 0, f"`{label}` printed a well-formed payload and then exited {code}: a change of answer, not of shape"
    return payload


def test_every_json_verb_has_a_recorded_payload_shape():
    """Both directions, so neither a new verb nor a dead record can pass quietly (#84)."""
    verbs = sorted(_json_verbs(_build_parser()))
    assert len(verbs) >= 15 and "doctor" in verbs and "session list" in verbs, f"the parser walk looks blind: {verbs}"   # must fire
    unpinned = [v for v in verbs if v not in _PAYLOAD_SHAPES]
    assert not unpinned, f"these verbs accept `--json` and have no recorded payload shape: {unpinned}. Run each once and record a case."
    stale = [v for v in _PAYLOAD_SHAPES if v not in verbs]
    assert not stale, f"these verbs have a recorded payload shape and no `--json`, so the record cannot fire: {stale}"


def test_every_public_json_payload_keeps_its_recorded_top_level_shape(paths):
    """The guard invariant 8 never had: every recorded invocation against one workspace, compared with the record."""
    cases = [case for verb in _PAYLOAD_SHAPES for case in _PAYLOAD_SHAPES[verb]]
    assert len(cases) >= 16, f"the recorded-shape table looks empty: {len(cases)} cases"   # must fire
    breaking: list[str] = []
    additive: list[str] = []
    for label, argv, keys in cases:
        gone, extra = _compare(label, _observe(label, argv, keys, paths), keys)
        breaking += gone
        additive += extra
    assert not breaking, ("BREAKING change to a public `--json` payload (a ledger row in docs/compatibility.md and a changelog "
                          "entry if intended, as #87, #84, #88 and #107 did):\n  " + "\n  ".join(breaking))
    assert not additive, ("a public `--json` payload gained an unrecorded top-level key (allowed by invariant 8; record it in "
                          "`_PAYLOAD_SHAPES` in the same change):\n  " + "\n  ".join(additive))


def test_the_epic_export_skeleton_is_pinned_to_its_version():
    """`EPIC_EXPORT_VERSION` was asserted only against itself before; a bump is a new entry beside the old ones."""
    assert EPIC_EXPORT_VERSION in _EPIC_EXPORT_SKELETONS, f"no skeleton is recorded for EPIC_EXPORT_VERSION {EPIC_EXPORT_VERSION}"
    skeleton = _EPIC_EXPORT_SKELETONS[EPIC_EXPORT_VERSION]
    # Two issues and a real `depends_on` edge, so every key on an issue object is observed populated.
    epic = Epic(title="Leave approval", milestone="Pilot", goal="Employees request leave, managers approve.",
                business_value="Removes email and spreadsheet churn.", in_scope=["Submission"],
                open_questions=["Who approves for the approver?"],
                issues=[{"id": "#1", "title": "Model the leave object", "description": "Fields.", "labels": ["backend"]},
                        {"id": "#2", "title": "Approval circuit", "labels": ["feature"], "depends_on": ["#1"]}])
    payload = epic_export(epic, "leave-approval", 3)
    assert (payload["format"], payload["version"]) == (EPIC_EXPORT_FORMAT, EPIC_EXPORT_VERSION)
    assert len(payload["issues"]) == 2, payload["issues"]   # must fire: the per-issue comparison iterates over something
    findings = [*_compare("epic export envelope", payload, skeleton["envelope"]),
                *_compare("epic export `epic` object", payload["epic"], skeleton["epic"])]
    # Indexed, never keyed on `ref`: a removed `ref` must be a finding, not a KeyError from the guard.
    findings += [pair for n, issue in enumerate(payload["issues"]) for pair in _compare(f"epic export issues[{n}]", issue, skeleton["issue"])]
    changed = [line for pair in findings for line in pair]
    assert not changed, (f"the epic export key skeleton changed while EPIC_EXPORT_VERSION is still {EPIC_EXPORT_VERSION}; bump it and "
                         "record a skeleton for the new number beside the old one:\n  " + "\n  ".join(changed))
