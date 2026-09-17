"""API usage tracking: what a call cost, what the terminal says about it, and the SDK field names the
billing reads (#72, #167, #254, #294, #435)."""
import ast
import inspect
import io
import logging
from contextlib import redirect_stdout
from datetime import date as _date

import pytest
from _fakes import _ENGINE_REPLY, FakeClient, RaisingClient, Spend, _model_in_out, run_cli
from anthropic.types import Usage

from requivo.core.errors import RequivoError
from requivo.providers.anthropic import completion as _completion_module
from requivo.providers.anthropic import run
from requivo.providers.anthropic.pricing import (
    _LAUNCH_PRICE_PER_MTOK,
    _PRICE_PER_MTOK,
    PRICING_AS_OF,
    price_call,
    price_per_mtok,
)
from requivo.providers.errors import EngineError
from requivo.render.terminal import render_usage
from requivo.usage import CallRecord, UsageLedger, track_usage

pytestmark = pytest.mark.usefixtures("workspace")
_USER = [{"role": "user", "content": "leave approval"}]
_BAD = '{"not": "an engine output"}'


def priced(model: str, on: _date = _date(2026, 9, 1), **kw) -> CallRecord:
    """A record carrying the rate it was billed at, the way a provider files one (#167)."""
    return price_call(CallRecord(model=model, **kw), on)


def _rendered(ledger) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(ledger)
    return buf.getvalue()


class _MaxTokensClient(FakeClient):
    """`FakeClient`, every reply flagged as cut off at the token ceiling."""

    def create(self, **kwargs):
        reply = super().create(**kwargs)
        reply.stop_reason = "max_tokens"
        return reply


@pytest.fixture
def launch_priced_model(monkeypatch):
    """A model on an intro rate that lapses, held in the tables for the length of one test (#254)."""
    name = "fixture-model-on-launch-pricing"
    monkeypatch.setitem(_PRICE_PER_MTOK, name, (3.00, 15.00))
    monkeypatch.setitem(_LAUNCH_PRICE_PER_MTOK, name, (2.00, 10.00, "2026-08-31"))
    return name


# ── the ledger and the rate table ─────────────────────────────────────────────────


def test_usage_ledger_totals_and_cost():
    ledger = UsageLedger()
    ledger.record(priced("claude-sonnet-5", input_tokens=1_000_000))
    ledger.record(priced("claude-sonnet-5", output_tokens=1_000_000))
    assert ledger.input_tokens == 1_000_000 and ledger.output_tokens == 1_000_000
    assert ledger.cost_usd() == pytest.approx(12.0)  # 1M input @ $2 + 1M output @ $10


def test_usage_ledger_cost_counts_cache_tiers():
    ledger = UsageLedger()
    ledger.record(priced("claude-sonnet-5", cache_read_tokens=1_000_000, cache_write_tokens=1_000_000))
    assert ledger.cost_usd() == pytest.approx(0.2 + 2.5)  # read 0.1x, write 1.25x of $2


def test_launch_pricing_applies_until_it_lapses(launch_priced_model):
    """A dated table with no expiry gets exactly one of these two days right (#254)."""
    assert price_per_mtok(launch_priced_model, _date(2026, 8, 31)) == (2.00, 10.00)
    assert price_per_mtok(launch_priced_model, _date(2026, 9, 1)) == (3.00, 15.00)


def test_no_launch_rate_outlives_the_day_it_lapses():
    for model, (_in, _out, until) in _LAUNCH_PRICE_PER_MTOK.items():
        assert _date.fromisoformat(until) >= _date.today(), (
            f"{model}'s launch rate lapsed on {until}; fold the standard rate into _PRICE_PER_MTOK and drop this row")


def test_a_call_is_priced_at_the_rate_in_force_when_it_was_made(launch_priced_model):
    """The stamp is why the ledger no longer takes an `on` (#167)."""
    costs = []
    for on in (_date(2026, 8, 31), _date(2026, 9, 1)):
        ledger = UsageLedger()
        ledger.record(priced(launch_priced_model, on, input_tokens=1_000_000, output_tokens=1_000_000))
        costs.append(ledger.cost_usd())
    assert costs == pytest.approx([12.0, 18.0])  # launch 2 + 10, standard 3 + 15


def test_usage_ledger_cost_is_none_for_unpriced_model():
    """An unknown model leaves both rate and date absent: half a provenance is none (invariant 6)."""
    ledger = UsageLedger()
    rec = priced("some-future-model", input_tokens=10)
    assert rec.rate_per_mtok is None and rec.priced_as_of is None
    ledger.record(rec)
    assert ledger.cost_usd() is None and ledger.priced_as_of == []


def test_call_record_operation_defaults_to_none():
    """Every existing `CallRecord(...)` construction omits `operation` (#435, additive only)."""
    assert CallRecord(model="claude-sonnet-5").operation is None


