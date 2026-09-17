"""Text read off disk, off a provider or off git cannot write a line of a verb's output (#40, #62, #70, #107)."""
from __future__ import annotations

import io
import json
import os
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _cli_harness import _SESSIONS_ROW
from _fakes import FakeClient, forge_meta, run_cli, run_cli_exit, run_cli_json, seed_session

from requivo.core import persistence as store

pytestmark = pytest.mark.usefixtures("workspace")

VERBS = ["prd", "criteria", "epic", "release"]


def _save_artifact(slug: str, artifact_type: str, content: str, tmp_path: Path) -> None:
    """Save `content` as `slug`'s artifact through the CLI, against revision 1."""
    doc = tmp_path / f"{slug}-{artifact_type}.md"
    doc.write_text(content, encoding="utf-8")
    run_cli(["artifact", "save", slug, "--type", artifact_type, "--file", str(doc), "--revision", "1"])


# ── the saved artifact body, on `artifact show` (#430) ────────────────────────────────

_FORGED_ARTIFACT = "# Real heading\nFORGED AT COLUMN ZERO\x1b[2Jtrailing prose"


def test_artifact_show_cannot_be_made_to_print_a_line_a_session_wrote(tmp_path):
    """must not fire: a raw ESC; must fire: the document is still shown, escaped (#430)."""
    _save_artifact(seed_session("as1", "Something."), "brief", _FORGED_ARTIFACT, tmp_path)
    out = run_cli(["artifact", "show", "as1", "--type", "brief"])
    assert "\x1b" not in out and "\\x1b" in out, out
    assert "# Real heading" in out.splitlines(), out
    assert "FORGED AT COLUMN ZERO" in out, "neutralised must not mean dropped"


def test_artifact_show_leaves_an_ordinary_document_byte_for_byte(tmp_path):
    """The control: `_cmd_artifact_show` is `print(content)`, which appends its own newline."""
    doc = "# Title\n\nSome prose with a tab\there, and a closing line.\n"
    _save_artifact(seed_session("as2", "Something."), "brief", doc, tmp_path)
    assert run_cli(["artifact", "show", "as2", "--type", "brief"]) == doc + "\n"


# ── the same class one layer earlier: every ordinary generation's own print (#449) ───────

_FORGED_TITLE = "Real Title\nFORGED AT COLUMN ZERO\x1b[2Jmore prose"
# One contract-valid payload per verb, `_FORGED_TITLE` in the field every writer renders as a heading line.
_GENERATION_PAYLOADS = {
    "prd": {"title": _FORGED_TITLE, "problem": "A problem statement."},
    "criteria": {"title": _FORGED_TITLE, "features": [{"name": "F1", "scenarios": [
        {"id": "S1", "title": "t", "when": "w", "then": ["result"]}]}]},
    "epic": {"title": _FORGED_TITLE, "issues": [{"id": "I1", "title": "Issue one"}]},
    "release": {"title": _FORGED_TITLE},
}


def _generate(verb: str, title: str | None = None) -> tuple[str, Path]:
    """Run `verb` on a fresh revision-1 session against a one-reply client; (stdout, the artifact's path)."""
    payload = dict(_GENERATION_PAYLOADS[verb], **({"title": title} if title is not None else {}))
    slug = seed_session(f"gen-{verb}-{abs(hash(title)) % 10_000}", "Something.")
    out = run_cli([verb, slug], client=FakeClient(json.dumps(payload)))
    m = re.search(r"Wrote .+ (\S+)$", out, re.MULTILINE)
    assert m, f"no 'Wrote …' line in {verb} output: {out!r}"
    return out, Path(m.group(1))


