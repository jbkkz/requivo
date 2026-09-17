"""Requivo Web: one paid generation at a time, and an honest wait while it runs (#555)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from requivo.web.templating import TEMPLATES_DIR
from tests.web.conftest import HIGH_EXPLICIT, _make_session

_BUSY_HARNESS = Path(__file__).parent / "busy_harness.js"
_ERROR_SWAP_HARNESS = Path(__file__).parent / "error_swap_harness.js"
_ELAPSED_HARNESS = Path(__file__).parent / "elapsed_harness.js"


def _busy_timeline() -> dict[str, dict]:
    """Execute the real `static/js/app.js` against a minimal DOM and return what it did (#50)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so the page-wide busy rule in static/js/app.js was NOT "
                    "asserted in this run — it is browser behaviour and nothing else in this suite "
                    "can see it (#50)")
    app_js = TEMPLATES_DIR.parent / "static" / "js" / "app.js"
    proc = subprocess.run([node, str(_BUSY_HARNESS), str(app_js)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, so nothing was observed:\n" + proc.stderr
    return {row["at"]: row for row in json.loads(proc.stdout)}


def test_one_generation_at_a_time_is_the_pages_rule_not_the_forms():
    """Mutual exclusion is a property of the page, not of a form (#50)."""
    t = _busy_timeline()

    assert t["initial"]["disabled"] == [False, False, False]

    # must fire.
    assert all(t["one in flight"]["disabled"]), (
        "one request in flight has to mute every submit button on the page, not only the one that was "
        "clicked — the sibling generator buttons are exactly what buys the duplicate call")
    assert t["one in flight"]["busy"] is True, "the page has to say it is working, not only look it"
    assert all(t["two in flight"]["disabled"])

    assert all(t["first finished, second still running"]["disabled"]), (
        "the first response must not hand the reader live buttons while a second call is still in "
        "flight — that needs a count, not a flag")
    assert not any(t["both finished"]["disabled"]), "the page has to come back when the work is done"
    assert t["both finished"]["busy"] is False

    # A swap replaces the region mid-flight; the incoming markup carries no disabled attribute.
    assert not any(t["swapped in, before afterSwap"]["disabled"]), (
        "precondition: swapped-in buttons really do arrive enabled, so the next assertion is repairing "
        "something rather than observing a no-op")
    assert all(t["swapped in, after afterSwap"]["disabled"]), (
        "htmx:afterSwap has to re-assert the busy state over markup the swap just brought in")
    assert not any(t["after the swap, request finished"]["disabled"])

    # bfcache: the shipped asset left a button disabled forever here.
    assert all(t["in flight before pageshow"]["disabled"])
    assert not any(t["after pageshow"]["disabled"]), (
        "returning to a cached page must not restore it with its buttons still muted")


def test_the_generator_forms_all_target_one_region_which_is_why_the_rule_is_page_wide(client,
                                                                                     monkeypatch):
    """The server-side half of #50, and the reason the rule cannot live in a form."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # the toolbar only shows with a provider
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    page = client.get("/sessions/leave-approval").text
    targets = re.findall(r'hx-post="/sessions/leave-approval/artifacts/[^"]+"[^>]*?'
                         r'hx-target="([^"]+)"', page, re.S)
    assert len(targets) > 1, "expected several generator forms on the page, found: " + repr(targets)
    assert set(targets) == {"#artifacts-region"}, (
        "every generator form posts to one region, so their responses collide — mutual exclusion has "
        "to be page-wide (#50)")


def test_every_rendered_button_is_reachable_by_the_page_wide_busy_rule(client, monkeypatch):
    """The busy rule selects `button[type="submit"]`, so a button without that attribute escapes it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    pages = {path: client.get(path).text for path in ("/", "/sessions/leave-approval")}

    for path, html in pages.items():
        opening_tags = re.findall(r"<button\b[^>]*>", html)
        # must fire: these pages really do render buttons, so the loop below is checking something rather than iterating over nothing.
        assert opening_tags, f"expected buttons on {path}, found none — this assertion saw nothing"
        for tag in opening_tags:
            assert 'type="submit"' in tag, (
                f'{path} renders a button with no explicit type="submit", so the page-wide busy rule '
                f"in static/js/app.js cannot reach it and it can still buy a provider call: {tag}")


# ── an honest wait (#236) ────────────────────────────────────────────────────


def _elapsed_timeline() -> dict[str, dict]:
    """Execute the real `static/js/app.js` against a fake clock and report what the status text said."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so the elapsed-time signal in static/js/app.js was NOT "
                    "asserted in this run — it is browser behaviour over time and nothing else in "
                    "this suite can see it (#236)")
    app_js = TEMPLATES_DIR.parent / "static" / "js" / "app.js"
    proc = subprocess.run([node, str(_ELAPSED_HARNESS), str(app_js)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, nothing observed: " + proc.stderr
    return {row["at"]: row for row in json.loads(proc.stdout)}


def test_a_long_call_says_so_after_ten_seconds_rather_than_looking_stuck():
    """A wait that outlives its own copy has to keep speaking (#236)."""
    t = _elapsed_timeline()
    started = t["request started"]["text"]
    eleven = t["eleven seconds in"]["text"]

    # must fire: nothing changes while the wait is still within what the copy promised.
    assert t["nine seconds in"]["text"] == started, (
        "the status text moved before the wait was long enough to need explaining — a label that "
        "always churns tells a reader nothing about a call that is genuinely slow")

    assert eleven != started, (
        "past ten seconds the page still said exactly what it said at second one, which is what a "
        "hung page also says")
    assert any("11s" in line for line in eleven), (
        "the elapsed seconds have to be visible, got: " + repr(eleven))
    assert any("20s" in line for line in t["twenty seconds in"]["text"]), (
        "the signal has to keep moving — one update and then stillness is a page that hung later")

    # The original label comes back, so a finished turn does not leave "still working" on screen.
    assert t["request finished"]["text"] == started
    assert t["request finished"]["liveTimers"] == 0, "the clock kept ticking after the call finished"

    # A second turn counts from zero.
    assert t["second request, three seconds in"]["text"] == started

    assert t["after pageshow"]["liveTimers"] == 0, (
        "returning to a cached page must not leave a timer running against a request that is over")


def test_no_provider_backed_button_still_promises_a_few_seconds():
    """The copy and the measurement have to agree (#236)."""
    offenders = [p.name for p in sorted(TEMPLATES_DIR.rglob("*.html"))
                 if "a few seconds" in p.read_text(encoding="utf-8")]
    assert offenders == [], (
        "these templates still promise 'a few seconds' for a call this repo documents as taking "
        "seconds to minutes: " + repr(offenders))


def test_the_no_js_path_still_states_how_long_it_will_take(client, monkeypatch):
    """The elapsed counter is an enhancement; the honest static copy is the floor."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    assert "Usually under a minute" in client.get("/").text, (
        "the create form states no duration at all, so a reader with JS off learns nothing")

    client.post("/sessions", data={"request_text": "x", "slug": "later",
                                   "provider": "create_only"})
    assert "Usually under a minute" in client.get("/sessions/later").text, (
        "the deferred-analysis page runs the same paid call and has to make the same promise")


def _swap_decisions() -> dict[int, dict]:
    """Execute the real `static/js/app.js` against htmx's own swap gate and report."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so the error-swap opt-in in static/js/app.js was NOT "
                    "asserted in this run — it is browser behaviour and nothing else in this suite "
                    "can see it (#203)")
    app_js = TEMPLATES_DIR.parent / "static" / "js" / "app.js"
    proc = subprocess.run([node, str(_ERROR_SWAP_HARNESS), str(app_js)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, so nothing was observed:\n" + proc.stderr
    out = json.loads(proc.stdout)
    result = {row["status"]: row for row in out["decisions"]}
    result["flash_cleared"] = out["flashClearedOnNewRequest"]
    return result


def test_error_responses_are_swapped_into_the_page_rather_than_dropped():
    """Every 4xx/5xx fragment this app builds was invisible in a real browser (#203)."""
    d = _swap_decisions()

    # must fire: the defect, still present in the vendored library, is that htmx drops all of these.
    assert not any(d[s]["htmxWouldSwap"] for s in (400, 403, 409, 413, 500, 502)), (
        "the harness no longer reproduces htmx's swap gate, so the rows below assert nothing"
    )

    for status in (409, 413, 502):
        assert d[status]["swapped"] is True, (
            f"a {status} response is still dropped, so the reader sees a completed progress bar and "
            f"an unchanged page — for 502 that is an invitation to buy the same call twice"
        )
    for status in (400, 403, 500):
        assert d[status]["swapped"] is True, f"a {status} response is still invisible"

    assert all(d[s]["isError"] is False for s in (409, 413, 502)), (
        "a handled, rendered response should stop logging as an uncaught one"
    )

    # The other direction, which a blanket `shouldSwap = true` would quietly break.
    assert d[200]["swapped"] is True, "the opt-in broke ordinary successful swaps"
    assert d[204]["swapped"] is False, (
        "204 means 'nothing to render' and htmx is right to skip it; the opt-in must not reach below "
        "400 and turn it into a swap of an empty body"
    )

    # #320: making errors visible is only half of it.
    assert d["flash_cleared"] is True, (
        "a new request did not clear the previous error notice, so a resolved failure goes on being "
        "displayed as a current one"
    )


