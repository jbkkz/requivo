"""A document body, or provider/git-authored prose, cannot write a line of its own render — #40, #62, #70,
#107."""
from __future__ import annotations

import io
import json
import os
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _cli_harness import _full_model, _run

from requivo.cli import app

# The saved artifact *body* itself, one call further than `artifact list`'s two metadata fields (#430).
#
# Reusing `display_text` (#213's own neutralizer) here would be wrong rather than merely redundant.
_FORGED_ARTIFACT = "# Real heading\nFORGED AT COLUMN ZERO\x1b[2Jtrailing prose"


def _save_artifact(slug: str, content: str, tmp_path: Path) -> None:
    """Get a session to revision 1 and save `content` as its `brief` artifact, through the CLI."""
    proposal = tmp_path / "p.json"
    proposal.write_text(json.dumps(_full_model()), encoding="utf-8")
    _run(["model", "apply", slug, str(proposal)])
    doc = tmp_path / "doc.md"
    doc.write_text(content, encoding="utf-8")
    _run(["artifact", "save", slug, "--type", "brief", "--file", str(doc), "--revision", "1"])


def test_artifact_show_cannot_be_made_to_print_a_line_a_session_wrote(workspace, tmp_path):
    """must not fire: the raw ESC byte -- the thing that can move a cursor or clear a screen. must fire: the
    document is still shown -- escaped, not dropped."""
    _run(["session", "init", "Something.", "--slug", "as1"])
    _save_artifact("as1", _FORGED_ARTIFACT, tmp_path)

    out = _run(["artifact", "show", "as1", "--type", "brief"])

    assert "\x1b" not in out, out                              # must not fire: no raw ESC reaches stdout
    assert "\\x1b" in out, out                                 # must fire: neutralised, not dropped
    assert "# Real heading" in out.splitlines(), out           # the document's own line break held
    assert "FORGED AT COLUMN ZERO" in out, "neutralised must not mean dropped"


def test_artifact_show_leaves_an_ordinary_document_byte_for_byte(workspace, tmp_path):
    """The control. A document with no control character renders exactly as saved."""
    _run(["session", "init", "Something.", "--slug", "as2"])
    doc = "# Title\n\nSome prose with a tab\there, and a closing line.\n"
    _save_artifact("as2", doc, tmp_path)

    # `_cmd_artifact_show` is `print(content)`, which always appends its own trailing newline.
    assert _run(["artifact", "show", "as2", "--type", "brief"]) == doc + "\n"


# ── the same class, one layer earlier: every ordinary generation, not only a later read-back (#449) ─
#
# `prd`/`criteria`/`epic`/`release` each used to `print(xxx_markdown(result.artifact))` with no neutralization at all -- the identical unguarded shape `_cmd_artifact_show` carried before #430, reachable on the *first* ordinary generation rather than only a later `artifact show`.
#
# `_StubProvider`/`_Reply` below are the same fake-SDK-client shape `test_sessions.py`'s `_RacingClient`/`_Reply` already use -- a raw `.messages.create()` reply, so the real `AnthropicProvider` -> `_complete()` -> Pydantic-contract-validation path runs completely unmodified and only the network call is faked.

class _Reply:
    def __init__(self, text: str):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"


class _StubProvider:
    """A raw Anthropic-SDK-shaped client whose one reply is always `json_text`."""

    def __init__(self, json_text: str):
        self.messages = self
        self._json_text = json_text

    def create(self, **kwargs):
        return _Reply(self._json_text)


# A real embedded newline *and* a raw ESC, matching the issue's own reproduction.
_FORGED_TITLE = "Real Title\nFORGED AT COLUMN ZERO\x1b[2Jmore prose"

# One minimal, contract-valid payload per verb, each carrying `_FORGED_TITLE` in the one field every writer below renders as a heading line (`prd_markdown`, `criteria_markdown`, `epic_markdown`, `release_markdown` all open with `f"# {…}"`-shaped output off `.title`) -- so all four are exercised through the identical assertion shape.
_GENERATION_PAYLOADS = {
    "prd": {"title": _FORGED_TITLE, "problem": "A problem statement."},
    "criteria": {"title": _FORGED_TITLE,
                "features": [{"name": "F1", "scenarios": [
                    {"id": "S1", "title": "t", "when": "w", "then": ["result"]}]}]},
    "epic": {"title": _FORGED_TITLE, "issues": [{"id": "I1", "title": "Issue one"}]},
    "release": {"title": _FORGED_TITLE},
}


