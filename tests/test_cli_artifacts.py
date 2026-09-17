"""End-to-end tests of `requivo.deterministic.artifacts` — `artifact save`, `list` and `show` (#141)."""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout

import pytest
from _cli_harness import _forge_meta, _full_model, _run, _run_json, _slot

from requivo.cli import _build_parser, app


def test_artifact_list_json_has_a_top_level_that_is_not_data(workspace):
    """`artifact list --json` printed `ArtifactService.list` straight out (#107)."""
    _run(["session", "init", "Something.", "--slug", "aj"])
    _forge_meta("aj", {"artifact_status": {"prd": {"revision": 1, "filename": "prd.md",
                                                   "updated_at": "2026-01-01T00:00:00Z",
                                                   "stale": False}}})

    payload = _run_json(["artifact", "list", "aj", "--json"])

    # must not fire: an artifact type is not a top-level key
    assert "prd" not in payload, payload
    # must fire, in the same fixture: it is there, one level down.
    assert "prd" in payload["artifacts"], payload

    assert set(payload) == {"slug", "artifacts"}, payload
    assert payload["slug"] == "aj"
    # ...and it names what was asked for, never the value stored inside, which no reader may trust (invariant 14).
    _forge_meta("aj", {"slug": "forged"})
    assert _run_json(["artifact", "list", "aj", "--json"])["slug"] == "aj"

    # Wrap, not restructure: the row is what `ArtifactService.list` already returned, same keys in the same order (#87).
    assert payload["artifacts"] == {"prd": {"revision": 1, "filename": "prd.md",
                                            "updated_at": "2026-01-01T00:00:00Z", "stale": False}}
    assert list(payload["artifacts"]["prd"]) == ["revision", "filename", "updated_at", "stale"]


def test_artifact_list_json_still_names_the_session_when_it_has_no_artifacts(workspace):
    """The empty case is the one the old shape answered worst (#107)."""
    _run(["session", "init", "Nothing saved yet.", "--slug", "noart"])

    payload = _run_json(["artifact", "list", "noart", "--json"])

    assert payload == {"slug": "noart", "artifacts": {}}


def test_artifact_save_reports_staleness_at_save_time(workspace, tmp_path):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p), "--json"])                       # revision 1
    p2 = tmp_path / "p2.json"
    p2.write_text(json.dumps(_full_model(**{"workflow": _slot(80, "explicit", "high", "new")})))
    _run(["model", "apply", "s", str(p2), "--json"])                      # revision 2

    doc = tmp_path / "prd.md"
    doc.write_text("# PRD\n")
    # Reasoned from revision 1, saved once the session is at 2.
    r = _run_json(["artifact", "save", "s", "--type", "prd", "--file", str(doc),
                   "--revision", "1", "--json"])
    assert r["revision"] == 1 and r["stale"] is True
    assert _run_json(["artifact", "list", "s", "--json"])["artifacts"]["prd"]["stale"] is True

    # This used to omit `--revision` and assert `revision: 2, stale: false` (#6).
    fresh = _run_json(["artifact", "save", "s", "--type", "prd", "--file", str(doc),
                       "--revision", "2", "--json"])
    assert fresh["revision"] == 2 and fresh["stale"] is False

    # …and leaving it off is now refused rather than guessed, on the exact surface the Claude Code plugin drives.
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        app(["artifact", "save", "s", "--type", "prd", "--file", str(doc), "--json"])
    assert exc.value.code == 1
    envelope = json.loads(buf.getvalue())
    assert envelope["details"]["source_revision"] is None
    assert "--revision" in envelope["message"]
    # The code names the omission rather than the session since #57.
    assert envelope["code"] == "unstated_source_revision"
    # and nothing was recorded against the guess: the PRD on disk is still the one saved above.
    assert _run_json(["artifact", "list", "s", "--json"])["artifacts"]["prd"]["revision"] == 2


def test_the_revision_flag_does_not_advertise_a_default_it_no_longer_has():
    """The help text is read *while deciding whether to pass the flag* (#6)."""
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as ei:
        _build_parser().parse_args(["artifact", "save", "--help"])
    assert ei.value.code == 0
    help_text = buf.getvalue()

    assert "--revision" in help_text, "must fire: this is not the help text that owns the flag"
    # Sliced between the two option names rather than read off a wrapped line.
    chunk = help_text.rsplit("--revision", 1)[1].split("--json", 1)[0].lower()
    assert "required" in chunk, f"`--revision` does not say it is required: {chunk!r}"

    root = _build_parser()
    inherited = {opt for a in root._actions for opt in a.option_strings}
    save = _subparser(_subparser(root, "artifact"), "save")
    own = [a for a in save._actions if not (set(a.option_strings) & inherited)]
    # Must fire, and specifically on `--revision` (#249).
    assert any("--revision" in a.option_strings for a in own), (
        "must fire: `--revision` fell out of the set this sweep reads, so the flag the test is "
        "named for is no longer being checked at all")
    assert len(own) >= 4, f"must fire: the walk found only {len(own)} option(s) on `artifact save`"
    offenders = [(a.option_strings or a.dest, form)
                 for a in own for form in ("default:", "defaults to")
                 if form in (a.help or "").lower()]
    assert not offenders, (
        "an option `artifact save` owns advertises a default; `--revision` has had none since #6 "
        f"and no other option on this subcommand has one either: {offenders}")


def _subparser(parser, name):
    """The named child parser, or an assertion failure."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and name in action.choices:
            return action.choices[name]
    raise AssertionError(f"no `{name}` subparser under {parser.prog!r}")
