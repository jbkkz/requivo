"""A document body, or provider/git-authored prose, cannot write a line of its own render — #40, #62,
#70, #107.

Split out of `test_cli_untrusted_output.py` by #555, once that file outgrew one module: this half
covers every surface that renders a **document body** rather than a metadata field — a saved
artifact's content, a freshly generated document on its very first print, one document read back two
different ways, and the golden harness's own readout of a question or a commit subject.
`test_cli_untrusted_metadata.py` covers the `session.json`-field half (`doctor`, `session verify`,
`session show`, `artifact list`, `impact`, `run`'s candidate listing).

The shared harness is `tests/_cli_harness.py`.
"""
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

# The saved artifact *body* itself, one call further than `artifact list`'s two metadata fields
# (#430). `_cmd_artifact_show` was `print(content)` with no neutralization at all: a hostile client
# request that steers the model into an artifact carrying an embedded newline and a raw ESC sequence
# forges a line in Requivo's own voice at the operator's terminal, the same threat #213 closed on the
# primary render path. Not the class's last unguarded member, as this comment used to say -- see
# #449's own section below, which closes the same gap on `prd`/`criteria`/`epic`/`release`.
#
# Reusing `display_text` (#213's own neutralizer) here would be wrong rather than merely redundant:
# it escapes *every* control character, including a real newline, and an artifact body is a real
# multi-paragraph document whose newlines are its layout. So this needs `display_text`'s
# document-shaped sibling -- everything `display_text` neutralizes except a real newline and a real
# tab -- which is exactly what the issue asked for and exactly what the "ordinary document survives"
# test below checks for.
_FORGED_ARTIFACT = "# Real heading\nFORGED AT COLUMN ZERO\x1b[2Jtrailing prose"


def _save_artifact(slug: str, content: str, tmp_path: Path) -> None:
    """Get a session to revision 1 and save `content` as its `brief` artifact, through the CLI --
    the same route `test_session_show_leaves_an_ordinary_session_byte_for_byte` uses to reach
    `artifact save`, since `ArtifactService.save` itself refuses a revision-0 session."""
    proposal = tmp_path / "p.json"
    proposal.write_text(json.dumps(_full_model()), encoding="utf-8")
    _run(["model", "apply", slug, str(proposal)])
    doc = tmp_path / "doc.md"
    doc.write_text(content, encoding="utf-8")
    _run(["artifact", "save", slug, "--type", "brief", "--file", str(doc), "--revision", "1"])


def test_artifact_show_cannot_be_made_to_print_a_line_a_session_wrote(workspace, tmp_path):
    """must not fire: the raw ESC byte -- the thing that can move a cursor or clear a screen.
    must fire: the document is still shown -- escaped, not dropped -- and its own embedded newlines
    still read as real line breaks rather than one collapsed line of escapes."""
    _run(["session", "init", "Something.", "--slug", "as1"])
    _save_artifact("as1", _FORGED_ARTIFACT, tmp_path)

    out = _run(["artifact", "show", "as1", "--type", "brief"])

    assert "\x1b" not in out, out                              # must not fire: no raw ESC reaches stdout
    assert "\\x1b" in out, out                                 # must fire: neutralised, not dropped
    assert "# Real heading" in out.splitlines(), out           # the document's own line break held
    assert "FORGED AT COLUMN ZERO" in out, "neutralised must not mean dropped"


def test_artifact_show_leaves_an_ordinary_document_byte_for_byte(workspace, tmp_path):
    """The control. A document with no control character renders exactly as saved -- multi-line,
    with a real tab -- so the guard added above cannot be the thing that makes an honest artifact
    unreadable."""
    _run(["session", "init", "Something.", "--slug", "as2"])
    doc = "# Title\n\nSome prose with a tab\there, and a closing line.\n"
    _save_artifact("as2", doc, tmp_path)

    # `_cmd_artifact_show` is `print(content)`, which always appends its own trailing newline --
    # that is pre-existing behaviour this test pins rather than a property of the new guard.
    assert _run(["artifact", "show", "as2", "--type", "brief"]) == doc + "\n"