@pytest.mark.parametrize("verb", VERBS)
def test_a_forged_generation_cannot_write_a_line_of_its_own_terminal_print(verb):
    """#449: no raw ESC reaches stdout, the real newline holds, and nothing is dropped."""
    out, path = _generate(verb)
    assert "\x1b[2J" not in out and "\\x1b" in out, out
    assert any(ln.endswith("Real Title") for ln in out.splitlines()), out
    assert "FORGED AT COLUMN ZERO" in out, "neutralised must not mean dropped"
    saved = path.read_text(encoding="utf-8")   # the disk copy is untouched (#430's other half)
    assert "\x1b[2J" in saved and "\\x1b" not in saved, saved


@pytest.mark.parametrize("verb", VERBS)
def test_an_ordinary_generation_prints_its_document_byte_for_byte(verb):
    out, _ = _generate(verb, "An entirely ordinary title")
    assert "An entirely ordinary title" in out and "\\x" not in out, out


@pytest.mark.parametrize("verb", VERBS)
def test_the_same_document_renders_identically_through_generation_and_read_back(verb):
    """#460: a CRLF is layout, not a forgery, on both print paths of one file."""
    generated, path = _generate(verb, "Windows Title\r\nA second line.")
    document = generated.split("\nWrote ")[0]
    read_back = run_cli(["artifact", "show", path.parent.parent.name, "--type", verb])
    assert "\\r" not in document and "\\r" not in read_back, (document, read_back)
    assert "A second line." in document, document
    if os.linesep == "\n":   # agreement is only assertable where writing a file does not rewrite it (#464)
        assert document == read_back, (document, read_back)
    with path.open(encoding="utf-8", newline="") as fh:
        assert "\r\n" in fh.read(), "the disk copy must keep its CRLF"


def test_a_lone_cr_is_still_escaped_because_it_is_not_a_line_ending():
    """#460's other half."""
    out, _ = _generate("prd", "Real Title\rFORGED AT COLUMN ZERO")
    assert "\r" not in out.replace("\n", "") and "\\r" in out, out
    assert "FORGED AT COLUMN ZERO" in out, out


# ── the same class one layer out: the golden readout (#137, #405) ─────────────────────


@pytest.fixture
def golden_readout(tmp_path, monkeypatch):
    """`questions_one` over a forged one-run baseline, git and the fixture root stubbed out."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import golden_diff as gd
    import golden_lib as gl

    def run(question: str, freshness: dict | None = None) -> list[str]:
        model = {"model": {}, "questions": [{"q": question, "slot": "problem", "why": "w"}],
                 "summary": {"objective": "o", "scope": "", "assumptions": [], "blind_spot": ""},
                 "decisions": [], "challenges": [], "opportunities": []}
        payload = json.dumps({"request": "r", "answers": {"problem": ["p"]},
                              "turns": [[{"index": 1, "answered": [], "model": model}]]})
        monkeypatch.setattr(gl, "GOLDEN", tmp_path)
        (tmp_path / "forged.runs.json").write_text(payload, encoding="utf-8")
        monkeypatch.setattr(gd, "_head_version", lambda _rel: payload)
        monkeypatch.setattr(gd, "baseline_commits_since", lambda _rel: freshness or {
            "state": "current", "captured_at": "2026-01-01T00:00:00+00:00"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            gd.questions_one("forged")
        return buf.getvalue().splitlines()

    return run


def _stale(subject: str) -> dict:
    return {"state": "stale", "captured_at": "2026-08-01T00:00:00+00:00",
            "commits": [{"sha": "abc123def", "date": "2026-08-15", "subject": subject}]}


FORGED_PROSE = "benign text\n[permissions] FORGED, at column 0"


def test_a_forged_question_cannot_write_a_line_of_the_golden_readout(golden_readout):
    lines = golden_readout(FORGED_PROSE)
    assert not any(ln.lstrip().startswith("[permissions] FORGED") for ln in lines), lines
    assert any("FORGED" in ln and "\\n" in ln for ln in lines), lines


def test_an_ordinary_question_is_rendered_byte_for_byte(golden_readout):
    prose = "When the budget runs out — is it rejected outright, or escalated?"
    assert any(ln.strip() == f"[problem] {prose}" for ln in golden_readout(prose)), prose


def test_a_forged_baseline_freshness_commit_subject_cannot_write_a_line_of_the_readout(golden_readout):
    """A commit subject is contributor-written text (#405's `_show_freshness`)."""
    lines = golden_readout("q?", freshness=_stale(FORGED_PROSE))
    assert not any(ln.lstrip().startswith("[permissions] FORGED") for ln in lines), lines
    assert any("FORGED" in ln and "\\n" in ln for ln in lines), lines


def test_an_ordinary_commit_subject_is_rendered_byte_for_byte(golden_readout):
    subject = "edit engine.md for the leave-approval card"
    assert any(subject in ln for ln in golden_readout("q?", freshness=_stale(subject))), subject


# ── `session.json` fields on doctor / verify / show / list (#40, #70) ─────────────────

_FORGED_CARD = "ok-card\nAll clear, nothing to see.\n  ✅ sessions        0 in this workspace"


@pytest.fixture
def forged_workspace(tmp_path, monkeypatch):
    """Two sessions on one card; `forged` then claims a hostile card name and the real card is gone."""
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "gone-card.md").write_text("# Gone card\n\nSome product context.\n", encoding="utf-8")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(cards))
    for slug in ("honest", "forged"):
        run_cli(["session", "init", f"Something {slug}.", "--slug", slug, "--context", "gone-card", "--json"])
    forge_meta("forged", {"context_cards": [_FORGED_CARD]})
    (cards / "gone-card.md").unlink()
    return cards


