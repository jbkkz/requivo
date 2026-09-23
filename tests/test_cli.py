"""The `requivo` journey verbs, end to end and offline (#72): status, demo, the generators, discover, answer,
`--help` (#244) and the pointer `status` ends with (#246)."""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

import pytest
from _fakes import (
    _ENGINE_REPLY,
    _JUDGMENT_REPLY,
    _ROUTING_REPLY,
    FakeClient,
    _model_in_out,
    forge_meta,
    full_model,
    printed,
    run_cli,
    run_cli_fails,
    seed_session,
    slot,
)
from _fakes import out as _built_model

from requivo.cli import (
    _HELP_GROUP_PLUMBING,
    _HELP_GROUP_SCRIPTS,
    _HELP_GROUP_START,
    DEMO_CHANGED_SLOT,
    DOC_TYPES,
    _build_parser,
    _doc_generation_order,
    _fenced_text,
    _prompt_doc_selection,
    _resolve_doc_types,
    app,
)
from requivo.core import persistence as store
from requivo.core.analysis import readiness_blockers
from requivo.core.context import available_cards
from requivo.core.contracts import Confidence, EngineOutput
from requivo.core.dependencies import propagate
from requivo.core.errors import RequivoError
from requivo.core.persistence import load_model
from requivo.deterministic import is_file_argument
from requivo.paths import DEMO
from requivo.providers.anthropic.generators import _OP_PROMPTS
from requivo.render.markdown import _stated
from requivo.render.terminal import DRAFT_NOTE, docs_menu_rows, next_command, render_readiness
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