# ── the same class, one layer earlier: every ordinary generation, not only a later read-back (#449) ─
#
# `prd`/`criteria`/`epic`/`release` each used to `print(xxx_markdown(result.artifact))` with no
# neutralization at all -- the identical unguarded shape `_cmd_artifact_show` carried before #430,
# reachable on the *first* ordinary generation rather than only a later `artifact show`. Worse in
# reach: no saved artifact has to exist yet, so the hostile model reply reaches the operator's
# terminal on the one paid call that produced it.
#
# `_StubProvider`/`_Reply` below are the same fake-SDK-client shape `test_sessions.py`'s
# `_RacingClient`/`_Reply` already use -- a raw `.messages.create()` reply, so the real
# `AnthropicProvider` -> `_complete()` -> Pydantic-contract-validation path runs completely unmodified
# and only the network call is faked. That matters here specifically: a hand-built `PRD`/
# `AcceptanceCriteria`/`Epic`/`ReleaseNotes` object would only prove `display_document` works, not
# that the CLI's real generation path actually calls it.

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


# A real embedded newline *and* a raw ESC, matching the issue's own reproduction -- the newline
# proves the document's own layout survives, the ESC proves the guard actually ran.
_FORGED_TITLE = "Real Title\nFORGED AT COLUMN ZERO\x1b[2Jmore prose"

# One minimal, contract-valid payload per verb, each carrying `_FORGED_TITLE` in the one field every
# writer below renders as a heading line (`prd_markdown`, `criteria_markdown`, `epic_markdown`,
# `release_markdown` all open with `f"# {…}"`-shaped output off `.title`) -- so all four are exercised
# through the identical assertion shape.
_GENERATION_PAYLOADS = {
    "prd": {"title": _FORGED_TITLE, "problem": "A problem statement."},
    "criteria": {"title": _FORGED_TITLE,
                "features": [{"name": "F1", "scenarios": [
                    {"id": "S1", "title": "t", "when": "w", "then": ["result"]}]}]},
    "epic": {"title": _FORGED_TITLE, "issues": [{"id": "I1", "title": "Issue one"}]},
    "release": {"title": _FORGED_TITLE},
}


def _generate(verb: str, payload: dict, tmp_path: Path) -> tuple[str, Path]:
    """Take a fresh session to revision 1, run `verb` against a stub provider whose one reply is
    `payload`, and return (stdout, the artifact's own path on disk).

    The path is read back off the `_wrote()` line in stdout itself ("Wrote … → <path>") rather than
    re-derived from `artifact_path`, so this test cannot silently drift from whatever that chokepoint
    actually returns."""
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
    """must fire -- #449. The raw ESC byte must not reach stdout; the forgery must still be visible,
    escaped rather than dropped; and the document's own real embedded newline (the one separating
    "Real Title" from the forged line after it) must still read as a real line break, not be
    collapsed into one long line of escapes -- `display_document`'s whole reason for existing over
    `display_text` (#430)."""
    out, _ = _generate(verb, _GENERATION_PAYLOADS[verb], tmp_path)
    assert "\x1b[2J" not in out, out                     # must not fire: no raw ESC reaches stdout
    assert "\\x1b" in out, out                            # must fire: neutralised, not dropped
    # The document's own real newline held -- "Real Title" (each writer's own heading prefix in
    # front of it varies -- "#", "# Epic: ") sits on its own line, never sharing one with the forged
    # text that follows it in the same source string.
    assert any(ln.endswith("Real Title") for ln in out.splitlines()), out
    assert "FORGED AT COLUMN ZERO" in out, "neutralised must not mean dropped"


@pytest.mark.parametrize("verb", ["prd", "criteria", "epic", "release"])
def test_a_forged_generations_saved_file_stays_byte_identical(verb, workspace, tmp_path):
    """The other half of #430's own promise, carried to an earlier print site: only the terminal
    print changes. The file `_wrote` just reported writing still holds the artifact's raw, unescaped
    markdown -- the byte-identical-on-disk guarantee `core/integrity.py`'s hashing and the web
    download route rest on, restated here because #449 is the same guard reached one print site
    earlier, not a new promise about what gets written."""
    _, path = _generate(verb, _GENERATION_PAYLOADS[verb], tmp_path)
    saved = path.read_text(encoding="utf-8")
    assert "\x1b[2J" in saved, saved                      # must fire: the disk copy is untouched
    assert "\\x1b" not in saved, saved                    # must not fire: no escaping crept onto disk


