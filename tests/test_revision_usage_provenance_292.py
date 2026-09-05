"""#292: stamp per-call token usage into revision provenance, so a session's cost is answerable.

Driven directly against `DiscoveryService`/`SessionService` with a stub `ReasoningProvider` that
records its own spend into the active `requivo.usage` ledger, the same way a real provider's
`_complete()` does -- no CLI, no web, no real network.
"""

from __future__ import annotations

import pytest
from _fakes import out, slot

from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.usage import CallRecord, record_call, track_usage


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))


class _SpendingProvider:
    """A stub `ReasoningProvider` whose `analyze()` records one `CallRecord` against whatever ledger
    is active -- exactly what `providers/anthropic/completion.py::_record` does for a real call."""

    name = "stub"

    def __init__(self, *records: CallRecord):
        self._records = list(records)

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False):
        record_call(self._records.pop(0))
        return out({"problem": slot(80, "explicit", "high")})

    def generate(self, *a, **k):  # pragma: no cover - unused here
        raise NotImplementedError

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None):
        return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}


def test_a_provider_backed_apply_stamps_token_and_rate_provenance_onto_its_revision():
    """The span `_usage_since` measures is "however many calls this operation made", not "one call".

    More than one can land in one span if a caller opens one around several, and such a span sums
    the tokens, because the revision it produced would embody all of them. Every call site today
    closes its span around exactly one provider call -- #467 split `start(..., finalize=True)`'s
    `analyze` and its brief's own `generate` into two applies with two spans, and was the last
    caller that spanned more than one.

    The rate is stamped only when every call in the span agrees on it. The ordinary case is one
    provider, one model, one price table for the whole span; a genuine disagreement is refused
    rather than averaged, which is the same argument `UsageLedger`'s own docstring makes for cost.
    """
    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    provider = _SpendingProvider(CallRecord(
        model="stub-model", input_tokens=1000, output_tokens=200,
        cache_read_tokens=50, cache_write_tokens=10,
        rate_per_mtok=(2.0, 10.0), priced_as_of="2026-08-29"))
    disco = DiscoveryService(provider=provider, sessions=sessions)

    with track_usage():
        disco.run_discovery(slug, surface="test")

    rec = sessions.repo.read_meta(slug).revisions[-1]
    assert rec.usage_input_tokens == 1000
    assert rec.usage_output_tokens == 200
    assert rec.usage_cache_read_tokens == 50
    assert rec.usage_cache_write_tokens == 10
    assert tuple(rec.usage_rate_per_mtok) == (2.0, 10.0)
    assert rec.usage_priced_as_of == "2026-08-29"


def test_a_deterministic_apply_carries_no_usage_provenance():
    """Must-fire control: a `model apply` that never touches a provider -- the shape a Claude Code
    turn or a hand-authored proposal takes -- must not read as having spent $0.00 (invariant 6)."""
    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    sessions.update_model(slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json())

    rec = sessions.repo.read_meta(slug).revisions[-1]
    assert rec.usage_input_tokens is None
    assert rec.usage_output_tokens is None
    assert rec.usage_cache_read_tokens is None
    assert rec.usage_cache_write_tokens is None
    assert rec.usage_rate_per_mtok is None
    assert rec.usage_priced_as_of is None


def test_a_provider_call_made_with_no_active_ledger_still_leaves_usage_absent():
    """The offline test suite's ordinary shape: a provider call made with no `track_usage()` scope
    open at all. `current_ledger()` is `None`, and that has to read the same as "nothing to report",
    not as a call that spent zero tokens."""
    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    provider = _SpendingProvider(CallRecord(model="stub-model", input_tokens=1000, output_tokens=200))
    disco = DiscoveryService(provider=provider, sessions=sessions)

    disco.run_discovery(slug, surface="test")  # no track_usage() scope open

    rec = sessions.repo.read_meta(slug).revisions[-1]
    assert rec.usage_input_tokens is None
    assert rec.usage_rate_per_mtok is None


def test_a_revision_record_with_no_usage_keys_round_trips_unchanged():
    """An old session.json, written before #292, carries no usage_* keys at all. `RevisionRecord`
    must load it without complaint and default every usage field to absent, not zero."""
    from requivo.core.persistence import RevisionRecord

    old_json = (
        '{"revision": 1, "created_at": "2026-01-01T00:00:00Z", "provider": "anthropic", '
        '"model_name": "claude-sonnet-5", "surface": "cli-discover", "model_hash": "sha256:abc"}'
    )
    rec = RevisionRecord.model_validate_json(old_json)
    assert rec.usage_input_tokens is None
    assert rec.usage_rate_per_mtok is None
    # Re-serializes without inventing a zero anywhere a real value was never known.
    again = RevisionRecord.model_validate_json(rec.model_dump_json())
    assert again.usage_input_tokens is None


def test_render_session_cost_is_silent_when_no_revision_carries_usage(capsys):
    """Must-fire control for the renderer itself: a session applied entirely through a deterministic
    path (or by Claude Code) must print nothing -- never $0.00 (invariant 6)."""
    from requivo.render.terminal import render_session_cost

    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    sessions.update_model(slug, out({"problem": slot(80, "explicit", "high")}).model_dump_json())
    revisions = sessions.repo.read_meta(slug).revisions

    render_session_cost(revisions)

    assert capsys.readouterr().out == ""


