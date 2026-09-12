"""The `requivo` subcommand surface, end to end and offline.

Split out of `test_engine.py` (#72). The modern surface is a thin layer over the same services;
`app()` takes an injected client so API-backed verbs run against a `FakeClient` and never reach the
network. What each test here pins is the *seam* — that the verb reaches the service, writes the
artifact where every other surface writes it, and records what it read.

The parser-shape tests (which verbs bind, which flags exist) live in `test_cli_flag_names.py`; the
no-LLM verbs live in the `test_cli_*.py` set that mirrors `requivo/deterministic/` — `_doctor`,
`_sessions`, `_session_archives`, `_model`, `_artifacts`, `_shared`, plus `_untrusted_output` for the
render-safety class that runs across all of them (#141).
"""
import argparse
import io
import json
import os
import shutil
import sys

import pytest
from _fakes import _ENGINE_REPLY, FakeClient, _model_in_out, _run_app, full_slots, slot
from _fakes import out as _built_model

from requivo.cli import _cmd_web, app
from requivo.core import persistence as store
from requivo.core.context import available_cards
from requivo.core.contracts import EngineOutput
from requivo.core.errors import RequivoError
from requivo.core.persistence import load_model
from requivo.deterministic import is_file_argument
from requivo.services.artifacts import ArtifactService


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    """Every test in this module writes sessions/artifacts into an isolated temp workspace, never the
    real repo. Points both the canonical root (.requivo/sessions) and the legacy root (out/) at tmp."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


def test_pc_status_runs_offline():
    with _model_in_out("clitest-status") as p:
        assert "UNDERSTANDING" in _run_app(["status", str(p)])  # no client built


def test_status_json_payload_is_rich_enough_for_a_client():
    # status(slug) must carry the full picture — understanding, questions, gaps, summary, context —
    # so Claude Code and a future Web client render it without rebuilding the presentation logic.
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
    """#541: several -> every candidate listed (human mode), the default marked, and the `--json`
    payload's own `slug` names the same pick without a line beside it (#246: nothing may print
    beside a `--json` payload)."""
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
    """#541: plumbing verbs keep their slug required -- a script must never act on 'whichever
    session is newest'."""
    with pytest.raises(SystemExit) as exit_:
        app(["session", "show"], client=None)
    assert exit_.value.code == 2


def test_pc_demo_runs_offline_from_saved_example():
    # The activation path: a visitor runs `requivo demo` with no key, no args, no network, and sees a
    # real run end to end. No client is passed and none is built.
    text = _run_app(["demo"])  # client=None
    assert "REQUIVO — DEMO" in text
    assert "freelancers to check guests in" in text     # the real request is shown
    assert "UNDERSTANDING" in text                       # status rendered live from the saved model
    assert "DECISION BRIEF" in text                      # the deliverable (the differentiator)
    assert "epic.md" in text                             # the other artifacts are pointed to


def test_the_demo_shows_the_computed_blast_radius_of_a_changed_answer():
    """The demo used to end at the decision brief — one beat short of the only step in it that a
    strong prompt cannot also produce (#223).

    Steps ① to ③ are a request, an understanding and a judgment. Step ④ is the dependency graph:
    `propagate` walks the edges the discovery recorded, so the same change yields the same list every
    time. Asserting the *contents* rather than the header is the point — a heading can be printed
    over an empty graph, and an empty blast radius is the shape this step must never silently take.
    """
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
    """Step ④'s prose names the deadline in words; `DEMO_CHANGED_SLOT` names it as a slot id. Nothing
    connects the two but this test, and a payload whose `constraints` stops being about six weeks
    would leave the demo describing a change it does not make (#223)."""
    from requivo.cli import DEMO_CHANGED_SLOT
    from requivo.core.persistence import load_model
    from requivo.paths import DEMO

    out = load_model(DEMO / "model.json")
    assert "six weeks" in out.model[DEMO_CHANGED_SLOT].value.lower()
    assert "six-week deadline" in _run_app(["demo"])


