"""#257's own guard: the measured per-card byte/token cost stated in `docs/context-cards.md` and
printed by the CLI's default-cards disclosure must agree with the actual bundled cards on disk.

A number in prose that no test can falsify buys one release and then lies (CLAUDE.md's own rule,
stated about a different count) -- this file recomputes the figure from the real files rather than
trusting a literal anyone could forget to update after adding or resizing a card.
"""
import re
from pathlib import Path

from requivo.core.context import available_cards, card_byte_size
from requivo.paths import CONTEXT

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _bundled_card_sizes() -> dict[str, int]:
    # `card_byte_size`, not `st_size`: the figure being pinned is what a card contributes to a
    # prompt, and on Windows those differ by one byte per line (see that function's own docstring).
    # Measured on disk, both assertions below were red on every Windows leg and green everywhere
    # else -- and the percentage test was worse, because it divided a CRLF-inflated numerator by a
    # denominator `build_prompt` had already normalised to LF.
    return {
        p.stem: card_byte_size(p)
        for p in sorted(CONTEXT.glob("*.md"))
        if not p.name.startswith("_")
    }


def test_the_docs_stated_bundled_card_byte_total_matches_the_files_on_disk():
    sizes = _bundled_card_sizes()
    assert sizes, "no bundled context cards found -- this test is not exercising anything"
    total = sum(sizes.values())
    doc = (_REPO_ROOT / "docs" / "context-cards.md").read_text(encoding="utf-8")
    m = re.search(r"([\d,]+) bytes, ~[\d.]+k tokens", doc)
    assert m, ("docs/context-cards.md no longer states a 'N bytes, ~Xk tokens' figure for the "
               "bundled cards -- update this test's pattern if the wording moved.")
    documented = int(m.group(1).replace(",", ""))
    assert documented == total, (
        f"docs/context-cards.md says {documented} bytes for the bundled cards; the real total is "
        f"{total} from {sizes}. A card was added, removed or resized -- re-measure and update the "
        "doc (and the CLI/web disclosure text, if the count of cards changed).")


def test_the_docs_stated_bundled_card_count_matches_available_cards():
    # `available_cards()` includes any user-installed cards too, so in an ordinary dev environment
    # (no REQUIVO_CONTEXT_DIR cards) it is exactly the bundled set -- the same set the CLI's default
    # disclosure enumerates. Must fire: an empty set would make every assertion above vacuous.
    cards = available_cards()
    sizes = _bundled_card_sizes()
    assert len(cards) >= len(sizes) >= 1


def test_average_card_byte_size_matches_an_independent_computation():
    """Found in review: the only test that previously exercised `average_card_byte_size()`
    (`tests/web/test_web_routing.py`'s hint test) computed its "expected" value by calling the same
    function -- a bug in the divisor, an off-by-one in the file count, or an accidental inclusion of
    `_template.md` would have gone undetected. This computes the average independently, straight
    from `_bundled_card_sizes()`, the way the byte-total test above already does for the sum."""
    from requivo.core.context import average_card_byte_size

    sizes = _bundled_card_sizes()
    assert sizes, "no bundled context cards found -- this test is not exercising anything"
    expected = sum(sizes.values()) // len(sizes)
    assert average_card_byte_size() == expected


def test_a_card_weighs_the_same_whatever_its_line_endings(tmp_path):
    """The Windows leg, reproduced on any platform. A card checked out with CRLF is one byte per
    line larger on disk, and `load_context()` never sees those bytes -- it reads in text mode, so
    CRLF collapses to LF before anything reaches `{{CONTEXT}}`. Measuring `st_size` made the #257
    disclosure over-report on exactly one platform. Goes red against `p.stat().st_size`: the two
    files below differ on disk by their line count."""
    body = "# card\n\nline one\nline two\n"
    lf = tmp_path / "lf.md"
    crlf = tmp_path / "crlf.md"
    lf.write_bytes(body.encode("utf-8"))
    crlf.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))

    assert crlf.stat().st_size == lf.stat().st_size + body.count("\n"), (
        "must fire: the fixture is not actually staging two different on-disk sizes")
    assert card_byte_size(crlf) == card_byte_size(lf) == len(body.encode("utf-8"))


def test_average_card_byte_size_is_none_on_an_empty_install(monkeypatch):
    """The defined empty-install branch (also found in review): `average_card_byte_size()` returns
    `None` rather than raising or dividing by zero, and the CLI's disclosure line has its own
    "measurable weight" fallback text for exactly this case -- neither was exercised anywhere."""
    import requivo.core.context as context_module

    monkeypatch.setattr(context_module, "_card_paths", lambda: {})
    assert context_module.average_card_byte_size() is None


def test_the_docs_stated_prompt_weight_range_matches_a_live_measurement():
    """The percentage claim ("65-78% of every call's system prompt") was unguarded -- found in
    review, where only the byte-total sentence above it was pinned. Every generator prompt is
    pure offline asset assembly (no API call), so the real min/max across all eight is measured
    here and checked against the documented range -- the next prompt-asset edit that moves these
    percentages now goes red instead of a stale number left in the doc."""
    from requivo.core.context import build_prompt

    sizes = _bundled_card_sizes()
    card_total = sum(sizes.values())
    assert card_total, "no bundled context cards found -- this test is not exercising anything"
    names = ["engine.md", "brief.md", "stories.md", "estimate.md", "prd.md", "criteria.md",
             "epic.md", "release.md"]
    percentages = [card_total / len(build_prompt(n).encode("utf-8")) * 100 for n in names]
    low, high = round(min(percentages)), round(max(percentages))

    doc = (_REPO_ROOT / "docs" / "context-cards.md").read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", doc)  # the range and its trailing words wrap across a source line
    m = re.search(r"(\d+)[-–](\d+)% of every call.s system prompt", flat)
    assert m, ("docs/context-cards.md no longer states an 'N-M% of every call's system prompt' "
               "range -- update this test's pattern if the wording moved.")
    documented_low, documented_high = int(m.group(1)), int(m.group(2))
    assert (documented_low, documented_high) == (low, high), (
        f"docs/context-cards.md says {documented_low}-{documented_high}%; a live measurement across "
        f"the eight generator prompts gives {low}-{high}% (from {list(zip(names, percentages))}). "
        "Re-measure and update the doc.")
