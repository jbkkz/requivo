"""`requivo docs` (#544): one verb over the seven generators, a menu when no type is given.

Mirrors `test_cli.py`'s harness (`tests/_fakes.py`) rather than reaching into that module directly
-- its own docstring says why a cross-file reach into a sibling test module is the wrong move.
"""
from __future__ import annotations

import pytest
from _fakes import FakeClient, _model_in_out, _run_app, slot
from _fakes import out as _built_model

from requivo.cli import DOC_TYPES, _doc_generation_order, _prompt_doc_selection, _resolve_doc_types, app
from requivo.core import persistence as store
from requivo.core.errors import RequivoError
from requivo.render.terminal import docs_menu_rows
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService
from requivo.web.example import seed_example


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


# ── pure helpers ─────────────────────────────────────────────────────────────────


def test_doc_generation_order_drops_the_duplicate_stories_write():
    """Picking `estimate` and `stories` together must write stories once -- `estimate`'s own
    `generate()` call already saves both against one revision (invariant 6). Canonical order
    otherwise. Pinned for #544."""
    assert _doc_generation_order(["estimate", "stories"]) == ["estimate"]
    assert _doc_generation_order(["release", "brief"]) == ["brief", "release"]
    assert _doc_generation_order(["stories"]) == ["stories"]


def test_resolve_doc_types_refuses_an_unknown_type_before_any_call():
    with pytest.raises(RequivoError):
        _resolve_doc_types(["prd", "nope"])
    assert _resolve_doc_types(["prd", "epic"]) == ["prd", "epic"]


def test_prompt_doc_selection_parses_numbers_names_and_all(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "2, stories")
    assert _prompt_doc_selection() == ["prd", "stories"]

    monkeypatch.setattr("builtins.input", lambda prompt="": "all")
    assert _prompt_doc_selection() == list(DOC_TYPES)

    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert _prompt_doc_selection() is None


