"""The plugin's public copy must not offer a provider-backed CLI verb as if it were keyless (#542)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "claude-code"

# The generators the CLI's own optional API mode can produce (#542).
CLI_API_MODE_GENERATORS = ("criteria", "epic", "release", "stories", "estimate")


def _descriptions():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    catalog = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = next(p for p in catalog["plugins"] if p["name"] == "requivo")
    return {"plugin.json": manifest["description"], "marketplace.json": entry["description"]}


@pytest.mark.parametrize("site", sorted(_descriptions()))
def test_a_description_offering_the_cli_generators_says_they_need_a_key(site):
    text = _descriptions()[site]
    if "no API key" not in text:
        pytest.skip(f"{site} makes no keyless claim, so there is nothing to qualify")
    named = [v for v in CLI_API_MODE_GENERATORS if v in text.lower()]
    if not named:
        return
    assert "API mode" in text, (
        f"{site} claims 'no API key' and offers the CLI's {named} in the same breath, without saying "
        f"those run in Requivo's optional API mode and do need one"
    )


def test_the_readme_section_that_lists_the_cli_generators_names_what_they_need():
    """The landing page, where the reader decides to run the command."""
    text = (PLUGIN / "README.md").read_text(encoding="utf-8")
    heading = "## The generators"
    assert heading in text, "the section naming what the CLI's own API mode needs is gone or renamed"
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    named = [v for v in CLI_API_MODE_GENERATORS if v in section.lower()]
    assert named, f"{heading!r} names none of the CLI's API-mode generators; is this still that section?"
    assert "ANTHROPIC_API_KEY" in section, (
        f"{heading!r} offers {named} without naming the key they need"
    )
    assert "requivo[anthropic]" in section, (
        f"{heading!r} offers {named} without naming the extra they need"
    )
