"""The artifact `brief` has one user-facing name; an asset that keeps the older one is a
declared exception, never an accident (#166).

`web/viewmodels/labels.py` renamed the caption everywhere a person reads it: "Decision brief", not
"solution assessment". Two assets under `src/requivo/assets/` still carried the old wording after
that rename, and they are not interchangeable:

- `assets/prompts/brief.md` is read into the Anthropic system prompt by `build_prompt()` on every
  `requivo brief` call (`providers/anthropic/generators.py:advise`). Changing its wording is a
  prompt-tuning edit that goes through the golden harness (`scripts/golden_run.py --brief`, then
  `scripts/golden_diff.py`) and moves the committed baseline in `fixtures/golden/` -- real API
  spend. Leaving it alone is a legitimate outcome, so it is a **declared exception** here rather
  than a rename, and the reason has to live at the call site: the asset itself is fed to the model
  verbatim and cannot carry an explanatory comment without becoming part of the prompt.
- `assets/perimeters/software/elicitation.md` is the human-readable spec of the framework. It is not
  assembled by `build_prompt()`/`load_context()` -- the only reader in the tree is
  `deterministic/doctor.py`'s `--framework` branch of `_cmd_schema`, which prints it for a human or
  a Claude Code agent reading `requivo schema --framework` directly, outside the API path the
  golden harness measures. Nothing here can move a baseline, so it carries the current vocabulary.

This guard is mechanical, not a judgement about which asset should keep the old wording -- that
call is CLAUDE.md's "Where a bug narrative lives" territory, applied once above. What this file
enforces is that the split stays exactly two-valued: every asset either uses the current caption,
or is named in `_DECLARED_EXCEPTIONS` with its reason recorded at the call site the next line
checks for. A third asset drifting to the old wording with nobody deciding so is what this guard
exists to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "src" / "requivo" / "assets"
GENERATORS = REPO_ROOT / "src" / "requivo" / "providers" / "anthropic" / "generators.py"
ELICITATION = ASSETS / "perimeters" / "software" / "elicitation.md"
BRIEF_PROMPT = ASSETS / "prompts" / "brief.md"

_OLD_PHRASE = re.compile(r"solution assessment", re.IGNORECASE)

# Every `.md` asset that deliberately keeps the old wording, and why (#166): `brief.md` is
# model-read and golden-measured (see module docstring); nothing else in this tree earns that
# exception. Adding a name here without a matching reason in the module docstring above is exactly
# the drift this guard exists to catch -- so keep the two in sync by hand, the same discipline
# CLAUDE.md's narrative-reference rule asks of a comment paragraph.
_DECLARED_EXCEPTIONS = {BRIEF_PROMPT}


def _md_files() -> list[Path]:
    return sorted(p for p in ASSETS.rglob("*.md") if not p.name.startswith("_"))


def test_the_scan_actually_sees_the_asset_tree():
    """Guards the guard: an empty walk would make every assertion below vacuously true (invariant 7's
    own lesson -- `assert not []` is an all-clear nobody earned)."""
    files = _md_files()
    assert files, f"expected asset markdown files under {ASSETS}, found none"
    assert BRIEF_PROMPT in files and ELICITATION in files


def test_the_declared_exception_still_carries_the_old_wording():
    """Positive control for the negative assertion below: the phrase this guard hunts for is real
    and present where it is supposed to be. Without this, a walk that silently found nothing would
    pass every "must not appear" check for the wrong reason."""
    assert _OLD_PHRASE.search(BRIEF_PROMPT.read_text(encoding="utf-8")), (
        "brief.md no longer says \"solution assessment\" -- either the rename happened (drop it "
        "from _DECLARED_EXCEPTIONS and this test) or the fixture stopped reading the right file"
    )


def test_every_asset_not_declared_an_exception_uses_the_current_vocabulary():
    offenders = [
        p for p in _md_files()
        if p not in _DECLARED_EXCEPTIONS and _OLD_PHRASE.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "these asset files still say \"solution assessment\" and are not declared exceptions in "
        "_DECLARED_EXCEPTIONS: either rename them to \"Decision brief\" or, if the wording is "
        "deliberate (a model-read, golden-measured prompt), add them to the set above with a "
        "reason in the module docstring:\n" + "\n".join(str(p) for p in offenders)
    )


def test_elicitation_md_teaches_the_current_caption():
    """`elicitation.md` is not golden-measured (see module docstring) so it carries the renamed
    vocabulary outright, matching `web/viewmodels/labels.py`'s `ARTIFACT_LABELS["brief"]`."""
    text = ELICITATION.read_text(encoding="utf-8")
    assert not _OLD_PHRASE.search(text), (
        "elicitation.md still says \"solution assessment\" -- this file is not read by "
        "build_prompt()/load_context() (see module docstring), so nothing here is golden-measured "
        "and the rename costs no API spend"
    )
    assert "Decision brief" in text, (
        "elicitation.md should name the artifact by its current caption, matching "
        "web/viewmodels/labels.py's ARTIFACT_LABELS[\"brief\"]"
    )


def test_the_declared_exception_records_its_reason_at_the_call_site():
    """`brief.md` cannot carry its own exemption comment -- the file is fed to the model verbatim,
    so a comment there becomes part of the prompt. The reason has to live where a developer editing
    the caller sees it: `generators.py`, next to the `build_prompt("brief.md", ...)` call."""
    text = GENERATORS.read_text(encoding="utf-8")
    assert "brief.md" in text
    assert "#166" in text, (
        "generators.py has no #166 reference -- the reason brief.md keeps the old wording must be "
        "recorded at the call site, not left implicit"
    )
    assert re.search(r"golden_run|golden_diff|golden harness", text, re.IGNORECASE), (
        "the #166 note in generators.py must name the mechanism (the golden harness) that makes "
        "editing brief.md a spend decision, not just cite the issue number"
    )
