"""The `requivo` subcommand surface, end to end and offline (#72)."""
import io
import json
import shutil
import sys

import pytest
from _fakes import _ENGINE_REPLY, _JUDGMENT_REPLY, _ROUTING_REPLY, FakeClient, _model_in_out, _run_app, full_slots, slot
from _fakes import out as _built_model

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.context import available_cards
from requivo.core.contracts import EngineOutput
from requivo.core.persistence import load_model
from requivo.deterministic import is_file_argument
from requivo.services.artifacts import ArtifactService


@pytest.fixture(autouse=True)
def _isolate_workspace(workspace):
    """Every test in this module writes sessions/artifacts into an isolated temp workspace."""


def test_pc_status_runs_offline():
    with _model_in_out("clitest-status") as p:
        assert "UNDERSTANDING" in _run_app(["status", str(p)])  # no client built


def test_status_json_payload_is_rich_enough_for_a_client():
    # status(slug) must carry the full picture — understanding, questions, gaps, summary, context — so Claude Code and a future Web client render it without rebuilding the presentation logic.
    from requivo.services.sessions import SessionService
    slug = "clitest-status-json"
    store.create_session(slug, "req")
    model = EngineOutput.model_validate({
        "model": full_slots(workflow=slot(90, "explicit", "high"),
                            business_rules=slot(30, "explicit", "high")),   # explicit but thin
        "questions": [{"q": "How are exceptions handled?", "slot": "business_rules", "why": "u×i"}],
        "summary": {"objective": "obj"},
    })
    SessionService().update_model(slug, model.model_dump())
    try:
        st = SessionService().status(slug)
        assert set(st) >= {"understanding", "questions", "summary", "remaining_gaps",
                           "context_cards", "artifacts", "readiness", "revision"}
        assert st["questions"][0]["slot"] == "business_rules" and st["questions"][0]["label"]
        assert st["summary"]["objective"] == "obj"
        # confirmed-but-thin high-impact slot: still a gap, and flagged `thin` in the understanding view
        assert "business_rules" in {g["slot"] for g in st["remaining_gaps"]}
        thin = [e for grp in st["understanding"].values() for e in grp if e["thin"]]
        assert any(e["slot"] == "business_rules" for e in thin)
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


# ── #541: `run`/`status`/`impact` default to the workspace's session when the slug is omitted ─────


def test_status_with_no_argument_matches_the_explicit_slug_when_there_is_one_session():
    """#541: one session -> that one, with the identical `--json` payload either way."""
    store.create_session("only-session", "a request")
    store.save_revision("only-session", _built_model({"problem": slot(80, "explicit", "high")}))
    explicit = _run_app(["status", "only-session", "--json"])
    implicit = _run_app(["status", "--json"])
    assert json.loads(implicit) == json.loads(explicit)


def test_status_with_no_argument_and_several_sessions_lists_them_with_the_default_marked():
    """#541."""
    store.create_session("older", "a request")
    store.save_revision("older", _built_model({"problem": slot(80, "explicit", "high")}))
    store.create_session("newer", "a second request")
    store.save_revision("newer", _built_model({"problem": slot(80, "explicit", "high")}))
    p = store.canonical_dir("newer") / "session.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["updated_at"] = "2999-01-01T00:00:00Z"
    p.write_text(json.dumps(data), encoding="utf-8")

    printed = _run_app(["status"])
    assert "older" in printed and "newer" in printed
    default_line = next(ln for ln in printed.splitlines() if "newer" in ln)
    assert "→" in default_line

    payload = json.loads(_run_app(["status", "--json"]))
    assert payload["slug"] == "newer"


def test_status_with_no_argument_and_no_session_exits_1_naming_run(capsys):
    """#541: none -> exit 1, naming `run` rather than argparse's own missing-argument usage error."""
    with pytest.raises(SystemExit) as exit_:
        app(["status"], client=None)
    assert exit_.value.code == 1
    assert "run" in capsys.readouterr().err