_DISCOVER = (_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
EXAMPLES = DEMO.parents[3] / "examples"   # assets/demo -> assets -> requivo -> src -> repo
_PROBLEM = {"problem": slot(80, "explicit", "high")}


def _only_slug() -> str:
    slugs = [m.slug for m in SessionService().list_sessions()]
    assert len(slugs) == 1, f"expected exactly one session, got {slugs}"
    return slugs[0]


def _saved_request(slug: str) -> str:
    return (store.canonical_dir(slug) / "request.md").read_text(encoding="utf-8")


def _artifact(p: Path, name: str) -> str:
    return (p.parent / "artifacts" / name).read_text(encoding="utf-8")


# ── status and impact ────────────────────────────────────────────────────────────


def test_status_json_payload_is_rich_enough_for_a_client():
    """`status` carries the whole picture, so no client rebuilds the presentation logic."""
    slug = "clitest-status-json"
    store.create_session(slug, "req")
    SessionService().update_model(slug, {
        **full_model(workflow=slot(90, "explicit", "high"), business_rules=slot(30, "explicit", "high")),
        "questions": [{"q": "How are exceptions handled?", "slot": "business_rules", "why": "u×i"}],
        "summary": {"objective": "obj"}})
    with _model_in_out("clitest-status") as p:
        assert "UNDERSTANDING" in run_cli(["status", str(p)])  # no client built
    st = SessionService().status(slug)
    assert set(st) >= {"understanding", "questions", "summary", "remaining_gaps", "context_cards", "artifacts", "readiness", "revision"}
    assert st["questions"][0]["slot"] == "business_rules" and st["questions"][0]["label"]
    assert st["summary"]["objective"] == "obj"
    # A confirmed-but-thin high-impact slot is still a gap, flagged `thin` in the understanding view.
    assert "business_rules" in {g["slot"] for g in st["remaining_gaps"]}
    assert any(e["slot"] == "business_rules" for grp in st["understanding"].values() for e in grp if e["thin"])


@pytest.mark.parametrize("verb", ["status", "impact"])
def test_status_with_no_argument_matches_the_explicit_slug_when_there_is_one_session(verb):
    """#541: no session -> exit 1 naming `run` (a plumbing verb keeps its slug, exit 2); one -> the identical payload."""
    code, err = run_cli_fails([verb])
    assert code == 1 and "run" in err
    assert run_cli_fails(["session", "show"])[0] == 2
    seed_session("only-session", **_PROBLEM)
    tail = ["--json"] if verb == "status" else []
    assert run_cli([verb, *tail]) == run_cli([verb, "only-session", *tail])


def test_status_with_no_argument_and_several_sessions_lists_them_with_the_default_marked():
    """#541: several sessions -> listed, the most recently written one marked and taken as default."""
    seed_session("older", **_PROBLEM)
    seed_session("newer", "a second request", **_PROBLEM)
    forge_meta("newer", {"updated_at": "2999-01-01T00:00:00Z"})
    text = run_cli(["status"])
    assert "older" in text and "→" in next(ln for ln in text.splitlines() if "newer" in ln)
    assert json.loads(run_cli(["status", "--json"]))["slug"] == "newer"


# ── `requivo demo` and the browsable examples (#223, #225) ───────────────────────


@pytest.fixture(scope="module")
def demo_text() -> str:
    return run_cli(["demo"])   # client=None: the whole verb is offline


def test_the_demo_shows_the_computed_blast_radius_of_a_changed_answer(demo_text):
    """The keyless activation path renders the saved model, the brief, then the change-impact step (#223)."""
    for needle in ("REQUIVO — DEMO", "freelancers to check guests in", "UNDERSTANDING", "DECISION BRIEF", "epic.md",
                   "④ CHANGE ONE ANSWER", "Computed, not generated", "IMPACT — what rests on: Constraints",
                   "DECISIONS TO RE-VALIDATE", "PREMISES TO RE-EXAMINE", "ARTIFACTS THAT GO STALE"):
        assert needle in demo_text, needle
    assert demo_text.index("DECISION BRIEF") < demo_text.index("④ CHANGE ONE ANSWER")
    # Step ④'s prose names the slot `DEMO_CHANGED_SLOT` changes, in words.
    assert "six weeks" in load_model(DEMO / "model.json").model[DEMO_CHANGED_SLOT].value.lower()
    assert "six-week deadline" in demo_text


def test_the_demo_points_a_wheel_install_at_something_it_can_reach(demo_text):
    """The closing block names keyless verbs, one URL a wheel install can open, and the one command that needs a key (#223, #225)."""
    tail = demo_text[demo_text.index("⑤ EVERYTHING ELSE"):]
    keyless = tail[tail.index("still no API key"):tail.index("With a key")]
    assert "requivo web" in keyless and "requivo impact" in keyless
    assert "https://github.com/jbkkz/requivo/tree/main/examples/event-checkin-reconciliation" in tail
    for line in tail.splitlines():
        if "examples/" in line and "https://" not in line:
            assert "requivo impact" in line, f"unlabelled repo-relative path in the demo tail: {line!r}"
    paid = tail[tail.index("With a key:"):]
    assert "requivo discover" in paid and "ANTHROPIC_API_KEY" in paid and "[anthropic]" in paid


def test_demo_payload_matches_the_browsable_example():
    """`requivo demo` replays a payload bundled in the wheel, byte-identical to `examples/`."""
    bundled = sorted(DEMO.glob("*"))
    assert bundled, "demo payload is empty"
    for f in bundled:
        assert f.read_text(encoding="utf-8") == (EXAMPLES / "event-checkin-reconciliation" / f.name).read_text(encoding="utf-8"), f.name


def test_the_browsable_examples_deterministic_half_matches_the_renderer():
    """#172: event-checkin's assessment is a terminal capture; its deterministic half must match today's renderer."""
    example_dir = EXAMPLES / "event-checkin-reconciliation"
    out = load_model(example_dir / "model.json")
    assessment = _fenced_text((example_dir / "solution-assessment.md").read_text(encoding="utf-8"))
    lines = assessment.splitlines()
    draft = bool(readiness_blockers(out))
    assert lines[1].strip() == ("DRAFT DECISION BRIEF" if draft else "DECISION BRIEF"), "the example's banner is stale"
    assert (lines[2].strip() if draft else None) == (DRAFT_NOTE if draft else None), "the example's draft sub-line is stale"
    live = printed(render_readiness, out).rstrip("\n")
    captured = ("ARE WE READY?" + assessment.split("ARE WE READY?", 1)[1]).rstrip("\n")
    assert captured == live, f"the example's readiness block is stale.\n--- captured ---\n{captured}\n--- live ---\n{live}"


def test_the_leave_approval_brief_still_projects_its_own_model():
    """The canonical example's brief is half a projection of its model, and `impact` on it has something to say (#223)."""
    example_dir = EXAMPLES / "leave-approval"
    out = load_model(example_dir / "model.json")
    brief = (example_dir / "solution-assessment.md").read_text(encoding="utf-8")

    def section(heading):
        body = brief.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
        return [ln for ln in body.splitlines() if ln.startswith("- **")]

    assert section("What is confirmed") == _stated(out, Confidence.explicit)
    assert section("Important assumptions") == _stated(out, Confidence.inferred)
    draft = " — Draft: unresolved topics remain" if readiness_blockers(out) else ""
    assert brief.splitlines()[0] == f"# Decision Brief{draft}"
    assert f"**Objective:** {out.summary.objective}" in brief
    assert out.decisions and out.challenges, "the canonical example carries no reasoning layer"
    hit = propagate(out, ["integrations"])
    assert hit.decisions and hit.challenges and hit.artifacts, "nothing rests on the integration topic"


# ── the generators ───────────────────────────────────────────────────────────────

_CRITERIA = {"title": "X", "features": [{"name": "Requesting leave", "scenarios": [
    {"id": "SC-1", "title": "Manager approves", "when": "the manager approves", "then": ["the request is marked approved"]}]}]}
_EPIC = {"title": "X", "issues": [{"id": "I-1", "title": "Build the request form"}]}
_STORIES = json.dumps({"stories": [{"id": "S1", "title": "T"}]})
_ESTIMATE = json.dumps({"items": [{"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]})
_BRIEF = json.dumps({"complexity": "low", "solution": "S"})
_PRD = '{"title": "X", "problem": "P"}'


@pytest.mark.parametrize("argv, replies, files, shown", [
    (["brief"], [_BRIEF], ["solution-assessment.md"], "DECISION BRIEF"),
    (["stories"], [_STORIES], ["stories.md"], "[S1] T"),
    (["estimate"], [_STORIES, _ESTIMATE], ["estimate.md"], "=== ESTIMATE"),
    (["prd"], [_PRD], ["prd.md"], ""),
    (["criteria"], [json.dumps(_CRITERIA)], ["acceptance-criteria.md"], ""),
    (["epic", "--export-json", "--github", "--gitlab"], [json.dumps(_EPIC)], ["epic.md", "epic.json", "epic.github.json", "epic.gitlab.json"], ""),
    (["release", "v1.0"], [json.dumps({"title": "X"})], ["release-notes.md"], ""),
], ids=["brief", "stories", "estimate", "prd", "criteria", "epic", "release"])
def test_pc_generators_write_their_artifacts_through_the_injected_client(argv, replies, files, shown):
    """Every generator verb renders and writes its artifact(s); the brief also records which prompt reasoned."""
    verb, *rest = argv
    with _model_in_out(f"clitest-{verb}") as p:
        assert shown in run_cli([verb, p.parent.name, *rest], client=FakeClient(*replies))
        for name in files:
            assert (p.parent / "artifacts" / name).exists(), name
        if verb == "prd":
            assert _artifact(p, "prd.md").startswith("# X")
        if verb == "release":
            assert "v1.0" in _artifact(p, "release-notes.md")
        listed = ArtifactService().list(p.parent.name)[verb]
        assert listed["stale"] is False and listed["revision"] >= 1
        if verb == "brief":   # the one generator that mints a revision, so the one with provenance to check
            rec = store.read_meta(p.parent.name).revisions[-1]
            assert rec.surface == "cli-brief" and rec.provider == "anthropic"
            assert rec.prompt_version and rec.prompt_version.startswith("sha256:")


def test_pc_brief_persists_reasoning_into_model():
    """Keystone: the brief's reasoning is absorbed into the saved model."""
    with _model_in_out("clitest-brief-persist") as p:
        run_cli(["brief", p.parent.name], client=FakeClient(json.dumps({
            "complexity": "high", "decisions": [{"decision": "draft-first", "tradeoff": "review step"}],
            "challenges": [{"headline": "Archive vs delete", "premise": "pr", "alternative": "al", "consequence": "co", "recommendation": "re"}],
            "opportunities": [{"text": "reuse engine", "leverage": "high", "modules": ["Invoicing"]}]})))
        reloaded = load_model(p)
        assert reloaded.challenges[0].headline == "Archive vs delete" and reloaded.decisions[0].decision == "draft-first"
        assert reloaded.opportunities[0].modules == ["Invoicing"]


def test_the_estimate_verb_reads_stories_and_estimate_from_one_snapshot(monkeypatch):
    """`estimate` makes two provider calls and the second is read against the first's output (#135)."""
    taken = []
    real = SessionService.snapshot
    monkeypatch.setattr(SessionService, "snapshot", lambda self, slug: (taken.append(slug), real(self, slug))[1])
    fake = FakeClient(_STORIES, _ESTIMATE)
    with _model_in_out("clitest-estimate-snapshot") as p:
        run_cli(["estimate", p.parent.name], client=fake)
    assert len(fake.calls) == 2, "both provider calls have to happen or the count below proves nothing"
    assert taken == ["clitest-estimate-snapshot"], f"{len(taken)} snapshots for one analysis"


def test_pc_epic_export_stamps_the_same_revision_the_paired_epic_md_was_saved_against():
    """#274: `epic.json` is the machine-consumed input an n8n flow acts on and needs provenance."""
    slug = "clitest-epic-revision"
    with _model_in_out(slug) as p:
        store.save_revision(slug, _built_model(_PROBLEM))   # past revision 1
        assert store.read_meta(slug).current_revision == 2
        run_cli(["epic", p.parent.name, "--export-json", "--github", "--gitlab"], client=FakeClient(json.dumps(_EPIC)))
        epic_revision = store.read_meta(slug).artifact_status["epic"].revision
        assert epic_revision == 2
        for name in ("epic.json", "epic.github.json", "epic.gitlab.json"):
            assert json.loads(_artifact(p, name))["source_revision"] == epic_revision
        assert json.loads(_artifact(p, "epic.json"))["slug"] == slug


# ── `requivo docs` (#544): one verb over the seven generators ─────────────────────


def test_docs_stories_and_estimate_together_write_stories_once():
    """Exactly two replies: stories, then the estimate read against them; `docs <slug> prd` matches `prd`."""
    fake = FakeClient(_STORIES, _ESTIMATE)
    with _model_in_out("clitest-docs-estimate") as p:
        run_cli(["docs", p.parent.name, "stories", "estimate"], client=fake)
        assert (p.parent / "artifacts" / "stories.md").exists() and (p.parent / "artifacts" / "estimate.md").exists()
    assert len(fake.calls) == 2, "stories must be reasoned and saved exactly once"
    with _model_in_out("clitest-docs-prd-a") as pa, _model_in_out("clitest-docs-prd-b") as pb:
        run_cli(["docs", pa.parent.name, "prd"], client=FakeClient(_PRD))
        run_cli(["prd", pb.parent.name], client=FakeClient(_PRD))
        assert _artifact(pa, "prd.md") == _artifact(pb, "prd.md")
        sa, sb = (store.read_meta(x.parent.name).artifact_status["prd"] for x in (pa, pb))
        assert (sa.revision, sa.stale) == (sb.revision, sb.stale)


def test_resolve_doc_types_refuses_an_unknown_type_before_any_call():
    """The command-line pick is validated first, and `estimate` already writes the stories (#544)."""
    with pytest.raises(RequivoError):
        _resolve_doc_types(["prd", "nope"])
    assert _resolve_doc_types(["prd", "epic"]) == ["prd", "epic"]
    assert _doc_generation_order(["estimate", "stories"]) == ["estimate"]
    assert _doc_generation_order(["release", "brief"]) == ["brief", "release"]
    assert _doc_generation_order(["stories"]) == ["stories"]


def test_prompt_doc_selection_refuses_an_unknown_token_before_any_call(monkeypatch):
    """The interactive pick parses numbers, names and `all`, and refuses a bad token before any generator runs (invariant 3)."""
    for typed, picked in (("2, stories", ["prd", "stories"]), ("all", list(DOC_TYPES)), ("", None)):
        monkeypatch.setattr("builtins.input", lambda prompt="", typed=typed: typed)
        assert _prompt_doc_selection() == picked
    monkeypatch.setattr("builtins.input", lambda prompt="": "prd, nope")
    with pytest.raises(RequivoError):
        _prompt_doc_selection()


def test_docs_all_flag_generates_every_document_skipping_the_menu():
    replies = ['{"complexity": "low"}', _PRD, _STORIES, _ESTIMATE, json.dumps(_CRITERIA), json.dumps(_EPIC),
               '{"title": "X", "summary": "S", "highlights": ["H"], "notes": ["N"]}']
    with _model_in_out("clitest-docs-all") as p:
        run_cli(["docs", p.parent.name, "--all"], client=FakeClient(*replies))
        for name in ("solution-assessment.md", "prd.md", "stories.md", "estimate.md", "acceptance-criteria.md", "epic.md", "release-notes.md"):
            assert (p.parent / "artifacts" / name).exists(), name


@pytest.mark.parametrize("argv, message", [
    (["docs", "{slug}", "bogus"], "neither a document type nor a session"),
    (["docs", "no-such-session", "--all"], "neither a document type nor a session"),
    (["docs", "{slug}", "prd", "--all"], "--all takes no types"),
], ids=["unknown-type", "unknown-token-with-all", "all-plus-type"])
def test_docs_all_refuses_a_token_that_names_neither_a_type_nor_a_session(argv, message):
    """#544 and its review: every malformed selection is refused before any call, and touches no other session."""
    fake = FakeClient()
    with _model_in_out("clitest-docs-refusal") as other:
        code, err = run_cli_fails([a.replace("{slug}", other.parent.name) for a in argv], client=fake)
        assert code == 1 and message in err and fake.calls == []
        artifacts_dir = other.parent / "artifacts"
        assert not artifacts_dir.exists() or not any(artifacts_dir.iterdir())


def test_docs_revision_zero_has_no_menu_and_points_at_run(monkeypatch):
    """A revision-0 session gets no menu; the bundled example shows the brief up to date and six not generated."""
    from requivo.web.example import seed_example

    store.create_session("clitest-docs-empty", "A request.")
    out_text = run_cli(["docs", "clitest-docs-empty"])
    assert "DOCUMENTS" not in out_text and "requivo run clitest-docs-empty" in out_text
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    lines = run_cli(["docs", seed_example(SessionService())]).splitlines()
    assert "up to date" in next(ln for ln in lines if "Decision brief" in ln)
    assert len([ln for ln in lines if "not generated" in ln]) == 6


def test_docs_menu_rows_state_reads_artifact_status_not_revision_arithmetic():
    """A rev-1 artifact on a rev-2 session whose change touched none of its slots still reads *up to date*."""
    slug = seed_session("clitest-docs-menu-rows", "A request.", **_PROBLEM)
    ArtifactService().save(slug, "criteria", "# Criteria\n", source_revision=1)
    store.save_revision(slug, _built_model({"problem": slot(90, "explicit", "high")}))
    meta = store.read_meta(slug)
    assert meta.current_revision == 2
    rows = {r.doc_type: r.state for r in docs_menu_rows(meta.artifact_status)}
    assert rows["criteria"].startswith("up to date (rev 1") and rows["brief"] == "not generated", rows


# ── `discover --once` and its request argument ────────────────────────────────────


def test_pc_discover_prints_the_default_cards_before_the_paid_call(monkeypatch):
    """`--once` saves a resumable session (#72); the default names every installed card, an explicit selection only its own (#257)."""
    output = run_cli(["discover", "clitest discover default cards", "--once"], client=FakeClient(*_DISCOVER))
    folder = store.canonical_dir(slug := _only_slug())
    assert (folder / "model.json").exists() and (folder / "request.md").exists()   # `answer` can resume
    assert store.read_meta(slug).current_revision == 1 and store.read_meta(slug).provider == "anthropic"
    cards = available_cards()
    assert cards, "no bundled context cards found -- this test is not exercising anything"
    for name in cards:
        assert name in output, f"{name!r} (a real installed card) is not named in the pre-call output"
    assert store.read_meta(slug).context_cards is None
    output = run_cli(["discover", "clitest discover explicit cards", "--once", "--context", "b2b-platform"], client=FakeClient(*_DISCOVER))
    assert "Context cards: b2b-platform" in output and "document-management" not in output
    monkeypatch.setattr("requivo.cli.average_card_byte_size", lambda: None)
    output = run_cli(["discover", "clitest discover no measurable weight", "--once"], client=FakeClient(*_DISCOVER))
    assert "measurable weight" in output and "bytes each" not in output


def test_discover_file_check_survives_a_real_length_request(tmp_path):
    """A paragraph-long request (#301), a blank one and a directory are not files; a readable file is."""
    assert is_file_argument("When a contract is signed we want everything to reconcile. " * 20) is False
    assert is_file_argument("") is False and is_file_argument("   \n\t ") is False
    assert is_file_argument(str(tmp_path)) is False
    f = tmp_path / "request.md"
    f.write_text("Build a leave approval system.", encoding="utf-8")
    assert is_file_argument(str(f)) is True


@pytest.mark.parametrize("blank", ["", "   "])
def test_pc_discover_rejects_empty_request(blank):
    assert run_cli_fails(["discover", blank], client=FakeClient(_ENGINE_REPLY))[0] != 0


class _Tty(io.StringIO):
    """Stdin as an interactive terminal: everything a pipe is, except `isatty()`."""

    def isatty(self):
        return True


def test_discover_reads_the_request_from_stdin_when_the_argument_is_a_dash(monkeypatch):
    """`discover -` reads stdin like every document-taking verb (#360), and a paid call carries that text."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("We would like a leave approval system."))
    fake = FakeClient(*_DISCOVER)
    run_cli(["discover", "-"], client=fake)
    assert "leave approval system" in _saved_request(_only_slug())
    assert "leave approval system" in json.dumps(fake.calls[0])


@pytest.mark.parametrize("stdin, code", [(_Tty(""), 1), (io.StringIO("   \n "), 2)], ids=["terminal", "empty"])
def test_a_dash_with_a_terminal_on_stdin_is_refused_rather_than_discovered_on(monkeypatch, stdin, code):
    """A terminal on stdin, or nothing on it, is refused before the provider (#360)."""
    monkeypatch.setattr(sys, "stdin", stdin)
    fake = FakeClient(*_DISCOVER)
    assert run_cli_fails(["discover", "-"], client=fake)[0] == code
    assert fake.calls == []


@pytest.mark.parametrize("argument, stdin, file_text, expect", [
    ("x", "PIPED TEXT THAT MUST NOT BE READ", None, "x"),
    ("-", "We would like a leave approval system.", "FILE CONTENT THAT MUST NOT BE READ", "leave approval system"),
    ("./-", "STDIN THAT MUST NOT BE READ", "We would like a leave approval system.", "leave approval system"),
    ("Leave Approval v3.md", "PIPED TEXT THAT MUST NOT BE READ", "We would like a leave approval system.", "leave approval system"),
], ids=["one-char-literal", "dash-beside-a-file", "path-ending-in-dash", "file-path"])
def test_a_dash_is_stdin_even_when_a_file_of_that_name_exists(monkeypatch, tmp_path, argument, stdin, file_text, expect):
    """`-` is stdin, `./-` and a path are files, and a one-character request is literal text (#360)."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    if file_text is not None:
        (cwd / Path(argument).name).write_text(file_text, encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    run_cli(["discover", argument], client=FakeClient(*_DISCOVER))
    slug = _only_slug()
    assert expect in _saved_request(slug).strip() and "MUST NOT BE READ" not in _saved_request(slug)
    if argument == "-":
        assert slug != "discovery" and "leave" in slug, "the slug came from the unread file's stem"
    if argument.endswith(".md"):
        assert slug == "leave-approval-v3", "a filename is a suggestion for the slug, not a slug"


def test_pc_answer_refines_the_model():
    """A stateless discovery turn: answers + the current model -> a refined model."""
    with _model_in_out("clitest-answer") as p:
        fake = FakeClient(json.dumps(full_model(problem=slot(95, "explicit", "high"))))
        run_cli(["answer", p.parent.name, "The approver is HR, and the circuit is per-client."], client=fake)
        sent = fake.calls[0]["messages"]
        assert "The approver is HR" in sent[-1]["content"] and "problem" in sent[1]["content"]
        reloaded = load_model(p)
        assert reloaded.model["problem"].completeness == 95 and reloaded.model["problem"].confidence.value == "explicit"


def test_requivo_package_is_importable_and_versioned():
    """The rename Product Copilot -> Requivo is complete: the old package is gone, `--help` exits 0."""
    import requivo
    assert requivo.__version__ and callable(app)
    with pytest.raises(ModuleNotFoundError):
        __import__("product_copilot")
    assert run_cli_fails(["--help"])[0] == 0


# ── `requivo --help` is the first screen (#244, #546) ─────────────────────────────

API_VERBS = (set(_OP_PROMPTS) - {"analyze"}) | {"discover", "answer", "run", "docs"}   # the paid verbs (#540)
MARKER = "(API)"
PLUMBING = {"doctor", "schema", "context", "session", "model", "artifact"}
_GROUPS = {"start": _HELP_GROUP_START, "scripts": _HELP_GROUP_SCRIPTS, "plumbing": _HELP_GROUP_PLUMBING}


def _subcommands() -> list:
    """(name, help) in registration order, which is not the rendered order (#546)."""
    for action in _build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return [(c.dest, c.help or "") for c in action._choices_actions]
    raise AssertionError("_build_parser() registers no subcommands")


def test_start_here_leads_the_rendered_help():
    """#546: the first names a reader meets are `demo` and `run`, never `discover`; the epilog survives argparse's reflow."""
    text = _build_parser().format_help()
    start, scripts, plumbing = text.index("Start here:"), text.index("For scripts and integrations"), text.index("Plumbing:")
    assert start < scripts < plumbing, "the three groups render out of order"
    start_section = text[start:scripts]
    assert re.search(r"^\s*demo\s", start_section, re.MULTILINE) and re.search(r"^\s*run\s", start_section, re.MULTILINE)
    assert not re.search(r"^\s*discover\s", start_section, re.MULTILINE), "discover leaked onto the first screen"
    epilog = _build_parser().epilog or ""
    assert "requivo demo" in epilog and "requivo run" in epilog and MARKER in epilog and "ANTHROPIC_API_KEY" in epilog
    assert "\n  requivo demo" in text and "\n  requivo run " in text


def test_every_registered_verb_appears_in_exactly_one_help_group():
    """The completeness half of #546's acceptance criterion: every existing verb appears exactly once."""
    order = [name for name, _ in _subcommands()]
    seen = {name: [label for label, group in _GROUPS.items() if name in group] for group in _GROUPS.values() for name in group}
    duplicated = {name: labels for name, labels in seen.items() if len(labels) > 1}
    assert duplicated == {}, f"a verb is in more than one --help group: {duplicated}"
    assert set(seen) == set(order), f"missing: {sorted(set(order) - set(seen))}; unregistered: {sorted(set(seen) - set(order))}"


def test_the_deterministic_package_still_registers_every_verb():
    """Moving `register_deterministic(sub)` down the function must not weaken its own guard."""
    assert PLUMBING <= {name for name, _ in _subcommands()}


def test_the_plumbing_verbs_come_after_the_journey_verbs_in_registration_order():
    """Registration order, not rendered order (#546 separated the two)."""
    order = [name for name, _ in _subcommands()]
    first_plumbing = min(order.index(name) for name in PLUMBING)
    assert all(order.index(v) < first_plumbing for v in ("run", "discover", "answer", "status", "brief")), order


@pytest.mark.parametrize("columns", ["60", "100", "200"])
def test_every_verb_help_is_byte_identical_regardless_of_the_root_formatter(monkeypatch, columns):
    """`requivo <verb> --help` is unchanged by the grouped root formatter (#546)."""
    monkeypatch.setenv("COLUMNS", columns)
    grouped_sub = next(a for a in _build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    plain_sub = next(a for a in _build_parser(formatter_class=argparse.HelpFormatter)._actions if isinstance(a, argparse._SubParsersAction))
    assert set(grouped_sub.choices) and set(grouped_sub.choices) == set(plain_sub.choices)   # must fire
    changed = sorted(n for n, sp in grouped_sub.choices.items() if sp.format_help() != plain_sub.choices[n].format_help())
    assert changed == [], f"`requivo <verb> --help` changed for: {changed}"


def test_every_paid_verb_in_a_compact_group_still_shows_the_marker():
    """Both directions of the marker (#546): every paid verb carries it, no free verb does, and the compact groups keep it."""
    helps = dict(_subcommands())
    marked = {name for name, text in helps.items() if MARKER in text}
    assert marked == API_VERBS, f"marked but free: {sorted(marked - API_VERBS)}; paid but unmarked: {sorted(API_VERBS - marked)}"
    assert not any(MARKER in helps[verb] for verb in ("status", "impact", "demo", *PLUMBING))
    text = _build_parser().format_help()
    paid_outside_start = [name for name in (*_HELP_GROUP_SCRIPTS, *_HELP_GROUP_PLUMBING) if MARKER in helps[name]]
    assert paid_outside_start   # must fire: vacuous otherwise
    for name in paid_outside_start:
        assert f"{name} {MARKER}" in text, f"{name} lost its {MARKER} marker in the rendered --help"


# ── `status` ends by naming the next command, once (#246) ─────────────────────────

_SLUG = "leave-approval"
_STALE_BRIEF = {"brief": {"revision": 1, "filename": "solution-assessment.md", "stale": True}}


def _payload(*, questions=0, ready=True, artifacts=None, perimeter=None) -> dict:
    return {"slug": _SLUG, "readiness": {"ready": ready, "blocking_slots": []}, "perimeter": perimeter,
            "questions": [{"q": f"Q{i}", "slot": "problem", "label": "L", "why": "w"} for i in range(questions)],
            "artifacts": artifacts if artifacts is not None else {}}


def test_open_questions_point_at_answer():
    """The order is a judgment: questions, then a stale artifact, then a missing brief, then nothing."""
    from requivo.core.perimeters import GO_TO_MARKET

    answer = f'requivo answer {_SLUG} "<your answers>"'
    assert next_command(_payload(questions=3, ready=False)) == answer
    assert next_command(_payload(questions=3, artifacts=_STALE_BRIEF)) == answer
    stale = {**_STALE_BRIEF, "prd": {"revision": 2, "filename": "prd.md", "stale": False}}
    assert next_command(_payload(artifacts=stale)) == f"requivo brief {_SLUG}   (regenerates solution-assessment.md; requivo impact {_SLUG} shows what else moved)"
    assert next_command(_payload()) == f"requivo brief {_SLUG}"
    assert next_command(_payload(perimeter=GO_TO_MARKET)) == f"requivo gtm_plan {_SLUG}"   # #608/#609: no brief to point at
    assert next_command(_payload(artifacts={"brief": {"revision": 3, "filename": "solution-assessment.md", "stale": False}})) is None
    assert next_command({"slug": "x", "questions": [], "readiness": {"ready": True}}) is None   # a bare model file


def test_the_human_status_view_ends_with_exactly_one_pointer():
    """The line is there once, at the end, and the `--json` payload is untouched by it."""
    store.create_session(_SLUG, "A leave approval system")
    model = {**full_model(), "questions": [{"q": "How are approvals routed today?", "slot": "problem", "why": "w"}]}
    store.save_revision(_SLUG, EngineOutput.model_validate(model))
    text = run_cli(["status", _SLUG])
    pointers = [ln for ln in text.splitlines() if ln.lstrip().startswith("→ requivo")]
    assert pointers == [f'→ requivo answer {_SLUG} "<your answers>"'], text
    assert text.rstrip().endswith(pointers[0])
    raw = run_cli(["status", _SLUG, "--json"])
    assert "requivo answer" not in raw and set(json.loads(raw)) >= {"slug", "readiness", "understanding", "questions", "summary"}