def test_the_demo_ends_on_something_a_reader_without_a_key_can_do():
    """The demo's premise is that no key is needed, and its closing step used to name only
    `requivo discover`, which requires one (#223). A walkthrough that leaves its own audience with
    nothing to do next has spent its whole effect on the last line."""
    text = _run_app(["demo"])
    tail = text[text.index("⑤ EVERYTHING ELSE"):]
    keyless = tail[tail.index("still no API key"):tail.index("With a key")]
    assert "requivo web" in keyless
    assert "requivo impact" in keyless
    # `discover` still appears, and is still marked as the one that costs something.
    assert "With a key:" in tail


def test_the_demo_points_a_wheel_install_at_something_it_can_reach():
    """The demo's closing evidence used to be two paths that exist only in a clone (#225).

    The README's own recommended installs are `uvx`, `uv tool install` and `pipx` — none of which
    leaves a checkout — and step ⑤ proved "everything else is a view" by naming
    `examples/<slug>/epic.md` and `acceptance-criteria.md`. For the majority install path those were
    two dead pointers, in the one place the walkthrough asks to be believed. The files ship inside
    the wheel; what was missing was any way for that reader to reach them.
    """
    text = _run_app(["demo"])
    assert "https://github.com/jbkkz/requivo/tree/main/examples/event-checkin-reconciliation" in text
    tail = text[text.index("⑤ EVERYTHING ELSE"):]
    # A bare repo-relative path is allowed only where the line says it needs the repo — which is the
    # `impact` command, and nothing else in this block.
    for line in tail.splitlines():
        if "examples/" in line and "https://" not in line:
            assert "requivo impact" in line, f"unlabelled repo-relative path in the demo tail: {line!r}"


def test_the_demo_names_the_key_requirement_beside_the_command_that_needs_one():
    """The banner promises no key is needed; exactly one command in the closing block needs one, and
    it has to say so in the same breath (#225). A keyless reader following that line lands on a
    failure the demo could have predicted for them."""
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
    # `requivo demo` reads a frozen payload bundled in the package (so it works from a wheel); examples/
    # holds the browsable copy at the repo root. Guard against silent drift between the two: every
    # bundled demo file must byte-match its counterpart under examples/event-checkin-reconciliation/.
    from requivo.paths import DEMO

    repo_root = DEMO.parents[3]  # assets/demo → assets → requivo → src → repo
    browsable = repo_root / "examples" / "event-checkin-reconciliation"
    bundled = sorted(DEMO.glob("*"))
    assert bundled, "demo payload is empty"
    for f in bundled:
        assert f.read_text(encoding="utf-8") == (browsable / f.name).read_text(encoding="utf-8"), f"demo payload drifted from examples/: {f.name}"


def test_the_browsable_examples_deterministic_half_matches_the_renderer():
    # #172: the test above compares the browsable example to its bundled twin -- two copies of each
    # other, so both can drift from what the renderer actually produces, together, and stay green.
    # This is the missing relationship: the readiness block and the draft banner are rendered by
    # code from the example's own model.json, not authored by the LLM, so they can be re-derived and
    # compared with no API call. The prose sections (challenges, risks, opportunities, next steps)
    # come from a `Brief` the provider writes and are NOT re-derived here -- regenerating those is a
    # spend decision, tracked separately (#172's "what is wanted" part 2).
    import io
    from contextlib import redirect_stdout

    from requivo.cli import _fenced_text
    from requivo.core.analysis import readiness_blockers
    from requivo.core.persistence import load_model
    from requivo.paths import DEMO
    from requivo.render.terminal import DRAFT_NOTE, render_readiness

    repo_root = DEMO.parents[3]
    # event-checkin only: its assessment is a *terminal* capture inside a ```text fence, which is
    # what `requivo demo` replays. leave-approval ships the markdown artifact `requivo brief` writes,
    # so its deterministic half is checked against `brief_markdown` instead, in the test below.
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
    # The sub-line under the banner is static and unconditioned on any LLM content -- it is
    # `DRAFT_NOTE` verbatim whenever draft, and absent otherwise -- so it is checked too, imported
    # from the renderer rather than duplicated as a literal here (found in review: the first version
    # of this test checked the banner text but not its sub-line, which could drift unnoticed).
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
    """The canonical example's decision brief is half a projection, and this is that half.

    `What is confirmed` and `Important assumptions` are not prose the provider was asked to write —
    `_stated()` reads each topic's value and provenance off the model, precisely so a restatement
    cannot drift from what it restates. Which means a committed pair can be checked with no API call,
    and a `model.json` swapped in without its brief goes red here.

    That could not be asserted before #223: the shipped assessment was a frozen capture from an
    earlier run, against an earlier layout, and its own README said so. Regenerating the example's
    whole chain from one model in one sitting is what makes the pair checkable at all.
    """
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
    # The assumptions section carries `summary.assumptions` after the projected topics; only the
    # `- **Label** —` lines are the projection, which is what `section` selects.
    assert section("Important assumptions") == _stated(out, Confidence.inferred)
    # The draft banner is the same rule `brief_markdown` applies, from the same model.
    draft = " — Draft: unresolved topics remain" if readiness_blockers(out) else ""
    assert brief.splitlines()[0] == f"# Decision Brief{draft}"
    assert f"**Objective:** {out.summary.objective}" in brief


