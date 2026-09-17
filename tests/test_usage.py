"""API usage tracking: what a call cost, and what the terminal says about it (#72)."""
import io
import logging
from contextlib import redirect_stdout
from datetime import date as _date

import anthropic
import httpx
import pytest
from _fakes import _ENGINE_REPLY, FakeClient, _FakeBlock, _model_in_out, _run_app

from requivo.core.errors import RequivoError
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


def priced(model: str, on: _date = _date(2026, 9, 1), **kw) -> CallRecord:
    """A record carrying the rate it was billed at, the way a provider files one (#167)."""
    return price_call(CallRecord(model=model, **kw), on)


@pytest.fixture
def launch_priced_model(monkeypatch):
    """A model on an intro rate that lapses, held in the tables for the length of one test (#254)."""
    name = "fixture-model-on-launch-pricing"
    monkeypatch.setitem(_PRICE_PER_MTOK, name, (3.00, 15.00))
    monkeypatch.setitem(_LAUNCH_PRICE_PER_MTOK, name, (2.00, 10.00, "2026-08-31"))
    return name


@pytest.fixture(autouse=True)
def _isolate_workspace(workspace):
    """conftest's `workspace` fixture, applied automatically to every test in this module."""


# ── Tier 3: API usage tracking (tokens / cost / latency) ──────────────────────
# Tokens are ground truth from the response; cost is a labelled estimate.


