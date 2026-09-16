"""The public payload shapes: what a `--json` output actually promises (#267).

Why this file exists
--------------------
`docs/compatibility.md` declares every `--json` output public, and `CLAUDE.md`'s invariant 8 repeats
it. Until this file the only thing enforcing that was `test_every_json_verb_is_inside_the_promise`,
which is a **membership** check: it asserts, in both directions, that the page names every verb the
parser gives a `--json` to. It says nothing whatever about what those verbs print.

So the promise most likely to be believed was the one with the weakest guard. Four breaking changes
to these payloads shipped in the 1.0.0 release alone (2026-08-20): `session list` became an object
(#87), `session import` renamed two keys (#84), `doctor` respelled an enum value (#88),
`artifact list` gained an envelope (#107).

That number is the argument for this guard rather than against it, and it is worth being exact about
because the issue that asked for this file was not. All four were **deliberate**, and all four are
correctly recorded on that page -- they are there because somebody audited the payload surface by
hand while cutting the 1.0 contract. Nothing in the tree would have gone red if a fifth had been
made by accident, or made deliberately and its row forgotten. The gap is not that these four
escaped; it is that the audit that caught them was a person choosing to look.

The consumers are real and drift by construction. The Claude Code plugin is pinned to a marketplace
SHA that advances independently of PyPI, its skills read these payloads semantically, and
`test_plugin_cli_drift.py` compares verbs and flags only -- a key rename passes every other check in
this tree.

What "public" means, and what is pinned here
--------------------------------------------
The contract `docs/compatibility.md` now states in one testable sentence, and the one this file
enforces: **a payload top-level key set, and the JSON types of those values, are the contract.**

Nested shapes are deliberately not pinned. The behavioural tests already exercise the load-bearing
nested fields, and a table that reached two levels down would be a second copy of the code -- which
is the failure mode of a freeze, not a freeze.

Exact set, not subset -- and the reasoning, because it looks wrong at first
--------------------------------------------------------------------------
Invariant 8 says adding a field is free, so a guard that goes red on an addition looks like a guard
fighting its own contract. It is not, and the deciding evidence is on the page itself: of that
page own `--json` ledger rows, four are *additive* (#67, #80, #97, #62). This project already writes
an addition down. All this file adds is that the record and the code cannot drift apart.

The two directions therefore fail differently, and the message says which one you are in:

* a **documented key that is gone, or whose type changed** -- breaking. It wants a ledger row in
  `docs/compatibility.md` and a changelog entry, or it wants reverting;
* an **undocumented key that appeared** -- additive, and allowed by the contract. Record it in
  `_PAYLOAD_SHAPES` below, in the same change. That red asks for one line; it does not refuse the
  work.

The alternative was a subset assertion -- documented keys must be present, extras ignored. It buys
the same removal detection and gives up the rest: a key added in one release and renamed in the next
would never be pinned at any point in its life, which is the membership guard own defect one level
down.

What this guard cannot see
--------------------------
Stated rather than left to read as coverage:

* **Values, and anything below the top level.** `readiness` being a dict is asserted; what is in it
  is not.
* **A forward-compatible session extra keys.** `session show --json` is `SessionMeta.model_dump()`
  and `SessionMeta` is `extra="allow"`, so a `session.json` written by a *newer* Requivo carries its
  unknown keys straight through into that payload. The fixture session is written by this build,
  so what is observed here is this build contract; extras seen in the wild are a property of the
  file being read, and are the promise of invariant 8 working rather than a break of it.
* **Editing a shipped `_EPIC_EXPORT_SKELETONS` entry instead of adding a new one.** The version
  ratchet below is a one-line diff a reviewer reads, not something a test can refuse -- no test can
  know what version 1 looked like except by being told, and being told is the thing being edited.
* **The structured error envelope** (`{code, message, path?, details?}`), which is public and is
  genuinely optional-keyed -- `RequivoError.to_dict` omits `path` and `details` when they are empty.
  It is not in the table below, because an exact key set is the wrong shape for it. What this file
  does do is refuse to mistake one for a payload: a verb that answers the envelope in the fixture
  workspace is reported as a failed run, never compared as a shape.
* **Anything a surface other than the CLI prints.** Requivo Web has its own boundary.
"""
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
    """The JSON type name of a decoded value. `bool` is tested before `int` on purpose -- in Python
    it is a subclass of it, so the obvious ordering would record every boolean as an integer and let
    a `stale: true` become `stale: 1` without a word."""
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
    """Every verb path that accepts `--json`, read off the built parser rather than off a grep of
    the source: a grep validates the reader own regex, and what is being promised is what the
    command actually accepts.

    A deliberate second copy of the walk in `test_every_json_verb_is_inside_the_promise`, which this
    file is otherwise standalone from. The two cannot silently disagree in the direction that
    matters, because both carry a must-fire lower bound on what the walk found -- a walk that went
    blind fails in both rather than passing in either.
    """
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
    """One observable invocation of one `--json` verb.

    `argv` carries brace placeholders filled from the fixture workspace paths, so the whole record
    stays readable in one place instead of being assembled somewhere else. `keys` maps each
    top-level key to the pipe-separated JSON types it may hold -- a set rather than one name because
    several of these are genuinely nullable and a narrower record would go red on a legitimate
    state.
    """

    label: str
    argv: tuple[str, ...]
    keys: dict[str, str]
    # The documented exit code for this invocation. 0 for every case recorded here, because the
    # fixture workspace is healthy -- but it is pinned rather than assumed, so that a verb which
    # starts exiting 4 (degraded) or 1 (inconsistent) against a healthy session goes red. Without
    # it, moving the exit-code judgement after the parse below would have removed a loud failure
    # and put nothing in its place.
    exits: int = 0