def test_doctor_cannot_be_made_to_print_a_row_a_session_wrote(forged_workspace):
    """#40: `doctor` answers *is anything wrong*, and a session it reports on could make it say no."""
    lines = run_cli(["doctor"]).splitlines()
    rows = [ln for ln in lines if _SESSIONS_ROW.match(ln)]
    assert len(rows) == 1 and rows[0].startswith("  ❌ sessions") and "2 in this workspace" in rows[0], rows
    honest = [ln for ln in lines if ln.startswith("     └─ honest: ")]
    assert len(honest) == 1 and "gone-card" in honest[0], honest
    assert "All clear, nothing to see." not in lines, "a card name wrote a line at column 0"
    assert all(ln.startswith("     └─ forged: ") for ln in lines if "0 in this workspace" in ln), lines
    forged = [ln for ln in lines if ln.startswith("     └─ forged: ")]
    assert len(forged) == 1 and "ok-card" in forged[0] and "All clear" in forged[0], forged
    assert any("REQUIVO_CONTEXT_DIR" in ln for ln in lines), lines
    assert any("session.json" in ln and "malformed" in ln for ln in lines), lines
    assert set(run_cli_json(["doctor", "--json"])["sessions"]["unresolved_cards"]) == {"honest", "forged"}


def test_session_verify_cannot_be_made_to_print_a_line_a_session_wrote(forged_workspace):
    """The same forgery on the anti-tampering verb; the remedy follows the finding."""
    def _verify(slug: str) -> list[str]:
        out, code = run_cli_exit(["session", "verify", slug])
        assert code == 1
        return out.splitlines()

    honest = _verify("honest")
    assert any(ln.startswith("  · [unknown_context_card] ") and "gone-card" in ln for ln in honest), honest
    assert any("REQUIVO_CONTEXT_DIR" in ln for ln in honest), honest
    forged = _verify("forged")
    assert not any("REQUIVO_CONTEXT_DIR" in ln for ln in forged), forged
    assert any("session.json" in ln and "malformed" in ln for ln in forged), forged
    assert "All clear, nothing to see." not in forged and not any(_SESSIONS_ROW.match(ln) for ln in forged), forged
    assert len([ln for ln in forged if ln.startswith("  · [") and "ok-card" in ln]) == 1, forged