class _FakeUsage:
    def __init__(self, i, o, cache_read=0, cache_write=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write


class _UsageResponse:
    def __init__(self, text, usage):
        self.content = [_FakeBlock(text)]
        self.usage = usage


class UsageFakeClient:
    """Like FakeClient but every reply carries a usage object, so `_complete` records real numbers."""

    def __init__(self, text, usage):
        self._text, self._usage = text, usage
        self.messages = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _UsageResponse(self._text, self._usage)


def test_usage_ledger_totals_and_cost():
    ledger = UsageLedger()
    ledger.record(priced("claude-sonnet-5", input_tokens=1_000_000))
    ledger.record(priced("claude-sonnet-5", output_tokens=1_000_000))
    assert ledger.input_tokens == 1_000_000 and ledger.output_tokens == 1_000_000
    # Standard rates: 1M input @ $2 + 1M output @ $10 = $12.00.
    assert abs(ledger.cost_usd() - 12.0) < 1e-6


def test_launch_pricing_applies_until_it_lapses(launch_priced_model):
    """A dated table with no expiry gets exactly one of these two days right (#254)."""
    assert price_per_mtok(launch_priced_model, _date(2026, 8, 31)) == (2.00, 10.00)
    assert price_per_mtok(launch_priced_model, _date(2026, 9, 1)) == (3.00, 15.00)


def test_no_launch_rate_outlives_the_day_it_lapses():
    """The other half: nothing in the shipped table is past its own end date."""
    today = _date.today()
    for model, (_in, _out, until) in _LAUNCH_PRICE_PER_MTOK.items():
        assert _date.fromisoformat(until) >= today, (
            f"{model}'s launch rate lapsed on {until}; confirm the standard rate against Anthropic's "
            f"pricing page, fold it into _PRICE_PER_MTOK and drop this row"
        )


def test_a_call_is_priced_at_the_rate_in_force_when_it_was_made(launch_priced_model):
    """The behaviour the stamp buys, and the reason the ledger no longer takes an `on` (#167)."""
    launch = UsageLedger()
    launch.record(priced(launch_priced_model, _date(2026, 8, 31),
                         input_tokens=1_000_000, output_tokens=1_000_000))
    standard = UsageLedger()
    standard.record(priced(launch_priced_model, _date(2026, 9, 1),
                           input_tokens=1_000_000, output_tokens=1_000_000))
    assert abs(launch.cost_usd() - 12.0) < 1e-6     # launch: 2 + 10
    assert abs(standard.cost_usd() - 18.0) < 1e-6   # standard: 3 + 15


def test_usage_ledger_cost_counts_cache_tiers():
    ledger = UsageLedger()
    # cache read ≈ 0.1× input rate, cache write ≈ 1.25× input rate (Sonnet standard input $2/Mtok)
    ledger.record(priced("claude-sonnet-5", cache_read_tokens=1_000_000, cache_write_tokens=1_000_000))
    assert abs(ledger.cost_usd() - (0.2 + 2.5)) < 1e-6


def test_usage_ledger_cost_is_none_for_unpriced_model():
    ledger = UsageLedger()
    rec = priced("some-future-model", input_tokens=10)
    assert rec.rate_per_mtok is None and rec.priced_as_of is None, (
        "an unknown model must leave both fields absent: a rate with no table date, or a date with "
        "no rate, is provenance nobody measured (invariant 6)"
    )
    ledger.record(rec)
    assert ledger.cost_usd() is None
    assert ledger.priced_as_of == []


def test_render_usage_shows_tokens_cache_latency_and_estimate():
    ledger = UsageLedger()
    ledger.record(priced("claude-sonnet-5", input_tokens=1000, output_tokens=200,
                         cache_read_tokens=500, latency_ms=1500))
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(ledger)
    text = buf.getvalue()
    assert "API USAGE" in text
    assert "1,500 tokens" in text          # processed = input + cache_read = 1000 + 500
    assert "500 served from cache" in text
    assert "1.5 s" in text
    assert "Est. cost" in text and "estimate" in text
    # The rate date comes off the ledger's records, not off a constant the renderer imported (#167).
    assert f"rates as of {PRICING_AS_OF}" in text


def test_render_usage_omits_the_rate_date_it_was_not_given():
    """The third state, and the reason it is a state rather than a fallback."""
    ledger = UsageLedger()
    ledger.record(CallRecord(model="claude-sonnet-5", input_tokens=1_000_000,
                             rate_per_mtok=(3.0, 15.0)))
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(ledger)
    text = buf.getvalue()
    assert "~$3.000" in text and "estimate)" in text   # the cost is still stated
    assert "rates as of" not in text                   # the provenance is not invented


def test_render_usage_silent_without_tokens():
    # No call, or usage absent (offline fake) → nothing printed.
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(UsageLedger())                                   # empty
        render_usage(UsageLedger(calls=[CallRecord(model="claude-sonnet-5")]))  # a call, zero tokens
    assert buf.getvalue() == ""


def test_render_usage_flags_unpriced_model_but_keeps_tokens():
    ledger = UsageLedger()
    ledger.record(priced("some-future-model", input_tokens=1000, output_tokens=200))
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(ledger)
    text = buf.getvalue()
    assert "1,200 tokens" in text or "1,000 tokens" in text  # tokens still reported
    assert "no price on file" in text                         # cost honestly withheld


def test_complete_records_usage_into_the_active_ledger():
    # run() → _complete records one CallRecord, summing the response's usage fields.
    client = UsageFakeClient(_ENGINE_REPLY, _FakeUsage(1000, 200, cache_read=500))
    with track_usage() as ledger:
        run(client, [{"role": "user", "content": "leave approval"}])
    assert len(ledger.calls) == 1
    assert ledger.input_tokens == 1000 and ledger.output_tokens == 200
    assert ledger.cache_read_tokens == 500
    assert ledger.cost_usd() is not None
    # The provider stamps the rate as it files the call — the ledger holds no table to look one up in.
    assert ledger.priced_as_of == [PRICING_AS_OF]


# ── A failed call is still billed, on every exit ──────────────────────────────
#
# The constraint #74 named as the one most likely to break quietly in the split.
#
# The positive control is the success arm above.


class _RaisingClient:
    """Raises the SDK's transport error — the exit `_stop()` reaches from the `except APIError` arm."""

    def __init__(self):
        self.messages = self
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        raise anthropic.APIConnectionError(
            message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))


class _TruncatedUsageClient:
    """A reply flagged as cut off at the ceiling whose JSON does not parse — the truncation exit."""

    def __init__(self, usage):
        self._usage = usage
        self.messages = self

    def create(self, **kwargs):
        usage = self._usage

        class _Resp:
            stop_reason = "max_tokens"
            content = [_FakeBlock('{"model": {"problem":')]
        _Resp.usage = usage
        return _Resp()


class _NonconformingClient:
    """Valid JSON that never satisfies the contract."""

    def __init__(self, usage):
        self._usage = usage
        self.messages = self
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _UsageResponse('{"not": "an engine output"}', self._usage)