# The recorded shapes. Ordered by **invocation**, not alphabetically: a session must exist before a
# model can be applied to it, a model before an artifact can be saved against it, and an archive
# before it can be imported. `_observe` runs them in this order against one workspace.
#
# `context_cards` is `list|null` in three places for one reason worth stating once: `None` is the
# no-restriction sentinel meaning *every card*, and it is what a session created without `--context`
# persists. `provider` and `model_name` are nullable because a session is created before any
# provider has spoken for it.
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
        # #599 added `changed_exclusions`/`invalidated_exclusions`; #604 adds
        # `changed_thresholds`/`invalidated_thresholds` the same way -- additive (invariant 8), the
        # fifth reasoning collection's own pair beside decisions/challenges/opportunities/exclusions.
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
        # The same `UpdateResult.to_dict()` as `model apply`, which is the point: `diff` is `apply`
        # without the write, and a consumer that reads one reads the other.
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
        # `_status_payload` layers `revision`, `context_cards` and `artifacts` on only when the
        # reference resolves to a canonical session; a bare `model.json` has no session to read them
        # from. Both shapes are public, so both are recorded -- pinning only the fuller one would
        # promise three keys the other form has never carried.
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
        # `notes` is additive, from #260: an artifact type this build has no generator for is
        # reported rather than counted as a defect, so it is a sibling of `problems` that moves
        # neither `ok` nor the exit code. Recorded here in the change that added it, which is what
        # the additive arm of the guard below asks for -- and this row is the first one it ever
        # asked for, on a real change rather than a rehearsal.
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
        # Last by necessity, not alphabetically: every case above operates on slug "s", and this one
        # removes it. `session import --force` above already restored "s" from the exported archive,
        # so it still exists here.
        _Case("session delete --json", ("session", "delete", "s", "--json"),
              {"slug": "str", "deleted": "bool"}),
    ),
}


