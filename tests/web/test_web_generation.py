"""Requivo Web: one paid generation at a time and an honest wait (#50, #236, #203), a failed first analysis that
is not a dead end (#207), and what a paid action cost, said out loud (#253)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS
from requivo.web.templating import TEMPLATES_DIR
from tests.web.conftest import (
    BRIEF_REPLY,
    HIGH_EXPLICIT,
    PAID,
    PAID_TOKENS,
    _make_session,
    analysed_via_post,
    create_via_post,
    engine_reply,
    run_js_harness,
)

_ANSWER = {"answers": "Exceptions go to HR.", "expected_revision": "1"}
_CONVERGED = engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT)


def _timeline(name: str, what: str, issue: str) -> dict[str, dict]:
    return {row["at"]: row for row in run_js_harness(name, what, issue)}


# ── one generation at a time (#50) ────────────────────────────────────────────


def test_one_generation_at_a_time_is_the_pages_rule_not_the_forms():
    """Mutual exclusion is a property of the page, not of a form (#50)."""
    t = _timeline("busy_harness", "the page-wide busy rule", "#50")
    assert t["initial"]["disabled"] == [False, False, False]
    assert all(t["one in flight"]["disabled"]), (   # must fire
        "one request in flight has to mute every submit button on the page, not only the one clicked")
    assert t["one in flight"]["busy"] is True, "the page has to say it is working, not only look it"
    assert all(t["two in flight"]["disabled"])
    assert all(t["first finished, second still running"]["disabled"]), "that needs a count, not a flag"
    assert not any(t["both finished"]["disabled"]) and t["both finished"]["busy"] is False
    # A swap replaces the region mid-flight; the incoming markup carries no disabled attribute.
    assert not any(t["swapped in, before afterSwap"]["disabled"]), "precondition: swapped-in buttons arrive enabled"
    assert all(t["swapped in, after afterSwap"]["disabled"]), "htmx:afterSwap has to re-assert the busy state"
    assert not any(t["after the swap, request finished"]["disabled"])
    # bfcache: the shipped asset left a button disabled forever here.
    assert all(t["in flight before pageshow"]["disabled"])
    assert not any(t["after pageshow"]["disabled"]), "a cached page must not come back with its buttons muted"


def test_the_generator_forms_all_target_one_region_which_is_why_the_rule_is_page_wide(client, monkeypatch):
    """The server-side half of #50: every generator form posts to one region, so the rule cannot live in a form."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # the toolbar only shows with a provider
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    page = client.get("/sessions/leave-approval").text
    targets = re.findall(r'hx-post="/sessions/leave-approval/artifacts/[^"]+"[^>]*?hx-target="([^"]+)"', page, re.S)
    assert len(targets) > 1, "expected several generator forms on the page, found: " + repr(targets)
    assert set(targets) == {"#artifacts-region"}


def test_every_rendered_button_is_reachable_by_the_page_wide_busy_rule(client, monkeypatch):
    """The busy rule selects `button[type="submit"]`, so a button without that attribute escapes it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    for path in ("/", "/sessions/leave-approval"):
        opening_tags = re.findall(r"<button\b[^>]*>", client.get(path).text)
        assert opening_tags, f"expected buttons on {path}, found none — this assertion saw nothing"
        for tag in opening_tags:
            assert 'type="submit"' in tag, f'{path} renders a button the busy rule cannot reach: {tag}'


# ── an honest wait (#236) ────────────────────────────────────────────────────


def test_a_long_call_says_so_after_ten_seconds_rather_than_looking_stuck():
    """A wait that outlives its own copy has to keep speaking (#236)."""
    t = _timeline("elapsed_harness", "the elapsed-time signal", "#236")
    started, eleven = t["request started"]["text"], t["eleven seconds in"]["text"]
    assert t["nine seconds in"]["text"] == started, "the status text moved before the wait needed explaining"
    assert eleven != started, "past ten seconds the page still said what a hung page also says"
    assert any("11s" in line for line in eleven), "the elapsed seconds have to be visible, got: " + repr(eleven)
    assert any("20s" in line for line in t["twenty seconds in"]["text"]), "the signal has to keep moving"
    assert t["request finished"]["text"] == started         # a finished turn does not leave "still working"
    assert t["request finished"]["liveTimers"] == 0, "the clock kept ticking after the call finished"
    assert t["second request, three seconds in"]["text"] == started   # a second turn counts from zero
    assert t["after pageshow"]["liveTimers"] == 0, "a cached page must not keep a timer against a finished request"


def test_no_provider_backed_button_still_promises_a_few_seconds():
    """The copy and the measurement have to agree (#236)."""
    offenders = [p.name for p in sorted(TEMPLATES_DIR.rglob("*.html")) if "a few seconds" in p.read_text(encoding="utf-8")]
    assert offenders == [], "these templates still promise 'a few seconds' for a seconds-to-minutes call: " + repr(offenders)


def test_the_no_js_path_still_states_how_long_it_will_take(client, monkeypatch):
    """The elapsed counter is an enhancement; the honest static copy is the floor (#236)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert "Usually under a minute" in client.get("/").text, "the create form states no duration at all"
    create_via_post(client, slug="later", provider="create_only")
    assert "Usually under a minute" in client.get("/sessions/later").text, "the deferred page makes the same promise"


# ── error responses reach the page (#203, #320) ──────────────────────────────


def test_error_responses_are_swapped_into_the_page_rather_than_dropped():
    """Every 4xx/5xx fragment this app builds was invisible in a real browser (#203); a new request clears it (#320)."""
    out = run_js_harness("error_swap_harness", "the error-swap opt-in", "#203")
    d = {row["status"]: row for row in out["decisions"]}
    # must fire: the defect, still present in the vendored library, is that htmx drops all of these.
    assert not any(d[s]["htmxWouldSwap"] for s in (400, 403, 409, 413, 500, 502)), "the harness no longer reproduces htmx's gate"
    for status in (400, 403, 409, 413, 500, 502):
        assert d[status]["swapped"] is True, f"a {status} response is still dropped"
    assert all(d[s]["isError"] is False for s in (409, 413, 502)), "a handled, rendered response should not log as uncaught"
    assert d[200]["swapped"] is True, "the opt-in broke ordinary successful swaps"
    assert d[204]["swapped"] is False, "the opt-in must not reach below 400 and swap an empty 204 body"
    assert out["flashClearedOnNewRequest"] is True, "a resolved failure goes on being displayed as a current one"


def test_an_error_fragment_retargets_but_a_full_region_keeps_its_own_target(client, with_provider):
    """The server half of #203: where a swapped 4xx/5xx lands decides whether the fix helps or repeats #30."""
    with_provider(engine_reply())   # one reply: the conflict below is only reached *after* the call (#205)
    slug = _make_session()
    oversized = client.post(f"/sessions/{slug}/answers",
                            data={"answers": "x" * (MAX_ANSWERS_CHARS + 1), "expected_revision": "1"})
    assert oversized.status_code == 413
    assert "HX-Retarget" not in oversized.headers, "the full-region refusal was retargeted, so #30's text is lost"
    assert "<textarea" in oversized.text and "x" * 300 in oversized.text
    conflict = client.post(f"/sessions/{slug}/answers", data={"answers": "The HR lead approves.", "expected_revision": "0"})
    assert conflict.status_code == 409
    assert conflict.headers["HX-Retarget"] == "#flash", "the conflict notice would swap over #session-body (#30 again)"
    assert conflict.headers["HX-Reswap"] == "innerHTML" and "notice danger" in conflict.text
    for path in ("/", f"/sessions/{slug}"):                 # the promise is about the document, not the response
        assert 'id="flash"' in client.get(path).text, f"{path} has no flash region to retarget into"


# ── a failed first analysis is not a dead end (#207) ─────────────────────────


@pytest.fixture
def failing_analysis(monkeypatch):
    """A provider that claims the session fine and then fails the paid call (#207)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def boom(self, slug, *, surface="discover"):
        raise EngineError("Anthropic API unavailable (529).")
    monkeypatch.setattr(DiscoveryService, "run_discovery", boom)


def _first_analysis(client, door: str):
    """The two doors onto a first analysis: `POST /sessions` with a provider, or `create_only` now and `/discover` later."""
    if door == "create":
        return client.post("/sessions", data={"request_text": "A leave approval system.", "provider": "anthropic"},
                           follow_redirects=True)
    slug = SessionService().create_session("A leave approval system.", slug="leave").slug
    return client.post(f"/sessions/{slug}/discover", follow_redirects=True)


def test_a_failed_first_analysis_lands_on_the_session_that_was_saved(client, with_provider, failing_analysis):
    """`start()` claims the session before the provider call, deliberately, so a refusal costs nothing (#207)."""
    with_provider()
    r = _first_analysis(client, "create")
    assert r.status_code == 200, "a failed first analysis still dead-ends on an error page"
    assert "Your request was saved" in r.text
    assert "Anthropic API unavailable" in r.text, "the cause was dropped, so the reader cannot act"
    assert "A leave approval system." in r.text, "the page does not show the request it saved"
    assert "Analyse request" in r.text, "the retry button the whole fix rests on is not on the page"
    assert [m.current_revision for m in SessionService().list_sessions()] == [0]


@pytest.mark.parametrize("door, failure", [("create", "retry-exhausted"), ("discover", "retry-exhausted"),
                                           ("discover", "transport")])
def test_the_other_failure_shapes_land_on_the_saved_session_too(client, with_provider, monkeypatch, door, failure):
    """`run_discovery`'s call fails two ways, through two doors; each lands on the retry page (#207)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    if failure == "transport":
        monkeypatch.setattr(DiscoveryService, "run_discovery",
                            lambda self, slug, *, surface="discover": (_ for _ in ()).throw(EngineError("boom (529).")))
        with_provider()
    else:
        with_provider("not json", "not json", "not json")
    r = _first_analysis(client, door)
    assert r.status_code == 200, "a failed first analysis dead-ends on an error page"
    assert "Your request was saved" in r.text and "Analyse request" in r.text
    assert "A leave approval system." in r.text
    assert [m.current_revision for m in SessionService().list_sessions()] == [0]


def test_a_retry_exhausted_analysis_carries_the_full_saved_reply_path_on_the_web_surface(client, with_provider,
                                                                                         monkeypatch, tmp_path):
    """#362: #283 appends the saved-reply path to `ProviderOutputError.message`'s tail, past the notice's 300-char cap."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    debug = tmp_path / ".requivo" / "debug"
    # Valid JSON with extra fields drives a long pydantic `ValidationError`; "not json" the shortest cause.
    long_reply = json.dumps({"model": {}, "summary": {}, **{f"extra_field_{i}": "x" * 10 for i in range(7)}})
    for request, reply in (("A leave approval system.", long_reply), ("A different request entirely.", "not json")):
        with_provider(reply, reply, reply)
        r = client.post("/sessions", data={"request_text": request, "provider": "anthropic"}, follow_redirects=True)
        assert r.status_code == 200
        saved = list(debug.glob("*.txt"))
        assert len(saved) == 1, "the give-up exit must have written exactly one debug file"
        path = str(saved[0])
        assert r.text.count(path) == 1, "the full path must appear exactly once, not as a truncated fragment too"
        assert r.text.count(Path(path).name[:20]) == 1, "no orphaned prefix of the filename survives mid-path truncation"
        assert r.text.count("was saved to") == 1, "the connector clause appears once, from the path sentence"
        saved[0].unlink()  # isolate the second POST to its own single debug file


def test_the_revision_zero_gate_still_holds_after_a_failed_analysis(client, with_provider, monkeypatch):
    """The must-fire half: recovering from the failure must not have cost the gate (#207)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    real = DiscoveryService.run_discovery
    monkeypatch.setattr(DiscoveryService, "run_discovery",
                        lambda self, slug, *, surface="discover": (_ for _ in ()).throw(EngineError("boom (529).")))
    with_provider(engine_reply())
    client.post("/sessions", data={"request_text": "A leave approval system.", "provider": "anthropic"}, follow_redirects=True)
    slug = SessionService().list_sessions()[0].slug
    monkeypatch.setattr(DiscoveryService, "run_discovery", real)
    r = client.post(f"/sessions/{slug}/discover", follow_redirects=True)
    assert r.status_code == 200
    assert SessionService().meta(slug).current_revision == 1, "the retry after a failed analysis did not land"


# ── what a paid action reports (#253) ────────────────────────────────────────


@pytest.mark.parametrize("action, reply", [("answers", _CONVERGED), ("artifacts/brief", BRIEF_REPLY)])
def test_a_paid_action_says_what_it_spent(client, with_provider, action, reply):
    """The fragment the reader lands on after a paid turn or generation states the tokens and the estimate."""
    analysed_via_post(client, with_provider, reply)
    r = client.post(f"/sessions/leave-approval/{action}", data=_ANSWER)
    assert r.status_code == 200
    assert PAID_TOKENS in r.text, "the action reported no token count"
    assert "estimate" in r.text and "$" in r.text, "a cost printed without the word estimate reads as a bill"


def test_an_offline_page_reports_no_spend(client, with_provider):
    """Reading a session costs nothing, so it says nothing."""
    analysed_via_post(client, with_provider)
    for path in ("/", "/sessions/leave-approval"):
        page = client.get(path).text
        assert "estimate" not in page and PAID_TOKENS not in page, f"{path} reported a spend for a page that made no call"


def test_a_call_the_provider_reported_no_usage_for_says_nothing_rather_than_zero(client, with_provider):
    """Zero tokens is not a measurement (#253)."""
    analysed_via_post(client, with_provider, _CONVERGED, spend=None)
    r = client.post("/sessions/leave-approval/answers", data=_ANSWER)
    assert r.status_code == 200
    assert "estimate" not in r.text, "a call with no reported usage was rendered as a $0.000 turn"


def test_an_unpriced_call_says_so_rather_than_guessing(client, with_provider, monkeypatch):
    """No price on file is an answer; a guessed number is not (invariant 6, and `cost_usd`'s own rule)."""
    monkeypatch.setenv("MODEL", "claude-something-nobody-priced")
    analysed_via_post(client, with_provider, _CONVERGED)
    r = client.post("/sessions/leave-approval/answers", data=_ANSWER)
    assert r.status_code == 200
    assert PAID_TOKENS in r.text, "tokens are exact even when the price is unknown"
    assert "no price on file" in r.text and "est. ~$" not in r.text


def test_a_failed_paid_call_still_records_what_it_spent(client, with_provider, caplog):
    """A call that failed is still billed, and the reader still gets the error page (#253)."""
    analysed_via_post(client, with_provider, "not json", "not json", "not json")   # three attempts, all billed
    with caplog.at_level(logging.INFO, logger="requivo.web"):
        r = client.post("/sessions/leave-approval/answers", data=_ANSWER)
    assert r.status_code >= 400, "the failure still has to reach the reader as an error"
    logged = [rec.getMessage() for rec in caplog.records]
    assert any("web-answer" in line for line in logged), "a paid turn that failed was not recorded: " + repr(logged)


@pytest.mark.parametrize("door", ["create", "deferred"])
def test_a_first_analysis_lands_on_a_page_showing_what_it_spent(client, with_provider, door):
    """Both doors onto a first analysis answer 303 with no body of their own; the figure survives to the GET (#253)."""
    if door == "deferred":
        with_provider()
        create_via_post(client, provider="create_only")
        with_provider(engine_reply(problem=HIGH_EXPLICIT), spend=PAID)
        r = client.post("/sessions/leave-approval/discover", follow_redirects=False)
    else:
        with_provider(engine_reply(problem=HIGH_EXPLICIT), spend=PAID)
        r = create_via_post(client)
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert PAID_TOKENS in page and "estimate" in page, "the figure did not survive the redirect"


def test_a_failed_first_analysis_still_shows_the_spend_it_recorded(client, with_provider, monkeypatch):
    """A call that fails after spending tokens still surfaces its recorded spend (#253)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with_provider("not json", "not json", "not json", spend=PAID)
    r = client.post("/sessions", data={"request_text": "A leave approval system", "provider": "anthropic"},
                    follow_redirects=True)
    assert r.status_code == 200 and "Your request was saved" in r.text
    assert "37,200" in r.text, "a paid, failed turn recorded a spend but the retry page shows none"


def test_reloading_the_landing_page_does_not_repeat_the_spend_line(client, with_provider):
    """Read-once: the page the redirect landed on shows the figure once, and a session nobody just paid for shows none."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT), spend=PAID)
    create_via_post(client)
    assert PAID_TOKENS in client.get("/sessions/leave-approval").text   # must fire: shown once
    reload = client.get("/sessions/leave-approval").text
    assert PAID_TOKENS not in reload and "estimate" not in reload, "a reload repeated a spend already shown once"