def _generate(verb: str, payload: dict, tmp_path: Path) -> tuple[str, Path]:
    """Take a fresh session to revision 1, run `verb` against a stub provider whose one reply is `payload`,
    and return (stdout, the artifact's own path on disk)."""
    slug = f"gen-{verb}-{abs(hash(json.dumps(payload, sort_keys=True))) % 10_000}"
    _run(["session", "init", "Something.", "--slug", slug])
    proposal = tmp_path / f"{slug}-p.json"
    proposal.write_text(json.dumps(_full_model()), encoding="utf-8")
    _run(["model", "apply", slug, str(proposal)])

    client = _StubProvider(json.dumps(payload))
    buf = io.StringIO()
    with redirect_stdout(buf):
        app([verb, slug], client=client)
    out = buf.getvalue()

    m = re.search(r"Wrote .+ (\S+)$", out, re.MULTILINE)
    assert m, f"no 'Wrote …' line in {verb} output: {out!r}"
    return out, Path(m.group(1))


@pytest.mark.parametrize("verb", ["prd", "criteria", "epic", "release"])
def test_a_forged_generation_cannot_write_a_line_of_its_own_terminal_print(verb, workspace, tmp_path):
    """must fire -- #449. The raw ESC byte must not reach stdout."""
    out, _ = _generate(verb, _GENERATION_PAYLOADS[verb], tmp_path)
    assert "\x1b[2J" not in out, out                     # must not fire: no raw ESC reaches stdout
    assert "\\x1b" in out, out                            # must fire: neutralised, not dropped
    # The document's own real newline held -- "Real Title" (each writer's own heading prefix in front of it varies -- "#", "# Epic: ") sits on its own line, never sharing one with the forged text that follows it in the same source string.
    assert any(ln.endswith("Real Title") for ln in out.splitlines()), out
    assert "FORGED AT COLUMN ZERO" in out, "neutralised must not mean dropped"


@pytest.mark.parametrize("verb", ["prd", "criteria", "epic", "release"])
def test_a_forged_generations_saved_file_stays_byte_identical(verb, workspace, tmp_path):
    """The other half of #430's own promise, carried to an earlier print site."""
    _, path = _generate(verb, _GENERATION_PAYLOADS[verb], tmp_path)
    saved = path.read_text(encoding="utf-8")
    assert "\x1b[2J" in saved, saved                      # must fire: the disk copy is untouched
    assert "\\x1b" not in saved, saved                    # must not fire: no escaping crept onto disk


@pytest.mark.parametrize("verb", ["prd", "criteria", "epic", "release"])
def test_an_ordinary_generation_prints_its_document_byte_for_byte(verb, workspace, tmp_path):
    """The control, matching `test_artifact_show_leaves_an_ordinary_document_byte_for_byte` (#449)."""
    payload = dict(_GENERATION_PAYLOADS[verb])
    payload["title"] = "An entirely ordinary title"
    out, _ = _generate(verb, payload, tmp_path)
    assert "An entirely ordinary title" in out, out
    assert "\\x" not in out, out


# ── one document, two print paths, one rendering (#460) ──────────────────────────────────────────
#
# #449 put `display_document` on four generation print sites, and inherited a premise that had only ever been true of the fifth.
#
# Nothing was corrupted: the disk copy was never in question and the direction was the safe one.
#
# The fix folds a CRLF pair to LF inside `display_document` (#430).


@pytest.mark.parametrize("verb", ["prd", "criteria", "epic", "release"])
def test_the_same_document_renders_identically_through_generation_and_read_back(
        verb, workspace, tmp_path):
    """must fire -- #460. The generation verb and `artifact show` are two views of one file."""
    payload = dict(_GENERATION_PAYLOADS[verb])
    payload["title"] = "Windows Title\r\nA second line."
    generated, path = _generate(verb, payload, tmp_path)

    # The generation print is everything above `_wrote`'s own receipt line.
    document = generated.split("\nWrote ")[0]
    slug = path.parent.parent.name   # `artifact_path` is `<slug>/artifacts/<filename>`
    read_back = _run(["artifact", "show", slug, "--type", verb])

    # Both paths must have stopped escaping the CRLF (#460).
    assert "\\r" not in document, document          # must not fire: a CRLF is layout, not a forgery
    assert "\\r" not in read_back, read_back
    assert "A second line." in document, document   # must fire: neutralised never means dropped

    # That the two *agree* is only assertable where writing a file does not rewrite what is in it (#464).
    if os.linesep == "\n":
        assert document == read_back, (document, read_back)
    # The saved file is untouched, as it is for every other guard in this module.
    with path.open(encoding="utf-8", newline="") as fh:
        assert "\r\n" in fh.read(), "the disk copy must keep its CRLF"