@pytest.mark.parametrize("verb", ["prd", "criteria", "epic", "release"])
def test_an_ordinary_generation_prints_its_document_byte_for_byte(verb, workspace, tmp_path):
    """The control, matching `test_artifact_show_leaves_an_ordinary_document_byte_for_byte`: a
    generation with no control character in it renders exactly as generated, so #449's guard is not
    what quietly starts escaping ordinary Requivo output."""
    payload = dict(_GENERATION_PAYLOADS[verb])
    payload["title"] = "An entirely ordinary title"
    out, _ = _generate(verb, payload, tmp_path)
    assert "An entirely ordinary title" in out, out
    assert "\\x" not in out, out


# ── one document, two print paths, one rendering (#460) ──────────────────────────────────────────
#
# #449 put `display_document` on four generation print sites, and inherited a premise that had only
# ever been true of the fifth. The guard held CR inside its escaped range because, in
# `selectors.py`'s own words, "the read path normalises a raw CR to LF (universal newlines), so a CR
# reaching this function is not layout". That is a fact about `_cmd_artifact_show`, which opens a
# file. The four new callers hand the generator's string straight in with no file read between, so a
# provider reply carrying CRLF -- and models do emit it in markdown -- printed a visible `\r` at the
# end of every line under `requivo prd`, while `requivo artifact show --type prd` printed the
# *identical saved file* clean.
#
# Nothing was corrupted: the disk copy was never in question and the direction was the safe one. What
# it cost is a reader concluding the artifact is broken, on the verb that just produced it.
#
# The fix folds a CRLF pair to LF inside `display_document`, so the premise becomes true of all five
# callers instead of being narrowed to one. A *lone* CR stays escaped, which is the half that
# matters: `\r\n` moves to the next line exactly as `\n` does and is layout, while a bare `\r`
# returns the cursor to column zero and is the cursor-control risk #430 exists to close. The test
# below asserts both halves, and asserts the two paths against *each other* rather than against a
# spelling -- a comparison that cannot pass by agreeing on the wrong answer twice, since only one of
# the two paths was ever wrong.


@pytest.mark.parametrize("verb", ["prd", "criteria", "epic", "release"])
def test_the_same_document_renders_identically_through_generation_and_read_back(
        verb, workspace, tmp_path):
    """must fire -- #460. The generation verb and `artifact show` are two views of one file, so the
    reader must not be able to tell which one they ran from the text on their screen."""
    payload = dict(_GENERATION_PAYLOADS[verb])
    payload["title"] = "Windows Title\r\nA second line."
    generated, path = _generate(verb, payload, tmp_path)

    # The generation print is everything above `_wrote`'s own receipt line, which `artifact show`
    # does not emit. `_wrote` opens with its own newline, so splitting on "\nWrote " leaves exactly
    # the `print(display_document(...))` output and the two sides are compared byte for byte.
    document = generated.split("\nWrote ")[0]
    slug = path.parent.parent.name   # `artifact_path` is `<slug>/artifacts/<filename>`
    read_back = _run(["artifact", "show", slug, "--type", verb])

    # Both paths must have stopped escaping the CRLF -- this is #460's own claim, and it holds on
    # every platform because it is about the print seam and nothing else.
    assert "\\r" not in document, document          # must not fire: a CRLF is layout, not a forgery
    assert "\\r" not in read_back, read_back
    assert "A second line." in document, document   # must fire: neutralised never means dropped

    # That the two *agree* is only assertable where writing a file does not rewrite what is in it.
    # `_atomic_write` writes in text mode with the default newline translation, so on Windows the
    # content's own `\r\n` reaches disk as `\r\r\n` and reads back as two line breaks -- the file the
    # two paths are views of has itself changed between the write and the read, which is #464 and
    # not this guard's subject. UNTESTED where `os.linesep` is not `\n`: that the generation print
    # and the read-back print are byte-identical. Every other platform asserts it.
    if os.linesep == "\n":
        assert document == read_back, (document, read_back)
    # The saved file is untouched, as it is for every other guard in this module -- the CRLF the
    # provider sent is still on disk, and it is the *rendering* of it that the two paths agree on.
    # `.open(newline="")`, not `read_text(newline=...)`: that keyword is 3.13+ and this project
    # supports 3.9. Universal newlines is exactly what has to be off here -- the assertion is about
    # the bytes on disk, and the default translation would rewrite the CRLF being asserted about.
    with path.open(encoding="utf-8", newline="") as fh:
        assert "\r\n" in fh.read(), "the disk copy must keep its CRLF"