def test_the_canonical_example_can_reproduce_the_change_impact_moment():
    """`impact` on the committed leave-approval model has something to say (#223).

    The shipped model carried no reasoning layer at all — six top-level keys, three of them empty —
    so `impact` on the example the README calls "the one to read first" listed stale artifacts and
    nothing else, while that README promised it would name the decisions resting on the integration
    topic. The differentiator was claimed on the example and reproducible only on the other one.
    """
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
    # Keystone: advise()'s reasoning is absorbed into the model and saved (backfill),
    # so downstream generators inherit it instead of it being regenerated and discarded.
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
    """`estimate` makes two provider calls and the second is read against the first's output, so both
    reason from one `SessionSnapshot` (#135).

    Two snapshots is invariant 12's own sentence — two reads, two instants — and a write landing
    between them estimates one model's stories against a different model. Nothing is written here, so
    unlike the case that invariant was written about there is no provenance to become a lie; what
    drifts is the answer itself, in a single terminal output that shows both halves and no revision.
    The snapshot is taken by the verb and the rendering stays between the two calls, so the stories
    still appear while the estimate is being reasoned.

    The call count is the must-fire half: "one snapshot" is also true of a verb that never ran.
    """
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
    # The terminal used to render the assessment and keep it: the Web and Claude Code saved a tracked
    # artifact, the CLI saved nothing. A generation now produces the same document wherever it was
    # asked for — same file, same provenance, same staleness tracking.
    with _model_in_out("clitest-brief-artifact") as p:
        _run_app(["brief", p.parent.name], client=FakeClient(json.dumps({"complexity": "low", "solution": "S"})))
        assert (p.parent / "artifacts" / "solution-assessment.md").exists()
        listed = ArtifactService().list(p.parent.name)["brief"]
        assert listed["stale"] is False and listed["revision"] >= 1


def test_pc_generators_record_which_prompt_reasoned(tmp_path):
    # A revision log that cannot say what produced it cannot reproduce it. Behaviour is tuned by
    # editing prompts and context cards, so the prompt hash is half the provenance.
    with _model_in_out("clitest-provenance") as p:
        _run_app(["brief", p.parent.name], client=FakeClient(json.dumps({"complexity": "low"})))
        rec = store.read_meta(p.parent.name).revisions[-1]
        assert rec.surface == "cli-brief" and rec.provider == "anthropic"
        assert rec.prompt_version and rec.prompt_version.startswith("sha256:")


# Minimal-but-legal artifact replies. The contracts require what makes each artifact *be* that
# artifact — a PRD states a problem, a scenario has a `when` and at least one `then`, an epic
# decomposes into at least one issue — so a stub reply has to carry those and nothing more.
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
    """#274: epic.json is the machine-consumed input an n8n flow acts on, and it used to carry no
    provenance -- an automation reading it had no signal that the session had moved on since it was
    written. `_cmd_epic` must thread the one `Generated.status.revision` snapshot the `epic.md` save
    used, not take a second read of the revision (invariant 12) -- so the assertion here is against
    the session's own recorded `artifact_status["epic"].revision`, not a hardcoded number, which is
    what would let a hardcoded stamp slip through."""
    slug = "clitest-epic-revision"
    with _model_in_out(slug) as p:
        # Bump past revision 1 first, so a test that only ever sees "1" cannot pass by accident --
        # the fixture above already lands the session at revision 1.
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
    _run_app(["discover", "clitest discover probe xyz", "--once"], client=FakeClient(_ENGINE_REPLY))
    folder = store.canonical_dir(slug)
    assert (folder / "model.json").exists()
    assert (folder / "request.md").exists()   # saved so `requivo answer` can resume
    assert store.read_meta(slug).current_revision == 1
    assert store.read_meta(slug).provider == "anthropic"