def test_a_lone_cr_is_still_escaped_because_it_is_not_a_line_ending(workspace, tmp_path):
    """must fire -- #460's other half. Folding CRLF must not be read as "CR is fine now"."""
    payload = dict(_GENERATION_PAYLOADS["prd"])
    payload["title"] = "Real Title\rFORGED AT COLUMN ZERO"
    out, _ = _generate("prd", payload, tmp_path)

    assert "\r" not in out.replace("\n", ""), out   # must not fire: no raw CR reaches stdout
    assert "\\r" in out, out                         # must fire: neutralised, not dropped
    assert "FORGED AT COLUMN ZERO" in out, out


# ── the same class, one layer out: the golden harness (#137) ─────────────────────────────────────
#
# `scripts/golden_diff.py --questions` renders a golden baseline, and every string it prints there.
#
# It lives in this file rather than beside the harness's own tests because the file's subject is the class.

def _golden_capture(question: str) -> str:
    """A one-run interactive baseline whose single question carries `question`."""
    model = {"model": {}, "questions": [{"q": question, "slot": "problem", "why": "w"}],
             "summary": {"objective": "o", "scope": "", "assumptions": [], "blind_spot": ""},
             "decisions": [], "challenges": [], "opportunities": []}
    return json.dumps({"request": "r", "answers": {"problem": ["p"]},
                       "turns": [[{"index": 1, "answered": [], "model": model}]]})


@pytest.fixture
def golden_readout(tmp_path, monkeypatch):
    """`questions_one` over a forged baseline, with git and the fixture root stubbed out (#405)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import golden_diff as gd
    import golden_lib as gl

    def run(question: str, freshness: dict | None = None) -> list[str]:
        monkeypatch.setattr(gl, "GOLDEN", tmp_path)
        payload = _golden_capture(question)
        (tmp_path / "forged.runs.json").write_text(payload, encoding="utf-8")
        monkeypatch.setattr(gd, "_head_version", lambda _rel: payload)
        monkeypatch.setattr(gd, "baseline_commits_since",
                            lambda _rel: freshness or {"state": "current",
                                                        "captured_at": "2026-01-01T00:00:00+00:00"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            gd.questions_one("forged")
        return buf.getvalue().splitlines()

    return run


def test_a_forged_question_cannot_write_a_line_of_the_golden_readout(golden_readout):
    """must fire: a newline inside provider prose is escaped rather than printed as a second line."""
    lines = golden_readout("benign question?\n[permissions] FORGED, at column 0")
    assert not any(ln.lstrip().startswith("[permissions] FORGED") for ln in lines), lines
    assert any("FORGED" in ln and "\\n" in ln for ln in lines), lines


def test_an_ordinary_question_is_rendered_byte_for_byte(golden_readout):
    """The control. `display_token` returns a safe line unchanged, so the readout a maintainer opens to judge
    a prompt change is not quoted or escaped."""
    prose = "When the budget runs out — is it rejected outright, or escalated?"
    assert any(ln.strip() == f"[problem] {prose}" for ln in golden_readout(prose)), prose


def test_a_forged_baseline_freshness_commit_subject_cannot_write_a_line_of_the_readout(golden_readout):
    """must fire: a commit subject is contributor-written text (#405's `_show_freshness`, the other new sink
    this class covers)."""
    stale = {"state": "stale", "captured_at": "2026-08-01T00:00:00+00:00",
             "commits": [{"sha": "abc123def", "date": "2026-08-15",
                          "subject": "benign subject\n[permissions] FORGED, at column 0"}]}
    lines = golden_readout("q?", freshness=stale)
    assert not any(ln.lstrip().startswith("[permissions] FORGED") for ln in lines), lines
    assert any("FORGED" in ln and "\\n" in ln for ln in lines), lines


def test_an_ordinary_commit_subject_is_rendered_byte_for_byte(golden_readout):
    """The control, matching the question-text one above: an ordinary subject renders unchanged."""
    subject = "edit engine.md for the leave-approval card"
    stale = {"state": "stale", "captured_at": "2026-08-01T00:00:00+00:00",
             "commits": [{"sha": "abc123def", "date": "2026-08-15", "subject": subject}]}
    lines = golden_readout("q?", freshness=stale)
    assert any(subject in ln for ln in lines), lines