def test_a_failed_call_is_still_recorded_on_every_exit():
    """All three failure exits file the spend, and each is asserted on its numbers."""
    # 1. Transport failure.
    with track_usage() as ledger:
        with pytest.raises(EngineError):
            run(_RaisingClient(), [{"role": "user", "content": "x"}])
    assert len(ledger.calls) == 1, "a transport failure was not recorded"
    assert ledger.calls[0].attempts == 1

    # 2. Truncation.
    with track_usage() as ledger:
        with pytest.raises(EngineError):
            run(_TruncatedUsageClient(_FakeUsage(900, 16000)), [{"role": "user", "content": "x"}])
    assert len(ledger.calls) == 1, "a truncated reply was not recorded"
    assert ledger.input_tokens == 900 and ledger.output_tokens == 16000
    assert ledger.cost_usd() is not None, "a recorded failure must still be priced"

    # 3. Retry give-up.
    client = _NonconformingClient(_FakeUsage(100, 50))
    with track_usage() as ledger:
        with pytest.raises(RequivoError):
            run(client, [{"role": "user", "content": "x"}])
    assert client.calls == 3, "the retry loop is not the shape this test thinks it is"
    assert len(ledger.calls) == 1, "the give-up exit did not record"
    assert ledger.calls[0].attempts == 3
    assert ledger.input_tokens == 300 and ledger.output_tokens == 150, (
        "a retry spends tokens too: the record sums every attempt, not just the last"
    )


def test_pc_status_reports_no_usage_offline():
    # An offline verb makes no call → no ledger records → no usage line.
    with _model_in_out("clitest-usage") as p:
        text = _run_app(["status", str(p)])
    assert "API USAGE" not in text


# ── #435: an `operation` field on CallRecord, additive only ──────────────────────
#
# The issue's acceptance criteria states the constraint in as many words.


def test_call_record_operation_defaults_to_none():
    """Every existing `CallRecord(...)` construction in this suite (see `priced()` above and every other call
    site grepped for this change) omits `operation`."""
    rec = CallRecord(model="claude-sonnet-5")
    assert rec.operation is None


def test_render_usage_is_unaffected_by_the_operation_field():
    """`render_usage()` prints ledger *totals* -- calls, tokens, latency, cost -- never a per-record field, so
    whether a `CallRecord` carries an `operation` or not must produce byte-identical output."""
    without = UsageLedger()
    without.record(priced("claude-sonnet-5", input_tokens=1000, output_tokens=200, latency_ms=1500))
    with_op = UsageLedger()
    with_op.record(priced("claude-sonnet-5", input_tokens=1000, output_tokens=200, latency_ms=1500,
                          operation="brief"))
    buf_without, buf_with = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_without):
        render_usage(without)
    with redirect_stdout(buf_with):
        render_usage(with_op)
    assert buf_without.getvalue() == buf_with.getvalue()


def test_run_stamps_the_analyze_operation_onto_the_call_record():
    """`run()` is the discovery turn -- `"analyze"` in `_OP_PROMPTS`'s own vocabulary."""
    client = FakeClient(_ENGINE_REPLY)
    with track_usage() as ledger:
        run(client, [{"role": "user", "content": "leave approval"}])
    assert len(ledger.calls) == 1
    assert ledger.calls[0].operation == "analyze"


# ── the optional `requivo.providers...` logger: completed / gave up (#435) ───────
#
# `completion.py`'s own `_complete()` is the one place a call's attempts and latency are actually known.


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


def test_a_completed_call_is_logged_at_debug_with_attempts_and_latency(_completion_handler):
    client = FakeClient(_ENGINE_REPLY)
    run(client, [{"role": "user", "content": "leave approval"}])
    messages = [r.getMessage() for r in _completion_handler]
    assert any("provider call completed" in m and "operation=analyze" in m and "attempts=1" in m
              for m in messages), messages


def test_a_call_that_gives_up_is_logged_as_a_warning(_completion_handler):
    client = _NonconformingClient(_FakeUsage(100, 50))
    with pytest.raises(RequivoError):
        run(client, [{"role": "user", "content": "x"}])
    warnings = [r for r in _completion_handler if r.levelno == logging.WARNING]
    assert any("provider call gave up" in r.getMessage() and "operation=analyze" in r.getMessage()
              and "attempts=3" in r.getMessage() for r in warnings), (
        [r.getMessage() for r in _completion_handler])