def test_pc_discover_prints_the_default_cards_before_the_paid_call():
    """#257: the default (no --context) reasons over every installed card, which CLAUDE.md's own
    "Known limit" note already names as the most expensive and most diluted path -- and nothing told
    a user which cards that was. The disclosure must be additive only: it must not change which
    cards actually get loaded, so `context_cards` on the saved session must still be `None` (== every
    card), the same as before this change -- a test that only checked the printed line could pass
    even if the disclosure secretly started narrowing the selection."""
    from requivo.services.sessions import SessionService

    output = _run_app(["discover", "clitest discover default cards", "--once"],
                       client=FakeClient(_ENGINE_REPLY))
    cards = available_cards()
    assert cards, "no bundled context cards found -- this test is not exercising anything"
    for name in cards:
        assert name in output, f"{name!r} (a real installed card) is not named in the pre-call output"
    slug = SessionService().list_sessions()[0].slug
    assert store.read_meta(slug).context_cards is None  # no behavior change: still every card


def test_pc_discover_names_the_fallback_weight_when_the_average_cannot_be_measured(monkeypatch):
    """Found in review: `average_card_byte_size() -> None` (an edge case -- reachable only when the
    card set's own average happens to be falsy, e.g. an empty install) has a dedicated fallback
    string in `_cmd_discover` ("measurable weight" instead of a byte figure), and nothing exercised
    it. Monkeypatches the CLI's own imported name, not the underlying function, so this pins the
    branch `_cmd_discover` actually takes rather than re-testing `average_card_byte_size` itself
    (that half is `tests/test_context.py`'s `..._is_none_on_an_empty_install`)."""
    import requivo.cli as cli_module

    monkeypatch.setattr(cli_module, "average_card_byte_size", lambda: None)
    output = _run_app(["discover", "clitest discover no measurable weight", "--once"],
                       client=FakeClient(_ENGINE_REPLY))
    assert "measurable weight" in output
    assert "bytes each" not in output


def test_pc_discover_with_explicit_context_does_not_also_print_the_all_cards_line():
    # The "no --context given" disclosure and the existing "Context cards: <selection>" line answer
    # the same question and must never both fire for one invocation -- that would say two different
    # things about what was loaded.
    output = _run_app(["discover", "clitest discover explicit cards", "--once",
                       "--context", "b2b-platform"],
                      client=FakeClient(_ENGINE_REPLY))
    assert "Context cards: b2b-platform" in output
    assert "document-management" not in output  # a card that was NOT selected must not be named


def test_discover_file_check_survives_a_real_length_request():
    # A real client request is a paragraph — longer than the OS filename limit. The file-vs-text
    # heuristic must treat that as text, not crash (Path.exists() raises OSError above the limit).
    # `is_file_argument` (moved to `deterministic/_shared.py` and shared with it by #301) is
    # `discover`'s own file-vs-text check -- exercised here through the name `cli.py` imports it as,
    # not re-implemented.
    long_request = "When a contract is signed we want everything to reconcile. " * 20
    assert is_file_argument(long_request) is False


def test_discover_file_check_rejects_blank_arg():
    # Path("") resolves to the current directory, which exists — so a naive .exists() check would
    # treat a blank request as a readable file and then blow up on read_text. Blank must read as text.
    assert is_file_argument("") is False
    assert is_file_argument("   \n\t ") is False


def test_discover_file_check_rejects_a_directory(tmp_path):
    # A directory `exists()` too. Accepting one means calling read_text() on it a line later, which
    # raises IsADirectoryError as a traceback instead of treating the argument as a request.
    assert is_file_argument(str(tmp_path)) is False
    f = tmp_path / "request.md"
    f.write_text("Build a leave approval system.")
    assert is_file_argument(str(f)) is True


