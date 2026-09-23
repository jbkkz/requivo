"""The context cards: how a card's weight is measured (#257), the card summaries the grounding
judgment reads (#593), and the one artifact caption the assets may not drift from (#166)."""
import re
from pathlib import Path

import pytest

from requivo.core import context as ctx
from requivo.core.context import average_card_byte_size, build_standalone_prompt, card_byte_size
from requivo.paths import CONTEXT

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _bundled_card_sizes() -> dict[str, int]:
    # `card_byte_size`, not `st_size`: what a card contributes to a prompt, which on Windows differs by one byte per line.
    sizes = {p.stem: card_byte_size(p) for p in sorted(CONTEXT.glob("*.md")) if not p.name.startswith("_")}
    assert sizes, "no bundled context cards found -- this test is not exercising anything"
    return sizes


def test_average_card_byte_size_matches_an_independent_computation(monkeypatch):
    sizes = _bundled_card_sizes()
    assert average_card_byte_size() == sum(sizes.values()) // len(sizes)
    monkeypatch.setattr(ctx, "_card_paths", lambda: {})
    assert ctx.average_card_byte_size() is None, "the defined empty-install branch"


def test_a_card_weighs_the_same_whatever_its_line_endings(tmp_path):
    """The Windows leg, reproduced on any platform (#257)."""
    body = "# card\n\nline one\nline two\n"
    lf, crlf = tmp_path / "lf.md", tmp_path / "crlf.md"
    lf.write_bytes(body.encode("utf-8"))
    crlf.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    assert crlf.stat().st_size == lf.stat().st_size + body.count("\n"), "must fire: two different on-disk sizes"
    assert card_byte_size(crlf) == card_byte_size(lf) == len(body.encode("utf-8"))


# ── the grounding judgment's deterministic half (#593) ───────────────────────────


def test_one_unreadable_card_degrades_its_own_summary_row(tmp_path, monkeypatch):
    """Invariant 15, one listing further along: an unreadable card reports itself and the other rows still arrive."""
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
    assert rows["good"].unreadable is False and rows["good"].domain == "dentistry"


def test_a_business_domain_is_joined_across_the_lines_it_wraps_onto(tmp_path, monkeypatch):
    card = tmp_path / "wrapped.md"
    card.write_text("- Business domain: financial reporting — consolidating operational\n"
                    "  data into figures a finance team acts on\n- Product type: platform\n", encoding="utf-8")
    monkeypatch.setattr(ctx, "_card_paths", lambda: {"wrapped": card})
    domain = ctx.card_summaries()[0].domain
    assert domain.endswith("a finance team acts on") and "Product type" not in domain, domain


def test_a_standalone_prompt_that_carries_the_shared_head_is_refused(tmp_path, monkeypatch):
    """The mirror of `build_system_prompt`'s own refusal; an ordinary standalone template still builds (must fire)."""
    (tmp_path / "carries_head.md").write_text(ctx.SHARED_PROMPT_HEAD + "then some instructions\n", encoding="utf-8")
    (tmp_path / "ok.md").write_text("Judge this: {{REQUEST}}\n", encoding="utf-8")
    monkeypatch.setattr(ctx, "PROMPTS", tmp_path)
    with pytest.raises(ValueError, match="shared leading block"):
        ctx.build_standalone_prompt("carries_head.md", {})
    assert ctx.build_standalone_prompt("ok.md", {"{{REQUEST}}": "a leave system"}) == "Judge this: a leave system\n"


def test_the_shipped_judgment_prompt_is_standalone_and_names_both_its_placeholders():
    text = build_standalone_prompt("context_judgment.md", {"{{REQUEST}}": "R", "{{CARDS}}": "- c: d"})
    assert "R" in text and "- c: d" in text and "{{" not in text, "a placeholder reached the provider unsubstituted"


# ── the artifact `brief` has one user-facing name; an asset keeping the older one is a declared exception (#166) ──

ASSETS = _REPO_ROOT / "src" / "requivo" / "assets"
GENERATORS = _REPO_ROOT / "src" / "requivo" / "providers" / "anthropic" / "generators.py"
ELICITATION = ASSETS / "perimeters" / "software" / "elicitation.md"
BRIEF_PROMPT = ASSETS / "prompts" / "brief.md"
_OLD_PHRASE = re.compile(r"solution assessment", re.IGNORECASE)
_DECLARED_EXCEPTIONS = {BRIEF_PROMPT}  # golden-measured prompts keep their wording; the reason is at the call site


def test_every_asset_not_declared_an_exception_uses_the_current_vocabulary():
    files = sorted(p for p in ASSETS.rglob("*.md") if not p.name.startswith("_"))
    assert BRIEF_PROMPT in files and ELICITATION in files, f"the scan is not seeing the asset tree under {ASSETS}"
    assert _OLD_PHRASE.search(BRIEF_PROMPT.read_text(encoding="utf-8")), "the positive control: brief.md dropped the old wording"
    offenders = [p for p in files if p not in _DECLARED_EXCEPTIONS and _OLD_PHRASE.search(p.read_text(encoding="utf-8"))]
    assert not offenders, "these assets still say \"solution assessment\" and are not in _DECLARED_EXCEPTIONS:\n" + "\n".join(map(str, offenders))


def test_the_declared_exception_records_its_reason_at_the_call_site():
    """`brief.md` cannot carry its own exemption comment; `elicitation.md` is not golden-measured so it renames outright."""
    elicitation = ELICITATION.read_text(encoding="utf-8")
    assert not _OLD_PHRASE.search(elicitation) and "Decision brief" in elicitation, "elicitation.md should name the current caption"
    text = GENERATORS.read_text(encoding="utf-8")
    assert "brief.md" in text and "#166" in text, "the reason brief.md keeps the old wording must be recorded at the call site"
    assert re.search(r"golden_run|golden_diff|golden harness", text, re.IGNORECASE), "the #166 note must name the golden harness"
