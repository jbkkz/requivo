"""The plugin's public copy must not offer a provider-backed CLI verb as if it were keyless.

The plugin's pitch is that reasoning runs in the reader's own Claude Code session, so "there is no API
key to configure" -- and the install section says in bold that the `requivo[anthropic]` extra is not
needed. Both true. But the catalog description then said the reader could "hand the same model to the
requivo CLI for acceptance criteria and tracker epics", and the README's *Beyond the six skills*
section (since #542, *The generators*) listed those generators with no mention of what they need. They
are provider verbs when the CLI runs them itself: they call the Anthropic API directly, so they need
the extra *and* a key. A marketplace reader following that pointer met the missing-SDK error first and
the missing-key error second, having been told twice that neither applied (#242). Since #542 the same
five also run keylessly as plugin skills -- this guard is about the CLI's *own* optional API mode,
which still needs both, not about whether a keyless path exists elsewhere on the page.

Both halves of the storefront are checked, because they are two hand-edited files saying one thing:
the catalog line a reader scans, and the page they land on.

The verb list is a literal here, on purpose. Deriving it would mean importing the CLI's provider
registry, and this test is about *prose*: what goes wrong is a sentence, and the sentence names these
words whether or not the registry still calls them that. A verb leaving the registry is
`test_plugin_cli_drift.py`'s job and it already fails on the plugin naming a verb that does not exist.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "claude-code"

# The generators the CLI's own optional API mode can produce -- provider-backed in THAT mode,
# regardless of whether a plugin skill also wraps the same artifact type keylessly (#542).
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
    """The landing page, where the reader decides to run the command. Checked on that one section
    rather than on the whole file: the file mentions `ANTHROPIC_API_KEY` elsewhere, in the install
    section that says the reader does *not* need it, so a whole-file scan would pass on the defect."""
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