def test_discover_from_a_file_slugifies_its_name(tmp_path, monkeypatch):
    # A filename is a suggestion for the slug, not a slug. Slugs name a directory in the session store
    # and are validated strictly, so passing the raw stem through turned an ordinary input file
    # ("Leave Approval v2.md") into an invalid_slug error.
    from requivo.services.sessions import SessionService

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    req = tmp_path / "Leave Approval v2.md"
    req.write_text("We would like a leave approval system.")
    _run_app(["discover", str(req), "--once"], client=FakeClient(_ENGINE_REPLY))
    assert [m.slug for m in SessionService().list_sessions()] == ["leave-approval-v2"]


def test_pc_discover_rejects_empty_request():
    # An empty/whitespace request should fail fast with a clear message, not crash or fire an
    # empty-content API call. The FakeClient would raise if reached — SystemExit means we never did.
    for blank in ("", "   "):
        with pytest.raises(SystemExit):
            _run_app(["discover", blank], client=FakeClient(_ENGINE_REPLY))


# ── `discover -` reads stdin, like every other document-taking verb (#360) ───────────────────
#
# `session init -`, `model apply <slug> -` and `artifact save --file -` all route through
# `deterministic/_shared.py`, which special-cases a bare `-` as "read the document from stdin".
# `discover` called `is_file_argument` directly instead, and `is_file_argument("-")` is False -- so
# the argument fell through to the treat-as-literal-text branch and the engine was asked, at full
# price, to discover a product from the two-character request `-`. Quiet, plausible-looking, and
# billed. Every case below is paired with its opposite in the same fixture, because "stdin was read"
# and "stdin was ignored" are only distinguishable when both are asserted.


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
    fake = FakeClient(_ENGINE_REPLY)
    _run_app(["discover", "-"], client=fake)
    assert "leave approval system" in _saved_request(_only_slug())
    # The bug is not only that the text was wrong -- it is that a paid call went out carrying it.
    assert "leave approval system" in json.dumps(fake.calls[0])


def test_a_one_character_request_that_is_not_a_dash_is_still_literal_text(monkeypatch):
    """The must-not-fire half. A fix that read stdin whenever stdin happened to be a pipe would
    hijack an ordinary short request -- and CI itself runs with a non-tty stdin, so that mistake
    would be invisible in exactly the environment that grades it."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("PIPED TEXT THAT MUST NOT BE READ"))
    _run_app(["discover", "x"], client=FakeClient(_ENGINE_REPLY))
    assert _saved_request(_only_slug()).strip() == "x"


def test_a_dash_with_a_terminal_on_stdin_is_refused_rather_than_discovered_on(monkeypatch):
    """`_read_stdin` refuses a terminal rather than hanging on input nobody meant to type. The
    assertion that matters is the second one: the refusal has to arrive *before* the provider call,
    or the fix has only changed which wrong request got paid for."""
    monkeypatch.setattr(sys, "stdin", _Tty(""))
    fake = FakeClient(_ENGINE_REPLY)
    with pytest.raises(SystemExit) as e:
        _run_app(["discover", "-"], client=fake)
    assert e.value.code == 1
    assert fake.calls == []


def test_an_empty_stdin_is_refused_rather_than_discovered_on(monkeypatch):
    """`printf "" | requivo discover -` is the same nothing-to-discover-from case the blank literal
    request above already refuses; it must reach the same refusal rather than the provider."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("   \n "))
    fake = FakeClient(_ENGINE_REPLY)
    with pytest.raises(SystemExit) as e:
        _run_app(["discover", "-"], client=fake)
    assert e.value.code == 2
    assert fake.calls == []


