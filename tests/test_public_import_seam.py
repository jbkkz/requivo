"""The declared Python import seam (#423) exists, and compatibility.md's declaration of it
actually names things the package has.

Filed from the 2026-09 readiness audit: the Python import surface used to be blanket-disclaimed in
compatibility.md while requivo-cloud already imported nine names across four modules, including six
provider-internal generator functions decision record 0003 flagged unstable. This file is the
mechanical half of closing that gap -- it does not judge whether the *chosen* seam is the right one
(that is compatibility.md's own prose, and a human's review of it), only that every name declared
there resolves, that `py.typed` actually ships, and that the five surfaces the page's own
"neither column" rule used to leave silent (render/paths/streams/cli/web) now carry a verdict.

Would this pass if #423 did nothing? No: before the fix `src/requivo/py.typed` does not exist, and
none of the names below are mentioned in compatibility.md at all -- the section they belong to does
not exist yet.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "src" / "requivo"
COMPAT_MD = REPO_ROOT / "docs" / "compatibility.md"

# (module -> names) -- the seam this change declares. Mirrors compatibility.md's own "Python import
# surface" section; kept here rather than parsed out of the page because the page is prose with a
# reason attached to each name, not a machine-readable table, and a name declared there that this
# suite cannot even import is the drift most worth catching mechanically.
SEAM: dict[str, tuple[str, ...]] = {
    "requivo.services.sessions": (
        "SessionService", "UpdateResult", "SessionEntry", "SessionSnapshot", "Readiness", "RescopeResult",
    ),
    "requivo.services.discovery": ("DiscoveryService", "Generated"),
    "requivo.services.artifacts": (
        "ArtifactService", "UnknownArtifactTypeError", "UnstatedSourceRevisionError",
        "UnreadableSourceRevisionError",
    ),
    "requivo.services.repository": ("SessionRepository", "FileSessionRepository", "default_repository"),
    "requivo.providers.base": ("ReasoningProvider",),
    "requivo.providers.errors": ("EngineError",),
    "requivo.core.contracts": (
        "EngineOutput", "ModelProposal", "Brief", "PRD", "AcceptanceCriteria", "Epic", "ReleaseNotes",
        "Stories", "EstimateDraft",
    ),
    "requivo.core.persistence": ("SessionMeta", "ArtifactStatus", "RevisionRecord", "UnexaminableEntry"),
    "requivo.core.errors": ("RequivoError",),
    "requivo.usage": ("UsageLedger", "CallRecord", "track_usage", "record_call", "current_ledger"),
}

# Modules the page's own "neither column" rule (#89) left silent before this change. #423's third
# acceptance criterion is that each gets a verdict, so each must appear in the new section too.
NEWLY_CLASSIFIED_MODULES = ("requivo.render", "requivo.paths", "requivo.streams", "requivo.cli", "requivo.web")


def test_every_declared_seam_name_actually_resolves():
    unresolved = []
    for module_name, names in SEAM.items():
        module = importlib.import_module(module_name)
        for name in names:
            if not hasattr(module, name):
                unresolved.append(f"{module_name}.{name}")
    assert unresolved == [], f"declared but does not exist: {unresolved}"


def test_the_package_ships_a_py_typed_marker():
    marker = PACKAGE_ROOT / "py.typed"
    assert marker.is_file(), "PEP 561 marker src/requivo/py.typed is missing"
    # Empty is the PEP 561 convention -- content is never read, only presence.
    assert marker.read_text(encoding="utf-8") == ""


def test_py_typed_is_shipped_as_package_data():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(r"\[tool\.setuptools\.package-data\](.*?)(\n\[|\Z)", text, re.DOTALL)
    assert section, "no [tool.setuptools.package-data] table in pyproject.toml"
    assert "py.typed" in section.group(1), (
        "py.typed is not listed under [tool.setuptools.package-data] -- it would ship on disk in "
        "this checkout but not in the wheel"
    )


def test_compatibility_md_declares_the_seam():
    text = COMPAT_MD.read_text(encoding="utf-8")
    missing = []
    for module_name, names in SEAM.items():
        for name in names:
            if name not in text:
                missing.append(f"{module_name}.{name}")
    assert missing == [], f"declared seam names not mentioned in docs/compatibility.md: {missing}"


def test_compatibility_md_classifies_the_previously_silent_modules():
    text = COMPAT_MD.read_text(encoding="utf-8")
    missing = [m for m in NEWLY_CLASSIFIED_MODULES if m not in text]
    assert missing == [], (
        f"{missing} were in neither compatibility.md column before #423 and must get a verdict now "
        "(the page's own file-a-bug rule at the foot of What is explicitly not stable)"
    )


def test_the_recommended_consumption_pattern_is_stated():
    text = COMPAT_MD.read_text(encoding="utf-8")
    assert "requivo==" in text, "the exact-pin recommendation (requivo==X.Y.Z) is not stated"