def test_impact_with_no_argument_matches_the_explicit_slug():
    """#541: `impact` resolves the same default session `status` does."""
    store.create_session("only-session", "a request")
    store.save_revision("only-session", _built_model({"problem": slot(80, "explicit", "high")}))
    explicit = _run_app(["impact", "only-session"])
    implicit = _run_app(["impact"])
    assert implicit == explicit


def test_session_show_with_no_slug_still_refuses():
    """#541: plumbing verbs keep their slug required."""
    with pytest.raises(SystemExit) as exit_:
        app(["session", "show"], client=None)
    assert exit_.value.code == 2


def test_pc_demo_runs_offline_from_saved_example():
    # The activation path: a visitor runs `requivo demo` with no key.
    text = _run_app(["demo"])  # client=None
    assert "REQUIVO — DEMO" in text
    assert "freelancers to check guests in" in text     # the real request is shown
    assert "UNDERSTANDING" in text                       # status rendered live from the saved model
    assert "DECISION BRIEF" in text                      # the deliverable (the differentiator)
    assert "epic.md" in text                             # the other artifacts are pointed to


def test_the_demo_shows_the_computed_blast_radius_of_a_changed_answer():
    """The demo used to end at the decision brief (#223)."""
    text = _run_app(["demo"])  # client=None — the whole step is offline
    assert "④ CHANGE ONE ANSWER" in text
    assert "Computed, not generated" in text
    assert "IMPACT — what rests on: Constraints" in text
    assert "DECISIONS TO RE-VALIDATE" in text
    assert "PREMISES TO RE-EXAMINE" in text
    assert "ARTIFACTS THAT GO STALE" in text
    # The brief comes before it: the change-impact step is the answer to the brief, not a preamble.
    assert text.index("DECISION BRIEF") < text.index("④ CHANGE ONE ANSWER")


def test_the_demo_prose_describes_the_slot_it_actually_changes():
    """Step ④'s prose names the deadline in words; `DEMO_CHANGED_SLOT` names it as a slot id (#223)."""
    from requivo.cli import DEMO_CHANGED_SLOT
    from requivo.core.persistence import load_model
    from requivo.paths import DEMO

    out = load_model(DEMO / "model.json")
    assert "six weeks" in out.model[DEMO_CHANGED_SLOT].value.lower()
    assert "six-week deadline" in _run_app(["demo"])


def test_the_demo_ends_on_something_a_reader_without_a_key_can_do():
    """The demo's premise is that no key is needed, and its closing step used to name only `requivo discover`,
    which requires one (#223)."""
    text = _run_app(["demo"])
    tail = text[text.index("⑤ EVERYTHING ELSE"):]
    keyless = tail[tail.index("still no API key"):tail.index("With a key")]
    assert "requivo web" in keyless
    assert "requivo impact" in keyless
    # `discover` still appears, and is still marked as the one that costs something.
    assert "With a key:" in tail


def test_the_demo_points_a_wheel_install_at_something_it_can_reach():
    """The demo's closing evidence used to be two paths that exist only in a clone (#225)."""
    text = _run_app(["demo"])
    assert "https://github.com/jbkkz/requivo/tree/main/examples/event-checkin-reconciliation" in text
    tail = text[text.index("⑤ EVERYTHING ELSE"):]
    # A bare repo-relative path is allowed only where the line says it needs the repo.
    for line in tail.splitlines():
        if "examples/" in line and "https://" not in line:
            assert "requivo impact" in line, f"unlabelled repo-relative path in the demo tail: {line!r}"


def test_the_demo_names_the_key_requirement_beside_the_command_that_needs_one():
    """The banner promises no key is needed; exactly one command in the closing block needs one (#225)."""
    text = _run_app(["demo"])
    tail = text[text.index("With a key:"):]
    assert "requivo discover" in tail
    assert "ANTHROPIC_API_KEY" in tail
    assert "[anthropic]" in tail


def test_pc_brief_uses_injected_client():
    with _model_in_out("clitest-brief") as p:
        text = _run_app(["brief", p.parent.name], client=FakeClient(json.dumps({"complexity": "low", "solution": "S"})))
        assert "DECISION BRIEF" in text


