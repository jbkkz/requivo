"""What a paid web action cost, said out loud (#253).

The repo's own ordering is "Web is the product experience, the CLI is infrastructure", and cost
observability existed only on the infrastructure. `track_usage()` was opened in `cli.py` and nowhere
else, and `record_call()` is an explicit no-op with no active ledger — so every provider call made
through a browser was billed by Anthropic and recorded nowhere: not shown, not logged, not
recoverable afterwards.

Three states, and the two that are not a number are the ones worth testing:

* a **priced** call reports exact tokens and a cost labelled as an estimate, with the rate date;
* an **unpriced** call says there is no price on file and never guesses one;
* a call the provider reported **no usage** for, and an offline page, say nothing at all — and those
  two are not the same as a page that lost its usage line.

Offline, isolated workspace per test; the fixtures live in `tests/web/conftest.py`.
"""

from __future__ import annotations

import logging

from tests.web.conftest import BRIEF_REPLY, HIGH_EXPLICIT, HIGH_INFERRED, Spend, engine_reply

# Enough tokens that the rendered figures are unmistakable in a page of other numbers.
PAID = Spend(input_tokens=9000, output_tokens=3000, cache_read_input_tokens=400)

# 9000 + 400 + 3000. Written out so the assertions below read as a claim about the arithmetic rather
# than as a copy of whatever the code happened to produce.
PAID_TOKENS = "12,400"


def _analysed(client, with_provider, *replies, spend=PAID):
    """A session at revision 1, created through the web with a provider that reports a spend."""
    fake = with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                         *replies, spend=spend)
    client.post("/sessions", data={"request_text": "A leave approval system",
                                   "slug": "leave-approval", "provider": "anthropic"})
    return fake


# ── what a paid action reports ────────────────────────────────────────────────

def test_an_answers_turn_says_what_it_spent(client, with_provider):
    """The fragment the reader lands on after a paid turn states the tokens and the estimate.

    Tokens are exact and the cost is labelled — the same split `render_usage` makes on the CLI, in
    the Web's own vocabulary rather than the engine's.
    """
    _analysed(client, with_provider,
              engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})

    assert r.status_code == 200
    assert PAID_TOKENS in r.text, "the turn reported no token count"
    assert "estimate" in r.text, "a cost printed without the word estimate reads as a bill"
    assert "$" in r.text


def test_a_generation_says_what_it_spent(client, with_provider):
    """Every paid action, not only the conversational one. Generating a document is the step a reader
    is most likely to repeat, and the one whose cost nothing was reporting."""
    _analysed(client, with_provider, BRIEF_REPLY)

    r = client.post("/sessions/leave-approval/artifacts/brief")

    assert r.status_code == 200
    assert PAID_TOKENS in r.text
    assert "estimate" in r.text


# ── the two silences, which are not the same silence ──────────────────────────

def test_an_offline_page_reports_no_spend(client, with_provider):
    """Reading a session costs nothing, so it says nothing.

    The must-fire control for every assertion above: a template that printed a usage line
    unconditionally would satisfy all of them and would be reporting a cost for a page view.
    """
    _analysed(client, with_provider)

    for path in ("/", "/sessions/leave-approval"):
        page = client.get(path).text
        assert "estimate" not in page, f"{path} reported a spend for a page that made no call"
        assert PAID_TOKENS not in page


def test_a_call_the_provider_reported_no_usage_for_says_nothing_rather_than_zero(client,
                                                                                 with_provider):
    """Zero tokens is not a measurement (#253).

    The provider can answer without usage figures, and it does throughout this suite's offline fakes.
    Printing "0 tokens, est. $0.000" for that would be this project's own defect class: a value that
    could not be read rendered as a value that was read and found to be nothing.
    """
    _analysed(client, with_provider,
              engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT),
              spend=None)

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})

    assert r.status_code == 200
    assert "estimate" not in r.text, "a call with no reported usage was rendered as a $0.000 turn"


def test_an_unpriced_call_says_so_rather_than_guessing(client, with_provider, monkeypatch):
    """No price on file is an answer; a guessed number is not (invariant 6, and `cost_usd`'s own rule).

    A model the rate table does not carry — a preview, a new generation, a name the operator pinned
    by hand — produces exact tokens and no cost at all. The one thing this must never do is fall back
    to a neighbouring model's rate, which is how an estimate becomes an invented bill.
    """
    monkeypatch.setenv("MODEL", "claude-something-nobody-priced")
    _analysed(client, with_provider,
              engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})

    assert r.status_code == 200
    assert PAID_TOKENS in r.text, "tokens are exact even when the price is unknown"
    assert "no price on file" in r.text
    assert "est. ~$" not in r.text, "an unpriced call must not print a dollar figure"


