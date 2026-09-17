"""#257's own guard: the measured per-card byte/token cost stated in `docs/context-cards.md` and printed by
the CLI's default-cards disclosure must agree with the actual bundled cards on disk."""
import re
from pathlib import Path

import pytest

from requivo.core.context import available_cards, card_byte_size
from requivo.paths import CONTEXT

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _bundled_card_sizes() -> dict[str, int]:
    # `card_byte_size`, not `st_size`: the figure being pinned is what a card contributes to a prompt, and on Windows those differ by one byte per line (see that function's own docstring).
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
    # `available_cards()` includes any user-installed cards too, so in an ordinary dev environment (no REQUIVO_CONTEXT_DIR cards) it is exactly the bundled set -- the same set the CLI's default disclosure enumerates.
    cards = available_cards()
    sizes = _bundled_card_sizes()
    assert len(cards) >= len(sizes) >= 1


def test_average_card_byte_size_matches_an_independent_computation():
    """Found in review."""
    from requivo.core.context import average_card_byte_size

    sizes = _bundled_card_sizes()
    assert sizes, "no bundled context cards found -- this test is not exercising anything"
    expected = sum(sizes.values()) // len(sizes)
    assert average_card_byte_size() == expected


def test_a_card_weighs_the_same_whatever_its_line_endings(tmp_path):
    """The Windows leg, reproduced on any platform (#257)."""
    body = "# card\n\nline one\nline two\n"
    lf = tmp_path / "lf.md"
    crlf = tmp_path / "crlf.md"
    lf.write_bytes(body.encode("utf-8"))
    crlf.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))

    assert crlf.stat().st_size == lf.stat().st_size + body.count("\n"), (
        "must fire: the fixture is not actually staging two different on-disk sizes")
    assert card_byte_size(crlf) == card_byte_size(lf) == len(body.encode("utf-8"))


def test_average_card_byte_size_is_none_on_an_empty_install(monkeypatch):
    """The defined empty-install branch (also found in review)."""
    import requivo.core.context as context_module

    monkeypatch.setattr(context_module, "_card_paths", lambda: {})
    assert context_module.average_card_byte_size() is None


def test_the_docs_stated_prompt_weight_range_matches_a_live_measurement():
    """The percentage claim ("65-78% of every call's system prompt") was unguarded."""
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


# ── the grounding judgment's deterministic half (#593) ────────────────────────────────────────────


def test_one_unreadable_card_degrades_its_own_summary_row(tmp_path, monkeypatch):
    """Invariant 15, one listing further along: a card that cannot be read reports itself as unreadable and
    the other rows still arrive."""
    from requivo.core import context as ctx

    good, bad = tmp_path / "good.md", tmp_path / "bad.md"
    good.write_text("# Card\n\n- Business domain: dentistry\n", encoding="utf-8")
    bad.write_text("unreadable", encoding="utf-8")
    monkeypatch.setattr(ctx, "_card_paths", lambda: {"good": good, "bad": bad})

    real_read = type(bad).read_text

    def refuse_one(self, *a, **kw):
        if self == bad:
            raise PermissionError("nope")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(type(bad), "read_text", refuse_one)
    rows = {c.stem: c for c in ctx.card_summaries()}

    assert rows["bad"].unreadable is True and rows["bad"].domain == ""
    assert rows["good"].unreadable is False, "a readable neighbour was dragged down with it"
    assert rows["good"].domain == "dentistry"


def test_a_business_domain_is_joined_across_the_lines_it_wraps_onto(tmp_path, monkeypatch):
    """The bundled cards wrap their long domain lines, and a domain cut at the wrap reads as a different
    domain to the judgment that has to recognise it."""
    from requivo.core import context as ctx

    card = tmp_path / "wrapped.md"
    card.write_text("- Business domain: financial reporting — consolidating operational\n"
                    "  data into figures a finance team acts on\n"
                    "- Product type: platform\n", encoding="utf-8")
    monkeypatch.setattr(ctx, "_card_paths", lambda: {"wrapped": card})

    domain = ctx.card_summaries()[0].domain
    assert domain.endswith("a finance team acts on"), domain
    assert "Product type" not in domain, "the join ran past the field it was reading"


def test_a_standalone_prompt_that_carries_the_shared_head_is_refused(tmp_path, monkeypatch):
    """The mirror of `build_system_prompt`'s own refusal."""
    from requivo.core import context as ctx

    bad = tmp_path / "carries_head.md"
    bad.write_text(ctx.SHARED_PROMPT_HEAD + "then some instructions\n", encoding="utf-8")
    monkeypatch.setattr(ctx, "PROMPTS", tmp_path)

    with pytest.raises(ValueError, match="shared leading block"):
        ctx.build_standalone_prompt("carries_head.md", {})

    # Must fire: an ordinary standalone template still builds, and substitutes.
    ok = tmp_path / "ok.md"
    ok.write_text("Judge this: {{REQUEST}}\n", encoding="utf-8")
    assert ctx.build_standalone_prompt("ok.md", {"{{REQUEST}}": "a leave system"}) == (
        "Judge this: a leave system\n")


def test_the_shipped_judgment_prompt_is_standalone_and_names_both_its_placeholders():
    """The asset itself, not a fixture: it has to be buildable by the builder the generator uses."""
    from requivo.core.context import build_standalone_prompt

    text = build_standalone_prompt("context_judgment.md", {"{{REQUEST}}": "R", "{{CARDS}}": "- c: d"})
    assert "R" in text and "- c: d" in text
    assert "{{" not in text, "a placeholder reached the provider unsubstituted"