def test_a_dash_is_stdin_even_when_a_file_of_that_name_exists(monkeypatch, tmp_path):
    """The one input where the two halves of `_cmd_discover`'s branch could disagree, found in
    review of this diff. `read_source` reads stdin for `-` unconditionally, but `is_file_argument`
    answers the ordinary path question -- and a file literally named `-` in the working directory
    makes it True. Computed independently, the slug would then be suggested by a file whose content
    was never read. The control below is the same directory, one argument different."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "-").write_text("FILE CONTENT THAT MUST NOT BE READ", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "stdin", io.StringIO("We would like a leave approval system."))
    _run_app(["discover", "-"], client=FakeClient(_ENGINE_REPLY))
    slug = _only_slug()
    assert "leave approval system" in _saved_request(slug)
    assert "MUST NOT BE READ" not in _saved_request(slug)
    # The observable half, and the reason `slug != "-"` would not have been an assertion at all:
    # `slug_hint("-")` does not fail, it returns the generic fallback `discovery`. So the divergence
    # does not crash -- it quietly replaces a slug derived from the client's own words with a
    # placeholder, which is the shape that survives review. Measured: with the two halves computed
    # independently this session lands under `discovery`.
    assert slug != "discovery", (
        "the slug came from `slug_hint(Path('-').stem)`, i.e. from a file whose content was never "
        "read, instead of from the request that was actually discovered on")
    assert "leave" in slug


def test_a_path_that_merely_ends_in_a_dash_is_still_a_file(monkeypatch, tmp_path):
    """The must-fire half of the case above: `-` is stdin, and `./-` is a file. A fix that refused
    every argument containing a dash, or that stopped consulting `is_file_argument` at all, would
    satisfy the test above and break this one."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "-").write_text("We would like a leave approval system.", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "stdin", io.StringIO("STDIN THAT MUST NOT BE READ"))
    _run_app(["discover", "./-"], client=FakeClient(_ENGINE_REPLY))
    assert "leave approval system" in _saved_request(_only_slug())