def test_a_lone_cr_is_still_escaped_because_it_is_not_a_line_ending(workspace, tmp_path):
    """must fire -- #460's other half. Folding CRLF must not be read as "CR is fine now": a bare CR
    returns the cursor to column zero, which is exactly the forgery `display_document` exists to
    stop, and is the one thing the fix must not have widened."""
    payload = dict(_GENERATION_PAYLOADS["prd"])
    payload["title"] = "Real Title\rFORGED AT COLUMN ZERO"
    out, _ = _generate("prd", payload, tmp_path)

    assert "\r" not in out.replace("\n", ""), out   # must not fire: no raw CR reaches stdout
    assert "\\r" in out, out                         # must fire: neutralised, not dropped
    assert "FORGED AT COLUMN ZERO" in out, out


# ── the same class, one layer out: the golden harness (#137) ─────────────────────────────────────
#
# `scripts/golden_diff.py --questions` renders a golden baseline, and every string it prints there —
# the question text, the challenge headline, the premise — is **provider-written prose read back off
# disk**. That is the same untrusted-value-renders-a-line class as every verb above, arriving through
# a file the maintainer captured rather than one a stranger wrote, which is a difference in
# likelihood and not in kind: invariant 14's rule is that a persisted field is untrusted input every
# time it is read back, whoever wrote it.
#
# It lives in this file rather than beside the harness's own tests because the file's subject is the
# class, not the layer — its docstring says the sweep across verbs *is* the finding, and a script the
# maintainer runs by hand is the one caller that was outside every previous sweep. `scripts/` is not
# shipped in the wheel; a forged line at column 0 of a regression readout is still a forged line.

def _golden_capture(question: str) -> str:
    """A one-run interactive baseline whose single question carries `question`."""
    model = {"model": {}, "questions": [{"q": question, "slot": "problem", "why": "w"}],
             "summary": {"objective": "o", "scope": "", "assumptions": [], "blind_spot": ""},
             "decisions": [], "challenges": [], "opportunities": []}
    return json.dumps({"request": "r", "answers": {"problem": ["p"]},
                       "turns": [[{"index": 1, "answered": [], "model": model}]]})


@pytest.fixture
def golden_readout(tmp_path, monkeypatch):
    """`questions_one` over a forged baseline, with git and the fixture root stubbed out.

    `baseline_commits_since` shells out to real git (#405 added `questions_one`'s own freshness
    line); stubbed to `current` by default so the two tests below stay about the question text, not
    this checkout's own git history. `freshness` lets a test reach the other states -- including a
    forged commit subject, the other half of this file's own class, exercised further down."""
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
    """The control. `display_token` returns a safe line unchanged, so the readout a maintainer opens
    to judge a prompt change is not quoted or escaped — a guard that made ordinary prose unreadable
    would be removed, and the class would come back with it."""
    prose = "When the budget runs out — is it rejected outright, or escalated?"
    assert any(ln.strip() == f"[problem] {prose}" for ln in golden_readout(prose)), prose


def test_a_forged_baseline_freshness_commit_subject_cannot_write_a_line_of_the_readout(golden_readout):
    """must fire: a commit subject is contributor-written text (#405's `_show_freshness`, the other
    new sink this class covers) -- a newline inside one must not render as a second, unescaped line
    of this readout."""
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