# ── what the terminal says ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("record, present, absent", [
    (priced("claude-sonnet-5", input_tokens=1000, output_tokens=200, cache_read_tokens=500, latency_ms=1500),
     ("API USAGE", "1,500 tokens", "500 served from cache", "1.5 s", "Est. cost", "estimate", f"rates as of {PRICING_AS_OF}"), ()),
    (CallRecord(model="claude-sonnet-5", input_tokens=1_000_000, rate_per_mtok=(3.0, 15.0)),
     ("~$3.000", "estimate)"), ("rates as of",)),   # a rate with no date: the cost is stated, the provenance not invented
    (priced("some-future-model", input_tokens=1000, output_tokens=200), ("1,000 tokens", "no price on file"), ()),
], ids=["priced", "undated-rate", "unpriced"])
def test_render_usage_states_tokens_cache_latency_and_a_labelled_estimate(record, present, absent):
    ledger = UsageLedger()
    ledger.record(record)
    text = _rendered(ledger)
    assert all(s in text for s in present) and not any(s in text for s in absent), text


def test_render_usage_silent_without_tokens():
    assert _rendered(UsageLedger()) == "" == _rendered(UsageLedger(calls=[CallRecord(model="claude-sonnet-5")]))


def test_render_usage_is_unaffected_by_the_operation_field():
    """`render_usage()` prints totals, never a per-record field (#435)."""
    without, with_op = UsageLedger(), UsageLedger()
    without.record(priced("claude-sonnet-5", input_tokens=1000, output_tokens=200, latency_ms=1500))
    with_op.record(priced("claude-sonnet-5", input_tokens=1000, output_tokens=200, latency_ms=1500, operation="brief"))
    assert _rendered(without) == _rendered(with_op)


def test_pc_status_reports_no_usage_offline():
    with _model_in_out("clitest-usage") as p:
        assert "API USAGE" not in run_cli(["status", str(p)])


# ── `_complete` records every exit ──────────────────────────────────────────────────


def test_complete_records_usage_into_the_active_ledger():
    client = FakeClient(_ENGINE_REPLY, spend=Spend(1000, 200, cache_read_input_tokens=500))
    with track_usage() as ledger:
        run(client, _USER)
    assert len(ledger.calls) == 1 and ledger.calls[0].operation == "analyze"
    assert (ledger.input_tokens, ledger.output_tokens, ledger.cache_read_tokens) == (1000, 200, 500)
    assert ledger.cost_usd() is not None and ledger.priced_as_of == [PRICING_AS_OF]  # the provider stamps the rate


def test_a_failed_call_is_still_recorded_on_every_exit():
    """All three failure exits file the spend, each asserted on its numbers (#74)."""
    with track_usage() as ledger, pytest.raises(EngineError):
        run(RaisingClient(), _USER)
    assert len(ledger.calls) == 1 and ledger.calls[0].attempts == 1, "a transport failure was not recorded"

    with track_usage() as ledger, pytest.raises(EngineError):
        run(_MaxTokensClient('{"model": {"problem":', spend=Spend(900, 16000)), _USER)
    assert len(ledger.calls) == 1 and (ledger.input_tokens, ledger.output_tokens) == (900, 16000)
    assert ledger.cost_usd() is not None, "a recorded failure must still be priced"

    client = FakeClient(_BAD, _BAD, _BAD, spend=Spend(100, 50))
    with track_usage() as ledger, pytest.raises(RequivoError):
        run(client, _USER)
    assert len(client.calls) == 3 and len(ledger.calls) == 1 and ledger.calls[0].attempts == 3
    assert (ledger.input_tokens, ledger.output_tokens) == (300, 150), "the record sums every attempt"


@pytest.fixture
def _completion_handler():
    logger = logging.getLogger("requivo.providers.anthropic.completion")
    before = (list(logger.handlers), logger.level)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.handlers, logger.level = before


def test_a_completed_call_and_a_give_up_are_logged_with_attempts(_completion_handler):
    """`_complete()` is the one place a call's attempts and latency are known (#435)."""
    run(FakeClient(_ENGINE_REPLY), _USER)
    with pytest.raises(RequivoError):
        run(FakeClient(_BAD, _BAD, _BAD, spend=Spend(100, 50)), _USER)
    messages = [(r.levelno, r.getMessage()) for r in _completion_handler]
    assert any(lvl == logging.DEBUG and "provider call completed" in m and "operation=analyze" in m and "attempts=1" in m
               for lvl, m in messages), messages
    assert any(lvl == logging.WARNING and "provider call gave up" in m and "operation=analyze" in m and "attempts=3" in m
               for lvl, m in messages), messages


# ── #294: the SDK `Usage` field names `_complete` reads through `getattr(u, name, 0) or 0` ──────


def _usage_field_names_completion_reads() -> tuple[str, ...]:
    tree = ast.parse(inspect.getsource(_completion_module))
    return tuple(node.args[1].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                 and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id == "u"
                 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str))


def test_the_sdk_usage_object_still_has_every_field_completion_py_reads():
    """Red when a name `completion.py` reads is missing from the installed SDK; the extractor itself is the control."""
    names = _usage_field_names_completion_reads()
    assert names, 'found no getattr(u, "...", 0) calls in completion.py: the extractor no longer matches the read site'
    missing = [name for name in names if name not in Usage.model_fields]
    assert not missing, f"anthropic.types.Usage no longer defines {missing}; a rename zeroes that field's billing silently"