def test_render_session_cost_sums_priced_revisions():
    from requivo.render.terminal import render_session_cost

    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    provider = _SpendingProvider(
        CallRecord(model="stub-model", input_tokens=1000, output_tokens=200,
                  rate_per_mtok=(2.0, 10.0), priced_as_of="2026-08-29"))
    disco = DiscoveryService(provider=provider, sessions=sessions)
    with track_usage():
        disco.run_discovery(slug, surface="test")

    revisions = sessions.repo.read_meta(slug).revisions
    render_session_cost(revisions)  # smoke: must not raise, prints the cumulative figure

    assert revisions[-1].usage_input_tokens == 1000


def test_render_session_cost_reads_its_arithmetic_from_usage_py_and_nowhere_else(capsys):
    """#389: `render_session_cost` used to re-implement `UsageLedger.cost_usd()` locally -- the
    same 0.1x cache-read and 1.25x cache-write multipliers, copied rather than called -- while
    `usage.py`'s own module docstring says cost is "arithmetic here and nowhere else". A mutation
    control proved the copy was unguarded: changing the cache-read multiplier in
    `render_session_cost` alone, leaving `usage.py` untouched, produced zero added failures across
    the full suite (`1 failed, 1392 passed` before and after -- the one failure a pre-existing,
    unrelated artifact of running outside a git checkout).

    This is the guard that closes that gap. It exercises every rate tier at once -- plain input,
    a cache read, a cache write, output -- and asserts the exact printed dollar figure, computed
    independently here, so a multiplier drifting in only one of the two places shows up as a wrong
    number rather than as a missing test. Round token counts (1,000,000 each) keep the arithmetic
    exact rather than merely close, so `.3f` rounding cannot paper over a divergence."""
    from requivo.core.persistence import RevisionRecord
    from requivo.render.terminal import render_session_cost

    revisions = [RevisionRecord(
        revision=1, created_at="2026-01-01T00:00:00Z",
        usage_input_tokens=1_000_000, usage_output_tokens=1_000_000,
        usage_cache_read_tokens=1_000_000, usage_cache_write_tokens=1_000_000,
        usage_rate_per_mtok=(2.0, 10.0), usage_priced_as_of="2026-01-01",
    )]

    render_session_cost(revisions)

    # (1_000_000 * 2.0)              input, full price
    # + (1_000_000 * 2.0 * 0.1)      cache read,  ~0.1x the input rate
    # + (1_000_000 * 2.0 * 1.25)     cache write, ~1.25x the input rate
    # + (1_000_000 * 10.0)           output, full price
    # = 14,700,000 / 1_000_000 == 14.700
    assert "~$14.700" in capsys.readouterr().out


def test_render_session_cost_does_not_stamp_a_dangling_separator_for_an_empty_priced_as_of(capsys):
    """Found in review (#388/#389): the old code built its `as_of` list with a *truthy* filter --
    `if r.usage_priced_as_of and ...` -- which silently dropped an empty-string date. Routing the
    same field through `UsageLedger.priced_as_of` (#389's fix) filters on `is not None` instead,
    which is the right rule for `usage.py`'s own contract (`None` means "unpriced", not "priced with
    an empty date") but is a different rule than the one this renderer used to apply -- so a revision
    whose `usage_priced_as_of` is `""` (never written by any real provider path, but not excluded by
    `RevisionRecord`'s schema either, and reachable through `session import`) used to be silently
    skipped and now leaves a dangling `" · "` with nothing after it: `rates as of 2026-01-01 · `.
    Normalizing an empty string to `None` on the way into the scratch `CallRecord` restores the old
    renderer's behaviour without reintroducing local arithmetic."""
    from requivo.core.persistence import RevisionRecord
    from requivo.render.terminal import render_session_cost

    dated = RevisionRecord(
        revision=1, created_at="2026-01-01T00:00:00Z",
        usage_input_tokens=1000, usage_output_tokens=200,
        usage_cache_read_tokens=0, usage_cache_write_tokens=0,
        usage_rate_per_mtok=(2.0, 10.0), usage_priced_as_of="2026-01-01",
    )
    undated = RevisionRecord(
        revision=2, created_at="2026-01-02T00:00:00Z", previous_revision=1,
        usage_input_tokens=500, usage_output_tokens=100,
        usage_cache_read_tokens=0, usage_cache_write_tokens=0,
        usage_rate_per_mtok=(2.0, 10.0), usage_priced_as_of="",
    )

    render_session_cost([dated, undated])

    text = capsys.readouterr().out
    assert "rates as of 2026-01-01" in text
    assert "· " not in text and " ·" not in text, text


def test_requivo_status_prints_the_cumulative_cost_line():
    """The CLI end -- `requivo status` shows the line once a revision carries usage."""
    from contextlib import redirect_stdout
    from io import StringIO

    from requivo.cli import app

    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    provider = _SpendingProvider(
        CallRecord(model="stub-model", input_tokens=1000, output_tokens=200,
                  rate_per_mtok=(2.0, 10.0), priced_as_of="2026-08-29"))
    disco = DiscoveryService(provider=provider, sessions=sessions)
    with track_usage():
        disco.run_discovery(slug, surface="test")

    buf = StringIO()
    with redirect_stdout(buf):
        app(["status", slug])
    printed = buf.getvalue()
    assert "SESSION COST" in printed
    assert "1,000" in printed or "1000" in printed