def test_a_failed_paid_call_still_records_what_it_spent(client, with_provider, caplog):
    """A call that failed is still billed, and the reader still gets the error page (#253) -- the
    same contract as `test_a_failed_call_is_still_recorded_on_every_exit` one layer up. On this
    path the fragment is replaced by the app's error rendering, so the spend cannot ride the
    response -- it goes to the `requivo.web` logger, the terminal the operator started the server
    in. Silence there would mean a paid, failed turn left no trace anywhere."""
    # Three malformed replies: the JSON retry loop makes three attempts and spends on every one, then
    # gives up as a clean `EngineError`. A failure the provider itself does not handle would prove
    # nothing here, because it would never reach the recording exit this test is about.
    _analysed(client, with_provider, "not json", "not json", "not json")

    with caplog.at_level(logging.INFO, logger="requivo.web"):
        r = client.post("/sessions/leave-approval/answers",
                        data={"answers": "Exceptions go to HR.", "expected_revision": "1"})

    assert r.status_code >= 400, "the failure still has to reach the reader as an error"
    logged = [rec.getMessage() for rec in caplog.records]
    assert any("web-answer" in line for line in logged), (
        "a paid turn that failed was not recorded anywhere: " + repr(logged))


# ── the two redirecting paths, which now carry the figure to the following GET (#253) ─────────
#
# Both `POST /sessions` (provider=anthropic) and `POST /sessions/{slug}/discover` answer 303 and
# have no body of their own to put a figure in. `track_web_usage(..., carry_to=slug)` stashes the
# view server-side, keyed by slug, and the GET the redirect sends the reader to pops it once. A
# query parameter was rejected on the issue itself: it would render a forgeable number as a cost
# claim, which is worse than showing nothing.


def test_a_first_analysis_lands_on_a_page_showing_what_it_spent(client, with_provider):
    """`POST /sessions` with provider=anthropic redirects to the session page with no body of its
    own -- the figure has to survive that hop server-side, not on the URL."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED), spend=PAID)

    r = client.post("/sessions", data={"request_text": "A leave approval system",
                                       "slug": "leave-approval", "provider": "anthropic"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"])

    assert PAID_TOKENS in page.text, "the figure did not survive the redirect from creation"
    assert "estimate" in page.text


def test_a_deferred_discovery_lands_on_a_page_showing_what_it_spent(client, with_provider):
    """The second door onto a first analysis: `create_only` now, `/discover` later -- same
    redirect-with-no-body shape, same carry."""
    with_provider()
    client.post("/sessions", data={"request_text": "A leave approval system",
                                   "slug": "leave-approval", "provider": "create_only"})
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED), spend=PAID)

    r = client.post("/sessions/leave-approval/discover", follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"])

    assert PAID_TOKENS in page.text
    assert "estimate" in page.text


def test_a_failed_first_analysis_still_shows_the_spend_it_recorded(client, with_provider, monkeypatch):
    """A call that fails after spending tokens still surfaces its recorded spend -- the same
    contract `test_a_failed_paid_call_still_records_what_it_spent` pins for the answers turn, now
    for the pending-page path rather than an error fragment. Three malformed replies: the JSON
    retry loop spends on every attempt before giving up, so usage accumulates across all three
    (9000+400+3000 x 3 = 37,200), not `PAID_TOKENS` itself."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with_provider("not json", "not json", "not json", spend=PAID)

    r = client.post("/sessions", data={"request_text": "A leave approval system",
                                       "provider": "anthropic"}, follow_redirects=True)

    assert r.status_code == 200
    assert "Your request was saved" in r.text, "the recovery page itself regressed"
    assert "37,200" in r.text, "a paid, failed turn recorded a spend but the retry page shows none"


def test_reloading_the_landing_page_does_not_repeat_the_spend_line(client, with_provider):
    """Read-once: a plain reload of the page the redirect landed on must not go on reporting a
    spend for an action that already happened and was already shown once."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED), spend=PAID)
    client.post("/sessions", data={"request_text": "A leave approval system",
                                   "slug": "leave-approval", "provider": "anthropic"})

    reload = client.get("/sessions/leave-approval")

    assert PAID_TOKENS not in reload.text, "a reload repeated a spend already shown once"


def test_an_offline_visit_to_a_session_with_no_pending_spend_shows_nothing(client, with_provider):
    """The must-fire control for the carry itself: a session nobody just paid for shows no line --
    proves the GET route's new `usage` context key is not unconditionally set."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED), spend=PAID)
    client.post("/sessions", data={"request_text": "A leave approval system",
                                   "slug": "leave-approval", "provider": "anthropic"})
    client.get("/sessions/leave-approval")  # consumes the stashed figure

    page = client.get("/sessions/leave-approval")

    assert "estimate" not in page.text
    assert PAID_TOKENS not in page.text