def test_prompt_doc_selection_refuses_an_unknown_token_before_any_call(monkeypatch):
    """The interactive pick, like the command-line one above, refuses before any generator runs
    (invariant 3) rather than silently dropping the bad token."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "prd, nope")
    with pytest.raises(RequivoError):
        _prompt_doc_selection()


def test_docs_menu_rows_state_reads_artifact_status_not_revision_arithmetic():
    """A saved-at-rev-1 artifact on a rev-2 session whose change touched none of its consumed slots
    still reads *up to date* -- the state is `ArtifactStatus.stale`, never a comparison of revision
    numbers (invariant 1). `criteria` does not consume `problem`, which is what moves here."""
    slug = "clitest-docs-menu-rows"
    store.create_session(slug, "A request.")
    store.save_revision(slug, _built_model({"problem": slot(80, "explicit", "high")}))
    ArtifactService().save(slug, "criteria", "# Criteria\n", source_revision=1)
    store.save_revision(slug, _built_model({"problem": slot(90, "explicit", "high")}))
    meta = store.read_meta(slug)
    assert meta.current_revision == 2

    rows = {r.doc_type: r.state for r in docs_menu_rows(meta.artifact_status)}
    assert rows["criteria"].startswith("up to date (rev 1"), rows
    assert rows["brief"] == "not generated", rows


# ── end to end ───────────────────────────────────────────────────────────────────


def test_docs_prd_writes_the_same_file_and_provenance_as_the_prd_verb():
    reply = '{"title": "X", "problem": "P"}'
    with _model_in_out("clitest-docs-prd-a") as pa, _model_in_out("clitest-docs-prd-b") as pb:
        _run_app(["docs", pa.parent.name, "prd"], client=FakeClient(reply))
        _run_app(["prd", pb.parent.name], client=FakeClient(reply))
        a = (pa.parent / "artifacts" / "prd.md").read_text(encoding="utf-8")
        b = (pb.parent / "artifacts" / "prd.md").read_text(encoding="utf-8")
        assert a == b
        sa = store.read_meta(pa.parent.name).artifact_status["prd"]
        sb = store.read_meta(pb.parent.name).artifact_status["prd"]
        assert (sa.revision, sa.stale) == (sb.revision, sb.stale)


def test_docs_stories_and_estimate_together_write_stories_once():
    """Exactly two replies -- stories, then the estimate read against them. A `docs` that generated
    `stories` separately before `estimate` would exhaust the fake and raise, not merely double-write."""
    fake = FakeClient(
        '{"stories": [{"id": "S1", "title": "T"}]}',
        '{"items": [{"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]}',
    )
    with _model_in_out("clitest-docs-estimate") as p:
        _run_app(["docs", p.parent.name, "stories", "estimate"], client=fake)
        assert (p.parent / "artifacts" / "stories.md").exists()
        assert (p.parent / "artifacts" / "estimate.md").exists()
    assert len(fake.calls) == 2, "stories must be reasoned and saved exactly once"


def test_docs_all_flag_generates_every_document_skipping_the_menu():
    replies = [
        '{"complexity": "low"}',                                    # brief
        '{"title": "X", "problem": "P"}',                           # prd
        '{"stories": [{"id": "S1", "title": "T"}]}',                # estimate: stories
        '{"items": [{"story_id": "S1", "title": "T", "complexity": "S", '
        '"days_low": 1, "days_high": 2}]}',                         # estimate: estimate
        '{"title": "X", "features": [{"name": "F", "scenarios": [{"id": "SC-1", "title": "T", '
        '"when": "w", "then": ["t"]}]}]}',                          # criteria
        '{"title": "X", "issues": [{"id": "I-1", "title": "T"}]}',  # epic
        '{"title": "X", "summary": "S", "highlights": ["H"], "notes": ["N"]}',  # release
    ]
    with _model_in_out("clitest-docs-all") as p:
        _run_app(["docs", p.parent.name, "--all"], client=FakeClient(*replies))
        for name in ("solution-assessment.md", "prd.md", "stories.md", "estimate.md",
                     "acceptance-criteria.md", "epic.md", "release-notes.md"):
            assert (p.parent / "artifacts" / name).exists(), name


def test_docs_revision_zero_has_no_menu_and_points_at_run():
    store.create_session("clitest-docs-empty", "A request.")
    out_text = _run_app(["docs", "clitest-docs-empty"])
    assert "DOCUMENTS" not in out_text
    assert "requivo run clitest-docs-empty" in out_text


def test_docs_refuses_an_unknown_type_argument_before_any_call(capsys):
    with _model_in_out("clitest-docs-unknown-type") as p:
        fake = FakeClient()
        with pytest.raises(SystemExit) as exit_:
            app(["docs", p.parent.name, "bogus"], client=fake)
        assert exit_.value.code == 1
        assert "neither a document type nor a session" in capsys.readouterr().err
        assert fake.calls == []


def test_docs_all_refuses_a_token_that_names_neither_a_type_nor_a_session(capsys):
    """P1 from an independent review of #544: `docs <bad-slug> --all` fell through to the
    workspace's default session and generated every document there, silently -- `--all` discarded
    the unmatched token instead of refusing it (invariant 3). A second real session is present so a
    fix that merely picked the *right* default would still fail this."""
    fake = FakeClient()
    with _model_in_out("clitest-docs-all-other-session") as other:
        with pytest.raises(SystemExit) as exit_:
            app(["docs", "no-such-session", "--all"], client=fake)
        assert exit_.value.code == 1
        assert "neither a document type nor a session" in capsys.readouterr().err
        assert fake.calls == []
        artifacts_dir = other.parent / "artifacts"
        assert not artifacts_dir.exists() or not any(artifacts_dir.iterdir())


def test_docs_all_combined_with_an_explicit_type_is_refused(capsys):
    """#544: `--all` plus a named type is ambiguous rather than additive, refused before any call."""
    fake = FakeClient()
    with _model_in_out("clitest-docs-all-with-type") as p:
        with pytest.raises(SystemExit) as exit_:
            app(["docs", p.parent.name, "prd", "--all"], client=fake)
        assert exit_.value.code == 1
        err = capsys.readouterr().err
        assert "--all takes no types" in err and "prd" in err
        assert fake.calls == []


def test_docs_bundled_example_shows_brief_up_to_date_and_six_not_generated(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    slug = seed_example(SessionService())
    out_text = _run_app(["docs", slug])
    lines = out_text.splitlines()
    brief_line = next(ln for ln in lines if "Decision brief" in ln)
    assert "up to date" in brief_line, brief_line
    assert len([ln for ln in lines if "not generated" in ln]) == 6, out_text