def test_a_file_path_argument_still_behaves_exactly_as_before(monkeypatch, tmp_path):
    """The third arm of the same branch, kept honest: routing `-` through the shared reader must not
    disturb the file case, whose filename is also what suggests the slug."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("PIPED TEXT THAT MUST NOT BE READ"))
    req = tmp_path / "Leave Approval v3.md"
    req.write_text("We would like a leave approval system.", encoding="utf-8")
    _run_app(["discover", str(req)], client=FakeClient(_ENGINE_REPLY))
    assert _only_slug() == "leave-approval-v3"
    assert "leave approval system" in _saved_request("leave-approval-v3")


def test_pc_answer_refines_the_model():
    # A stateless discovery turn: answers + the current model → a refined model.
    with _model_in_out("clitest-answer") as p:
        turn2 = json.dumps({
            "model": full_slots(problem=slot(95, "explicit", "high")),
            "questions": [],
            # A discovery reply owes an objective — a session of slots with nothing naming what they
            # are for renders as a blank heading everywhere. The boundary check rejects it otherwise.
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

def test_a_wildcard_bind_is_not_auto_allowlisted_and_the_warning_names_the_env_var(monkeypatch, capsys):
    """#217: `--host 0.0.0.0` used to `os.environ.setdefault(REQUIVO_WEB_ALLOWED_HOSTS, "0.0.0.0")` --
    the literal string `"0.0.0.0"`, which no browser's `Host` header is ever going to equal, since a
    browser addresses the server by the reachable interface it actually connected to. So the flag
    appeared to bind wide and then 403'd every LAN request with no clue why. A wildcard bind address
    is meaningless as a `Host` value and must not be auto-allowlisted; the warning has to say what to
    do instead, by name, with a copy-pasteable example.

    Driven straight at `_cmd_web` (the same seam `test_the_missing_web_extra_keeps_its_published_error_code`
    uses) rather than through a real server: `uvicorn` is stubbed out so the function raises before it
    would ever bind a port, and the host-handling logic under test runs entirely before that import.
    """
    monkeypatch.delenv("REQUIVO_WEB_ALLOWED_HOSTS", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    args = argparse.Namespace(host="0.0.0.0", port=8000, no_open=True, reload=False)

    with pytest.raises(RequivoError):
        _cmd_web(args, None)

    assert "REQUIVO_WEB_ALLOWED_HOSTS" not in os.environ, (
        "the literal wildcard address must not be allowlisted -- no Host header will ever equal it")
    warning = capsys.readouterr().err
    assert "REQUIVO_WEB_ALLOWED_HOSTS" in warning, "the warning has to name the env var, not just hint at it"
    assert "0.0.0.0" in warning              # the copy-pasteable example names the flag that was passed

    # must-fire control, same fixture: a real (non-wildcard) LAN address IS a legitimate Host value, so
    # it keeps being auto-allowlisted exactly as before -- this is not a tightening of that path.
    monkeypatch.delenv("REQUIVO_WEB_ALLOWED_HOSTS", raising=False)
    args_lan = argparse.Namespace(host="192.168.1.50", port=8000, no_open=True, reload=False)
    with pytest.raises(RequivoError):
        _cmd_web(args_lan, None)
    assert os.environ["REQUIVO_WEB_ALLOWED_HOSTS"] == "192.168.1.50"
    capsys.readouterr()  # drain this leg's own warning before the next assertion reads stderr

    # and the loopback default is untouched: no warning, no env var written.
    monkeypatch.delenv("REQUIVO_WEB_ALLOWED_HOSTS", raising=False)
    args_default = argparse.Namespace(host="127.0.0.1", port=8000, no_open=True, reload=False)
    with pytest.raises(RequivoError):
        _cmd_web(args_default, None)
    assert "REQUIVO_WEB_ALLOWED_HOSTS" not in os.environ
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("spelling", ["::0", "0000:0000:0000:0000:0000:0000:0000:0000", "0:0:0:0:0:0:0:0"])
def test_an_equivalent_spelling_of_the_wildcard_address_is_caught_too(monkeypatch, capsys, spelling):
    """A string-literal check for `"::"` alone recognises exactly one spelling of the IPv6 unspecified
    address and none of its equivalents -- `::0`, the fully-expanded all-zeros form, and every other
    way to write "every interface" in IPv6 all mean the identical bind address (`ipaddress.ip_address`
    agrees they are all `is_unspecified`), and a socket layer binds them identically. `--host ::0`
    would otherwise fall into the "real address" branch, get auto-allowlisted verbatim, and reproduce
    #217's exact symptom -- every LAN request 403ing with no diagnostic pointing at the cause -- under
    a spelling the literal-string guard simply does not recognise (flagged by this diff's own review,
    #217's audit)."""
    monkeypatch.delenv("REQUIVO_WEB_ALLOWED_HOSTS", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    args = argparse.Namespace(host=spelling, port=8000, no_open=True, reload=False)

    with pytest.raises(RequivoError):
        _cmd_web(args, None)

    assert "REQUIVO_WEB_ALLOWED_HOSTS" not in os.environ, (
        f"{spelling!r} is the same address as '::' and must not be allowlisted verbatim either")
    warning = capsys.readouterr().err
    assert "REQUIVO_WEB_ALLOWED_HOSTS" in warning


def test_the_missing_web_extra_keeps_its_published_error_code(monkeypatch):
    """A missing `[web]` extra reports `provider_unavailable`, and that is a decision, not an oversight
    (#135).

    The type reads oddly at the call site: `EngineError` is the *provider transport* error, and an
    optional dependency has nothing to do with a provider. It stays anyway, because the code travels
    in the `--json` envelope and `docs/compatibility.md` promises that moving a condition from one
    code to another is a breaking change — from 1.0.0 that costs a major version, so this is a
    decision about a published payload rather than a rename.

    And the vocabulary already answers this question the same way one layer down: `new_client()`
    raises the same code for a missing `[anthropic]` extra. `provider_unavailable` is what this
    product says when an optional install is absent, so `_cmd_web` is consistent with its sibling
    rather than an outlier — which is the half that makes the comment at the call site an argument
    instead of an excuse.

    This test is what makes that decision checkable: swap the type for a new core error and it goes
    red under the name of the promise being broken.
    """
    # `None` in sys.modules is what makes `import uvicorn` raise without uninstalling anything —
    # the extra really is installed in the dev environment this runs in.
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    args = argparse.Namespace(host="127.0.0.1", port=8000, no_open=True, reload=False)

    with pytest.raises(RequivoError) as e:
        _cmd_web(args, None)

    assert e.value.code == "provider_unavailable"
    assert "requivo[web]" in str(e.value), "the remedy is the message's whole job"
    assert e.value.to_dict()["code"] == "provider_unavailable", "the envelope is what a caller reads"