def test_session_show_renders_a_card_name_as_one_line(forged_workspace):
    """The third render site, which no selector guard can reach (#40)."""
    assert "  context  gone-card" in run_cli(["session", "show", "honest"]).splitlines()
    forged = run_cli(["session", "show", "forged"]).splitlines()
    assert "All clear, nothing to see." not in forged, "a card name wrote a line at column 0"
    context = [ln for ln in forged if ln.startswith("  context  ")]
    assert len(context) == 1 and "ok-card" in context[0], forged


def test_impact_cannot_be_made_to_print_a_line_by_an_unmatched_slot_token():
    """The gap the #40 guard left open; an unmatched slot exits 1 since #250."""
    seed_session("imp", "Something.")
    assert "Unknown slot" not in run_cli(["impact", "imp", "workflow"])
    unknown, code = run_cli_exit(["impact", "imp", "zzz"])
    assert code == 1 and any(ln.startswith("Unknown slot(s): zzz") for ln in unknown.splitlines()), unknown
    forged = run_cli_exit(["impact", "imp", "\nFORGED AT COLUMN 0"])[0].splitlines()
    assert "FORGED AT COLUMN 0" not in forged, forged
    named = [ln for ln in forged if ln.startswith("Unknown slot(s): ")]
    assert len(named) == 1 and "FORGED AT COLUMN 0" in named[0], forged


# One forgery per untrusted `str` on `session show`'s text path (#70); `session_id` is sliced to 12 first.
_SHOW_FORGERIES = {
    "slug": "s\nSession 'trusted'  (id 000000000000…)",
    "session_id": "ab\nFORGED SESSION ID",
    "created_at": "2026-01-01T00:00:00Z\n  revision 999",
    "updated_at": "2026-01-01T00:00:00Z\n  provider trusted   model trusted",
    "provider": "anthropic\n  revision 999",
    "model_name": "claude\n  context  all cards",
    "artifact_status": {
        "prd\n    brief        trusted.md                 rev 9  fresh": {
            "revision": 1, "filename": "prd.md\n    stories      trusted.md                 rev 9  fresh",
            "updated_at": "2026-01-01T00:00:00Z", "stale": False}},
}
((_FORGED_TYPE, _FORGED_STATUS),) = _SHOW_FORGERIES["artifact_status"].items()


def test_session_show_cannot_be_made_to_print_a_line_a_session_wrote():
    """#70: eight lines, every forged value shown escaped on the line that owns it, slice before escape."""
    seed_session("victim", "Something.", analysed=False)
    forge_meta("victim", _SHOW_FORGERIES)
    out = run_cli(["session", "show", "victim"])
    lines = out.splitlines()
    assert len(lines) == 8 and len([ln for ln in lines if ln.startswith("Session '")]) == 1, out
    for label in ("  created  ", "  updated  ", "  revision ", "  provider ", "  context  "):
        assert len([ln for ln in lines if ln.startswith(label)]) == 1, (label, out)
    assert lines[6] == "  artifacts:" and len([ln for ln in lines if ln.startswith("    ")]) == 1, out
    assert lines[3] == "  revision 0", out
    for i, value in ((0, _SHOW_FORGERIES["slug"]), (1, _SHOW_FORGERIES["created_at"]),
                     (2, _SHOW_FORGERIES["updated_at"]), (4, _SHOW_FORGERIES["provider"]),
                     (4, _SHOW_FORGERIES["model_name"]), (7, _FORGED_TYPE), (7, _FORGED_STATUS["filename"])):
        assert repr(value) in lines[i], (i, value, lines[i])
    assert repr(_SHOW_FORGERIES["session_id"][:12]) in lines[0], lines[0]


def test_session_show_leaves_an_ordinary_session_byte_for_byte(tmp_path):
    """The other half of #70: the fix cost nothing."""
    _save_artifact(seed_session("plain", "Reconcile event check-ins."), "prd", "# PRD\n", tmp_path)
    m = store.read_meta("plain")
    st = m.artifact_status["prd"]
    assert run_cli(["session", "show", "plain"]).splitlines() == [
        f"Session '{m.slug}'  (id {m.session_id[:12]}…)",
        f"  created  {m.created_at}",
        f"  updated  {m.updated_at}",
        f"  revision {m.current_revision}",
        f"  provider {m.provider or '—'}   model {m.model_name or '—'}",
        "  context  all cards",
        "  artifacts:",
        f"    {'prd':<12} {st.filename:<26} rev {st.revision}  fresh",
    ]


