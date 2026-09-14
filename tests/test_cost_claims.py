"""Every dollar figure in the docs is recomputed from the rate table (#252).

An OSS user pays with their own key before the CLI ever prints a real number, and this repo already shipped a stale price past its own expiry once (#254) -- so a dollar figure in prose is **derived** here or it does not ship.

Four checks, each failing independently: the rate/date come from `pricing.py`; the token ranges bracket what `fixtures/golden/` actually assembles and receives; every dollar figure is arithmetic over those two; and each row's total must follow from its own call count. Token counts are estimated at four
characters per token, since no real `UsageLedger` capture is committed here."""
from __future__ import annotations

import json
import re
from pathlib import Path

from requivo.core.context import build_prompt
from requivo.core.contracts import Brief, EngineOutput
from requivo.providers.anthropic.generators import _OP_PROMPTS
from requivo.providers.anthropic.pricing import PRICING_AS_OF, price_per_mtok

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_DOC = ROOT / "docs" / "providers.md"
README = ROOT / "README.md"

MODEL = "claude-sonnet-5"
CHARS_PER_TOKEN = 4

# `| label | calls | input | output | $lo-$hi |`, with the en dash the docs actually use.
_ROW = re.compile(r"^\|\s*(?P<label>[^|]+?)\s*\|\s*(?P<calls>\d+)\s*\|"
                  r"\s*(?P<input>[\d,–—-]+|—)\s*\|\s*(?P<output>[\d,–—-]+|—)\s*\|"
                  r"\s*\*?\*?\$(?P<lo>[\d.]+)–\$(?P<hi>[\d.]+)\*?\*?\s*\|", re.M)

def _tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN

def _model_dump_tokens(*, absorbed: bool) -> list:
    """Token size of a real resolved model exactly as a generator or refinement turn sends it -- built from `fixtures/golden/` (`runs` and `turns`), validated as `EngineOutput`. `absorbed=False` is the model before `absorb_reasoning` runs (every non-first discovery turn, plus `brief`); `absorbed=True` is after,
    applying the same decisions/challenges/opportunities copy `finalize_discovery` does, so each population matches what a real call actually sends."""
    sizes = []
    for path in sorted((ROOT / "fixtures" / "golden").glob("*.runs.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        runs = data.get("runs") or []
        briefs = data.get("briefs") or []
        if absorbed:
            for i, run in enumerate(runs):
                if i >= len(briefs):
                    continue
                out = EngineOutput.model_validate(run)
                brief = Brief.model_validate(briefs[i])
                out = out.model_copy(update={
                    "decisions": brief.decisions, "challenges": brief.challenges,
                    "opportunities": brief.opportunities,
                })
                sizes.append(_tokens(out.model_dump_json()))
        else:
            replies = list(runs)
            for turn_seq in data.get("turns") or []:
                replies.extend(t["model"] for t in turn_seq if isinstance(t, dict) and "model" in t)
            for reply in replies:
                sizes.append(_tokens(EngineOutput.model_validate(reply).model_dump_json()))
    assert sizes, "no golden captures were read -- an empty scan cannot support a published range"
    return sizes

def measured_input_tokens() -> tuple:
    """Every operation's assembled system prompt plus what a real call adds as its own user message. Only the first discovery turn adds nothing; every other call attaches a resolved model (`estimate` uses the stories size as the same order of magnitude, since no `Stories` reply is captured). `brief` and a
    refinement turn (`analyze`, turn 2+) see the model before its reasoning layer fills; every other generator sees it after -- see `_model_dump_tokens` for which captures back each state."""
    bare = _model_dump_tokens(absorbed=False)
    absorbed = _model_dump_tokens(absorbed=True)
    sizes = []
    for op, name in _OP_PROMPTS.items():
        system = _tokens(build_prompt(name, None))
        if op == "analyze":
            sizes.append(system)                  # the very first turn: nothing to attach yet
            sizes.append(system + min(bare))       # every turn after it: the model so far, unreasoned
            sizes.append(system + max(bare))
        elif op == "brief":
            sizes.append(system + min(bare))       # advise() produces the reasoning; can't have it yet
            sizes.append(system + max(bare))
        else:
            sizes.append(system + min(absorbed))   # every later generator inherits advise()'s reasoning
            sizes.append(system + max(absorbed))
    return min(sizes), max(sizes)

def measured_output_tokens() -> tuple:
    """Every reply this repository has actually captured -- the golden baselines are real API output, which is the closest thing to a ledger the offline suite can reach."""
    sizes = []
    for path in sorted((ROOT / "fixtures" / "golden").glob("*.runs.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        replies = list(data.get("runs") or []) + [b for b in (data.get("briefs") or []) if b]
        for turn in data.get("turns") or []:
            replies.extend(turn if isinstance(turn, list) else [turn])
        sizes.extend(_tokens(json.dumps(r, ensure_ascii=False)) for r in replies)
    assert sizes, "no golden captures were read -- an empty scan cannot support a published range"
    return min(sizes), max(sizes)

def _rows() -> dict:
    return {m.group("label"): m for m in _ROW.finditer(PROVIDERS_DOC.read_text(encoding="utf-8"))}

def _range(text: str) -> tuple:
    lo, hi = re.split(r"[–—-]", text.replace(",", ""))
    return int(lo), int(hi)

def test_the_documented_rate_and_its_date_are_the_rate_table():
    """The half that dates itself. Anthropic's price is not this project's to remember twice."""
    doc = PROVIDERS_DOC.read_text(encoding="utf-8")
    rate = price_per_mtok(MODEL)
    assert rate is not None, f"{MODEL} has no price on file -- the docs cannot quote one either"
    assert f"${rate[0]:.2f} / ${rate[1]:.2f} per million tokens" in doc, (
        f"the documented rate is not {rate} from pricing.py")
    assert PRICING_AS_OF in doc, f"the documented rate date is not PRICING_AS_OF ({PRICING_AS_OF})"

def test_the_documented_token_ranges_bracket_what_this_repository_measures():
    """A published range must be true of the prompts and replies actually in the tree. Bracketing rather than equality on purpose: the docs round to a readable figure, and rounding *outwards* is the only direction that keeps the claim honest."""
    row = _rows()["One provider call"]
    doc_in, doc_out = _range(row.group("input")), _range(row.group("output"))
    real_in, real_out = measured_input_tokens(), measured_output_tokens()
    assert doc_in[0] <= real_in[0] and doc_in[1] >= real_in[1], (
        f"documented input {doc_in} does not bracket the assembled prompts {real_in}")
    assert doc_out[0] <= real_out[0] and doc_out[1] >= real_out[1], (
        f"documented output {doc_out} does not bracket the captured replies {real_out}")

def _expected(calls: int) -> tuple:
    row = _rows()["One provider call"]
    (in_lo, in_hi), (out_lo, out_hi) = _range(row.group("input")), _range(row.group("output"))
    in_rate, out_rate = price_per_mtok(MODEL)
    lo = calls * (in_lo * in_rate + out_lo * out_rate) / 1_000_000
    hi = calls * (in_hi * in_rate + out_hi * out_rate) / 1_000_000
    return round(lo, 2), round(hi, 2)

def test_every_documented_dollar_figure_is_arithmetic_over_the_rate_table():
    """The rule this file exists for: a dollar figure is derived or it does not ship. Each row is checked against *its own* stated call count, so a row whose total does not follow from its own arithmetic is red -- which is what a hand-typed number looks like."""
    rows = _rows()
    assert len(rows) >= 4, f"the cost table lost rows -- found only {sorted(rows)}"
    for label, match in rows.items():
        calls = int(match.group("calls"))
        found = (float(match.group("lo")), float(match.group("hi")))
        assert found == _expected(calls), (
            f"row {label!r} claims {found} for {calls} call(s); the rate table gives "
            f"{_expected(calls)}")

def test_the_readme_states_a_cost_before_the_first_paid_command():
    """The README is read before a key is set, so it carries the per-call and full-session cost too, both derived. It used to assert a flat "under $1" ceiling -- a hand-typed claim that broke once the table's own input range widened (#404) -- so state the real derived figure instead."""
    text = README.read_text(encoding="utf-8")
    per_call = _expected(1)
    assert f"${per_call[0]:.2f}" in text and f"${per_call[1]:.2f}" in text, (
        f"the README does not state the per-call range {per_call}")
    session_row = _rows()["A complete session, end to end"]
    session = _expected(int(session_row.group("calls")))
    assert f"${session[0]:.2f}" in text and f"${session[1]:.2f}" in text, (
        f"the README does not state the full-session range {session}")