def test_demo_payload_matches_the_browsable_example():
    # `requivo demo` reads a frozen payload bundled in the package (so it works from a wheel).
    from requivo.paths import DEMO

    repo_root = DEMO.parents[3]  # assets/demo → assets → requivo → src → repo
    browsable = repo_root / "examples" / "event-checkin-reconciliation"
    bundled = sorted(DEMO.glob("*"))
    assert bundled, "demo payload is empty"
    for f in bundled:
        assert f.read_text(encoding="utf-8") == (browsable / f.name).read_text(encoding="utf-8"), f"demo payload drifted from examples/: {f.name}"


def test_the_browsable_examples_deterministic_half_matches_the_renderer():
    # #172: the test above compares the browsable example to its bundled twin.
    import io
    from contextlib import redirect_stdout

    from requivo.cli import _fenced_text
    from requivo.core.analysis import readiness_blockers
    from requivo.core.persistence import load_model
    from requivo.paths import DEMO
    from requivo.render.terminal import DRAFT_NOTE, render_readiness

    repo_root = DEMO.parents[3]
    # event-checkin only: its assessment is a *terminal* capture inside a ```text fence, which is what `requivo demo` replays. leave-approval ships the markdown artifact `requivo brief` writes, so its deterministic half is checked against `brief_markdown` instead, in the test below.
    example_dir = repo_root / "examples" / "event-checkin-reconciliation"
    out = load_model(example_dir / "model.json")
    assessment = _fenced_text((example_dir / "solution-assessment.md").read_text(encoding="utf-8"))
    lines = assessment.splitlines()

    draft = bool(readiness_blockers(out))
    expected_banner = "DRAFT DECISION BRIEF" if draft else "DECISION BRIEF"
    actual_banner = lines[1].strip()
    assert actual_banner == expected_banner, (
        f"the captured example's banner ({actual_banner!r}) disagrees with what render_brief would "
        f"print for this model.json today ({expected_banner!r}) -- the example is stale"
    )
    # The sub-line under the banner is static and unconditioned on any LLM content.
    actual_note = lines[2].strip() if draft else None
    expected_note = DRAFT_NOTE if draft else None
    assert actual_note == expected_note, (
        f"the captured example's draft sub-line ({actual_note!r}) disagrees with DRAFT_NOTE "
        f"({expected_note!r}) -- the example is stale"
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        render_readiness(out)
    expected_readiness = buf.getvalue().rstrip("\n")
    actual_readiness = "ARE WE READY?" + assessment.split("ARE WE READY?", 1)[1]
    actual_readiness = actual_readiness.rstrip("\n")
    assert actual_readiness == expected_readiness, (
        "the captured example's readiness block disagrees with render_readiness() over the same "
        f"model.json.\n--- captured ---\n{actual_readiness}\n--- live ---\n{expected_readiness}"
    )


def test_the_leave_approval_brief_still_projects_its_own_model():
    """The canonical example's decision brief is half a projection (#223)."""
    from requivo.core.analysis import readiness_blockers
    from requivo.core.contracts import Confidence
    from requivo.core.persistence import load_model
    from requivo.paths import DEMO
    from requivo.render.markdown import _stated

    example_dir = DEMO.parents[3] / "examples" / "leave-approval"
    out = load_model(example_dir / "model.json")
    brief = (example_dir / "solution-assessment.md").read_text(encoding="utf-8")

    def section(heading):
        body = brief.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
        return [ln for ln in body.splitlines() if ln.startswith("- **")]

    assert section("What is confirmed") == _stated(out, Confidence.explicit)
    # The assumptions section carries `summary.assumptions` after the projected topics.
    assert section("Important assumptions") == _stated(out, Confidence.inferred)
    # The draft banner is the same rule `brief_markdown` applies, from the same model.
    draft = " — Draft: unresolved topics remain" if readiness_blockers(out) else ""
    assert brief.splitlines()[0] == f"# Decision Brief{draft}"
    assert f"**Objective:** {out.summary.objective}" in brief


def test_the_canonical_example_can_reproduce_the_change_impact_moment():
    """`impact` on the committed leave-approval model has something to say (#223)."""
    from requivo.core.dependencies import propagate
    from requivo.core.persistence import load_model
    from requivo.paths import DEMO

    out = load_model(DEMO.parents[3] / "examples" / "leave-approval" / "model.json")
    assert out.decisions and out.challenges, "the canonical example carries no reasoning layer"
    hit = propagate(out, ["integrations"])
    assert hit.decisions, "no decision rests on the integration topic"
    assert hit.challenges, "no premise contests the integration topic"
    assert hit.artifacts, "no artifact consumes the integration topic"


def test_pc_brief_persists_reasoning_into_model():
    # Keystone: advise()'s reasoning is absorbed into the model and saved (backfill).
    with _model_in_out("clitest-brief-persist") as p:
        brief_json = json.dumps({
            "complexity": "high",
            "decisions": [{"decision": "draft-first", "tradeoff": "review step"}],
            "challenges": [{
                "headline": "Archive vs delete", "premise": "pr",
                "alternative": "al", "consequence": "co", "recommendation": "re",
            }],
            "opportunities": [{"text": "reuse engine", "leverage": "high", "modules": ["Invoicing"]}],
        })
        _run_app(["brief", p.parent.name], client=FakeClient(brief_json))
        reloaded = load_model(p)  # the saved model now carries the reasoning
        assert reloaded.challenges[0].headline == "Archive vs delete"
        assert reloaded.decisions[0].decision == "draft-first"
        assert reloaded.opportunities[0].modules == ["Invoicing"]


def test_pc_stories_renders():
    with _model_in_out("clitest-stories") as p:
        text = _run_app(["stories", p.parent.name], client=FakeClient(json.dumps({"stories": [{"id": "S1", "title": "T"}]})))
        assert "=== USER STORIES ===" in text and "[S1] T" in text


def _estimate_client() -> FakeClient:
    """The two scripted replies `estimate` needs: the stories, then the estimate read against them."""
    return FakeClient(
        json.dumps({"stories": [{"id": "S1", "title": "T"}]}),
        json.dumps({"items": [{"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]}),
    )


def test_pc_estimate_renders():
    with _model_in_out("clitest-estimate") as p:
        assert "=== ESTIMATE" in _run_app(["estimate", p.parent.name], client=_estimate_client())


def test_the_estimate_verb_reads_stories_and_estimate_from_one_snapshot(monkeypatch):
    """`estimate` makes two provider calls and the second is read against the first's output (#135)."""
    from requivo.services.sessions import SessionService

    taken = []
    real = SessionService.snapshot

    def counting(self, slug):
        taken.append(slug)
        return real(self, slug)

    monkeypatch.setattr(SessionService, "snapshot", counting)
    fake = _estimate_client()
    with _model_in_out("clitest-estimate-snapshot") as p:
        _run_app(["estimate", p.parent.name], client=fake)

    assert len(fake.calls) == 2, "both provider calls have to happen or the count below proves nothing"
    assert taken == ["clitest-estimate-snapshot"], (
        f"{len(taken)} snapshots for one analysis — the stories and the estimate can be read against "
        f"two different revisions"
    )


def test_pc_brief_writes_the_artifact_like_every_other_surface():
    # The terminal used to render the assessment and keep it.
    with _model_in_out("clitest-brief-artifact") as p:
        _run_app(["brief", p.parent.name], client=FakeClient(json.dumps({"complexity": "low", "solution": "S"})))
        assert (p.parent / "artifacts" / "solution-assessment.md").exists()
        listed = ArtifactService().list(p.parent.name)["brief"]
        assert listed["stale"] is False and listed["revision"] >= 1


def test_pc_generators_record_which_prompt_reasoned(tmp_path):
    # A revision log that cannot say what produced it cannot reproduce it.
    with _model_in_out("clitest-provenance") as p:
        _run_app(["brief", p.parent.name], client=FakeClient(json.dumps({"complexity": "low"})))
        rec = store.read_meta(p.parent.name).revisions[-1]
        assert rec.surface == "cli-brief" and rec.provider == "anthropic"
        assert rec.prompt_version and rec.prompt_version.startswith("sha256:")


# Minimal-but-legal artifact replies.
_CRITERIA = {"title": "X", "features": [
    {"name": "Requesting leave", "scenarios": [
        {"id": "SC-1", "title": "Manager approves", "when": "the manager approves",
         "then": ["the request is marked approved"]}]}]}
_EPIC = {"title": "X", "issues": [{"id": "I-1", "title": "Build the request form"}]}


def test_pc_prd_writes_artifact():
    with _model_in_out("clitest-prd") as p:
        _run_app(["prd", p.parent.name], client=FakeClient(json.dumps({"title": "X", "problem": "P"})))
        assert (p.parent / "artifacts" / "prd.md").read_text(encoding="utf-8").startswith("# X")


def test_pc_criteria_writes_artifact():
    with _model_in_out("clitest-criteria") as p:
        _run_app(["criteria", p.parent.name], client=FakeClient(json.dumps(_CRITERIA)))
        assert (p.parent / "artifacts" / "acceptance-criteria.md").exists()


def test_pc_epic_writes_all_views():
    with _model_in_out("clitest-epic") as p:
        _run_app(["epic", p.parent.name, "--export-json", "--github", "--gitlab"],
                 client=FakeClient(json.dumps(_EPIC)))
        for name in ("epic.md", "epic.json", "epic.github.json", "epic.gitlab.json"):
            assert (p.parent / "artifacts" / name).exists()


def test_pc_epic_export_stamps_the_same_revision_the_paired_epic_md_was_saved_against():
    """#274: `epic.json` is the machine-consumed input an n8n flow acts on and needs provenance."""
    slug = "clitest-epic-revision"
    with _model_in_out(slug) as p:
        # Bump past revision 1 first, so a test that only ever sees "1" cannot pass by accident.
        store.save_revision(slug, _built_model({"problem": slot(80, "explicit", "high")}))
        assert store.read_meta(slug).current_revision == 2
        _run_app(["epic", p.parent.name, "--export-json", "--github", "--gitlab"],
                 client=FakeClient(json.dumps(_EPIC)))
        epic_revision = store.read_meta(slug).artifact_status["epic"].revision
        assert epic_revision == 2
        for name in ("epic.json", "epic.github.json", "epic.gitlab.json"):
            payload = json.loads((p.parent / "artifacts" / name).read_text(encoding="utf-8"))
            assert payload["source_revision"] == epic_revision
        neutral = json.loads((p.parent / "artifacts" / "epic.json").read_text(encoding="utf-8"))
        assert neutral["slug"] == slug


def test_pc_release_stamps_version():
    with _model_in_out("clitest-release") as p:
        _run_app(["release", p.parent.name, "v1.0"], client=FakeClient(json.dumps({"title": "X"})))
        assert "v1.0" in (p.parent / "artifacts" / "release-notes.md").read_text(encoding="utf-8")


def test_pc_discover_once_saves_model():
    slug = "clitest-discover-probe-xyz"
    _run_app(["discover", "clitest discover probe xyz", "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    folder = store.canonical_dir(slug)
    assert (folder / "model.json").exists()
    assert (folder / "request.md").exists()   # saved so `requivo answer` can resume
    assert store.read_meta(slug).current_revision == 1
    assert store.read_meta(slug).provider == "anthropic"


def test_pc_discover_prints_the_default_cards_before_the_paid_call():
    """#257: the default (no `--context`) reasons over every installed card."""
    from requivo.services.sessions import SessionService

    output = _run_app(["discover", "clitest discover default cards", "--once"],
                       client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    cards = available_cards()
    assert cards, "no bundled context cards found -- this test is not exercising anything"
    for name in cards:
        assert name in output, f"{name!r} (a real installed card) is not named in the pre-call output"
    slug = SessionService().list_sessions()[0].slug
    assert store.read_meta(slug).context_cards is None  # no behavior change: still every card


def test_pc_discover_names_the_fallback_weight_when_the_average_cannot_be_measured(monkeypatch):
    """Found in review."""
    import requivo.cli as cli_module

    monkeypatch.setattr(cli_module, "average_card_byte_size", lambda: None)
    output = _run_app(["discover", "clitest discover no measurable weight", "--once"],
                       client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    assert "measurable weight" in output
    assert "bytes each" not in output


def test_pc_discover_with_explicit_context_does_not_also_print_the_all_cards_line():
    # The "no --context given" disclosure and the existing "Context cards.
    output = _run_app(["discover", "clitest discover explicit cards", "--once",
                       "--context", "b2b-platform"],
                      client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    assert "Context cards: b2b-platform" in output
    assert "document-management" not in output  # a card that was NOT selected must not be named


def test_discover_file_check_survives_a_real_length_request():
    # A real client request is a paragraph — longer than the OS filename limit (#301).
    long_request = "When a contract is signed we want everything to reconcile. " * 20
    assert is_file_argument(long_request) is False


def test_discover_file_check_rejects_blank_arg():
    # Path("") resolves to the current directory, which exists.
    assert is_file_argument("") is False
    assert is_file_argument("   \n\t ") is False


def test_discover_file_check_rejects_a_directory(tmp_path):
    # A directory `exists()` too.
    assert is_file_argument(str(tmp_path)) is False
    f = tmp_path / "request.md"
    f.write_text("Build a leave approval system.")
    assert is_file_argument(str(f)) is True


def test_discover_from_a_file_slugifies_its_name(tmp_path, monkeypatch):
    # A filename is a suggestion for the slug, not a slug.
    from requivo.services.sessions import SessionService

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    req = tmp_path / "Leave Approval v2.md"
    req.write_text("We would like a leave approval system.")
    _run_app(["discover", str(req), "--once"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    assert [m.slug for m in SessionService().list_sessions()] == ["leave-approval-v2"]


def test_pc_discover_rejects_empty_request():
    # An empty/whitespace request should fail fast with a clear message.
    for blank in ("", "   "):
        with pytest.raises(SystemExit):
            _run_app(["discover", blank], client=FakeClient(_ENGINE_REPLY))


# ── `discover -` reads stdin, like every other document-taking verb (#360) ───────────────────
#
# `session init -`, `model apply <slug> -` and `artifact save --file -` all route through `deterministic/_shared.py`, which special-cases a bare `-` as "read the document from stdin".


class _Tty(io.StringIO):
    """Stdin as an interactive terminal: everything a pipe is, except `isatty()`."""

    def isatty(self):
        return True


def _saved_request(slug: str) -> str:
    return (store.canonical_dir(slug) / "request.md").read_text(encoding="utf-8")


def _only_slug() -> str:
    from requivo.services.sessions import SessionService

    slugs = [m.slug for m in SessionService().list_sessions()]
    assert len(slugs) == 1, f"expected exactly one session, got {slugs}"
    return slugs[0]


def test_discover_reads_the_request_from_stdin_when_the_argument_is_a_dash(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("We would like a leave approval system."))
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
    _run_app(["discover", "-"], client=fake)
    assert "leave approval system" in _saved_request(_only_slug())
    # The bug is not only that the text was wrong -- it is that a paid call went out carrying it.
    assert "leave approval system" in json.dumps(fake.calls[0])


def test_a_one_character_request_that_is_not_a_dash_is_still_literal_text(monkeypatch):
    """The must-not-fire half. A fix that read stdin whenever stdin happened to be a pipe would hijack an
    ordinary short request."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("PIPED TEXT THAT MUST NOT BE READ"))
    _run_app(["discover", "x"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    assert _saved_request(_only_slug()).strip() == "x"


def test_a_dash_with_a_terminal_on_stdin_is_refused_rather_than_discovered_on(monkeypatch):
    """`_read_stdin` refuses a terminal rather than hanging on input nobody meant to type."""
    monkeypatch.setattr(sys, "stdin", _Tty(""))
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
    with pytest.raises(SystemExit) as e:
        _run_app(["discover", "-"], client=fake)
    assert e.value.code == 1
    assert fake.calls == []


def test_an_empty_stdin_is_refused_rather_than_discovered_on(monkeypatch):
    """`printf "" | requivo discover -` is the same nothing-to-discover-from case the blank literal request
    above already refuses; it must reach the same refusal rather than the provider."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("   \n "))
    fake = FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)
    with pytest.raises(SystemExit) as e:
        _run_app(["discover", "-"], client=fake)
    assert e.value.code == 2
    assert fake.calls == []


def test_a_dash_is_stdin_even_when_a_file_of_that_name_exists(monkeypatch, tmp_path):
    """The one input where the two halves of `_cmd_discover`'s branch could disagree."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "-").write_text("FILE CONTENT THAT MUST NOT BE READ", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "stdin", io.StringIO("We would like a leave approval system."))
    _run_app(["discover", "-"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    slug = _only_slug()
    assert "leave approval system" in _saved_request(slug)
    assert "MUST NOT BE READ" not in _saved_request(slug)
    # The observable half, and the reason `slug != "-"` would not have been an assertion at all.
    assert slug != "discovery", (
        "the slug came from `slug_hint(Path('-').stem)`, i.e. from a file whose content was never "
        "read, instead of from the request that was actually discovered on")
    assert "leave" in slug


def test_a_path_that_merely_ends_in_a_dash_is_still_a_file(monkeypatch, tmp_path):
    """The must-fire half of the case above: `-` is stdin, and `./-` is a file."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "-").write_text("We would like a leave approval system.", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "stdin", io.StringIO("STDIN THAT MUST NOT BE READ"))
    _run_app(["discover", "./-"], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    assert "leave approval system" in _saved_request(_only_slug())


def test_a_file_path_argument_still_behaves_exactly_as_before(monkeypatch, tmp_path):
    """The third arm of the same branch, kept honest."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("PIPED TEXT THAT MUST NOT BE READ"))
    req = tmp_path / "Leave Approval v3.md"
    req.write_text("We would like a leave approval system.", encoding="utf-8")
    _run_app(["discover", str(req)], client=FakeClient(_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY))
    assert _only_slug() == "leave-approval-v3"
    assert "leave approval system" in _saved_request("leave-approval-v3")


def test_pc_answer_refines_the_model():
    # A stateless discovery turn: answers + the current model → a refined model.
    with _model_in_out("clitest-answer") as p:
        turn2 = json.dumps({
            "model": full_slots(problem=slot(95, "explicit", "high")),
            "questions": [],
            # A discovery reply owes an objective — a session of slots with nothing naming what they are for renders as a blank heading everywhere.
            "summary": {"objective": "A leave approval system"},
        })
        fake = FakeClient(turn2)
        _run_app(["answer", p.parent.name, "The approver is HR, and the circuit is per-client."], client=fake)
        # the answers + the prior model reached the engine turn
        sent = fake.calls[0]["messages"]
        assert "The approver is HR" in sent[-1]["content"]
        assert "problem" in sent[1]["content"]  # prior model carried as assistant turn
        # and the saved model is refined (inferred/80 → explicit/95)
        reloaded = load_model(p)
        assert reloaded.model["problem"].completeness == 95
        assert reloaded.model["problem"].confidence.value == "explicit"


# ── Rename: Product Copilot → Requivo (identity is correct and complete) ───────


def test_requivo_package_is_importable_and_versioned():
    import requivo

    assert requivo.__version__  # a real version string
    from requivo.cli import app  # the entry point resolves
    assert callable(app)


def test_old_package_name_is_gone():
    # The package was renamed, not shimmed — the old import must fail so nothing silently depends on it.
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("product_copilot")


def test_cli_help_exits_cleanly():
    # `requivo --help` prints usage and exits 0 via argparse.
    with pytest.raises(SystemExit) as ei:
        app(["--help"])
    assert ei.value.code == 0