def test_session_show_json_escapes_a_control_character_before_it_reaches_a_line():
    """`--json` needs no `display_token`: the newline half by the grammar, the NEL half by `ensure_ascii` (#62)."""
    seed_session("j", "Something.", analysed=False)
    forge_meta("j", dict(_SHOW_FORGERIES, model_name="claude\x85FORGED BY A NEL"))
    raw = run_cli(["session", "show", "j", "--json"])
    assert "\nSession 'trusted'" not in raw and "\\nSession 'trusted'" in raw, raw
    assert "\x85" not in raw and "\\u0085FORGED BY A NEL" in raw, raw
    assert len(raw.splitlines()) == raw.count("\n"), "a value split a line of the payload"
    parsed = json.loads(raw)
    assert parsed["slug"] == _SHOW_FORGERIES["slug"] and parsed["model_name"] == "claude\x85FORGED BY A NEL"


def test_the_two_output_paths_guard_different_ranges_and_json_is_the_stricter():
    """Where the terminal guard stops (U+2028), stated as a test so the claim cannot drift (#70)."""
    from requivo.core.selectors import display_token

    sep = " "
    assert len(f"a{sep}b".splitlines()) == 2
    assert display_token(f"a{sep}b") == f"a{sep}b", "the terminal guard is documented as not covering U+2028"
    seed_session("lsep", "Something.", analysed=False)
    forge_meta("lsep", {"provider": f"anthropic{sep}FORGED BY A LINE SEPARATOR"})
    raw = run_cli(["session", "show", "lsep", "--json"])
    assert sep not in raw and "\\u2028FORGED BY A LINE SEPARATOR" in raw, raw
    assert len(raw.splitlines()) == raw.count("\n"), "a value split a line of the payload"


def test_artifact_list_cannot_be_made_to_print_a_row_a_session_wrote():
    """The sibling verb, found by sweeping the class rather than the instance (#70)."""
    seed_session("al", "Something.", analysed=False)
    forge_meta("al", {"artifact_status": _SHOW_FORGERIES["artifact_status"]})
    lines = run_cli(["artifact", "list", "al"]).splitlines()
    assert lines[0] == "Artifacts for 'al':" and len(lines) == 2, lines
    assert repr(_FORGED_TYPE) in lines[1] and repr(_FORGED_STATUS["filename"]) in lines[1], lines[1]
    assert lines[1].endswith("rev 1  fresh"), lines[1]
    seed_session("al2", "Other.", analysed=False)
    forge_meta("al2", {"artifact_status": {"prd": {"revision": 1, "filename": "prd.md",
                                                   "updated_at": "2026-01-01T00:00:00Z", "stale": False}}})
    assert run_cli(["artifact", "list", "al2"]).splitlines() == [
        "Artifacts for 'al2':", f"  {'prd':<12} {'prd.md':<26} rev 1  fresh"]


def test_run_candidate_listing_cannot_be_made_to_print_a_line_a_session_wrote():
    """#541's several-sessions listing reads `updated_at` straight off `session.json`."""
    seed_session("honest", "Something.")
    seed_session("forged", "Other.")
    forged_value = "2026-01-01T00:00:00Z\n  ✅ sessions        0 in this workspace"
    forge_meta("forged", {"updated_at": forged_value})
    printed = run_cli(["run"], client=FakeClient())   # neither session has an open question: no paid call
    assert forged_value not in printed, "the raw newline wrote its own line, unescaped"
    assert repr(forged_value) in printed, "the forged value was dropped rather than shown, escaped"