# The neutral epic export is versioned in the payload itself (`EPIC_EXPORT_VERSION`), which is what
# makes a ratchet possible here and not on the verbs above. The skeleton is recorded **per version**,
# so a key change leaves two ways forward and only one of them is quiet. Bump `EPIC_EXPORT_VERSION`
# and add a skeleton for the new number beside this one: the shipped entry then stays as the record
# of what version 1 was, which is what an importer pinned to that number still receives from an
# older Requivo. Or edit the entry below in place, which records a shape that never shipped under it.
#
# The second is a one-line diff a reviewer can see and no test can refuse. It is named here so that
# doing it is a decision rather than the path of least resistance.
_EPIC_EXPORT_SKELETONS: dict[int, dict[str, dict[str, str]]] = {
    1: {
        "envelope": {"format": "str", "version": "int", "epic": "dict", "issues": "list",
                     "open_questions": "list"},
        "epic": {"title": "str", "description": "str", "labels": "list", "milestone": "str"},
        "issue": {"ref": "str", "title": "str", "description": "str", "labels": "list",
                  "milestone": "str", "depends_on": "list"},
    },
    # v2 (#274): `slug` and `source_revision` are the provenance stamp epic.json was missing --
    # the machine-consumed input an n8n flow acts on, with no staleness row of its own, gained a
    # basis it can compare against `requivo status --json`'s `artifacts.epic.stale`. The `epic` and
    # `issue` sub-skeletons are unchanged from v1; only the envelope gained the two top-level keys.
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
    """(breaking, additive). Two lists rather than one, because the two are different events with
    different remedies, and a single list of differences makes the reader do that sort by hand."""
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
    """A workspace and the documents the recorded invocations need. Returns the substitutions for
    each `_Case.argv` placeholder."""
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
    # Deliberately *outside* any session directory: a `model.json` inside one resolves back to its
    # slug, which would silently turn the second `status` case into a duplicate of the first. It
    # still needs a kebab-case parent, because `_resolve_ref` derives a would-be slug from the
    # directory name and `validate_slug` refuses one that is not -- and pytest names its tmp
    # directories with underscores.
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
    """Run one recorded invocation and return its payload.

    Every way this can fail to produce one is raised as its own named failure rather than folded
    into an empty dict. A shape comparison against a payload nobody obtained would report every
    documented key as removed, which is a true statement about the wrong thing.
    """
    argv = [part.format(**paths) for part in case.argv]
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf):
            app(argv, client=None)  # client=None -> any accidental API use would blow up
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    raw = buf.getvalue()
    # Parse first, then judge the exit code -- the same ordering the provider runner uses for a
    # truncated reply, and for the same reason. Several verbs here print their payload and *then*
    # signal: `session verify` raises SystemExit(1) for an inconsistent session and
    # SystemExit(EXIT_DEGRADED) for one it could not examine, and `session list` does the same for a
    # degraded row. Reading the exit code first would throw a payload that is sitting in the buffer
    # and report it as an unobservable verb, which is a true sentence about the wrong thing.
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
        # value forge a line of a verb own output (#40). Nothing untrusted reaches this fixture
        # today, and quoting it costs one character.
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
    """Both directions, so neither a new verb nor a dead record can pass quietly.

    A verb that gains `--json` and no recorded shape is a public output nobody pinned -- the state
    #84 walked into. A record for a verb the parser no longer offers is the mirror: coverage that
    cannot fire.
    """
    verbs = sorted(_json_verbs(_build_parser()))
    # must fire: the walk really found the surface. An empty list would make both assertions below
    # vacuously true, and `assert not []` is an all-clear nobody earned.
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
    """The guard invariant 8 never had. Runs every recorded invocation against one workspace and
    compares it with what is recorded above.

    A key that vanished or changed type is breaking; a key that appeared is additive, allowed, and
    needs one line here.
    """
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
    """Two issues and a real `depends_on` edge, so `issues` and every key on an issue object are
    observed populated rather than defaulted away."""
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
    """`EPIC_EXPORT_VERSION` was asserted only against itself before, so the export carried a version
    number nothing forced to move -- the stated consumer is an out-of-repo n8n flow that cannot be
    grepped for breakage.

    The skeleton is recorded per version here, so a key change is red until the version moves.
    """
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
    # Indexed, never keyed on `ref`: the label must not read a key the comparison is about to
    # report as missing, or a removed `ref` becomes a KeyError from the guard instead of a finding.
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
