"""The counting-not-clipping affordance on the 20,000-character fields (#239).

`docs/web.md` invited exactly one shape of client-side help here and ruled out the other: "A
client-side affordance is welcome here, but it has to count and warn; it must never trim what the
reader typed." That is invariant 3 at the one place a real user meets it, and the reflex
implementation is the thing it forbids — `maxlength` makes a browser drop everything past the
ceiling with no event, no message and no visual difference, so an over-long paste arrives at exactly
the ceiling and sails through the server-side refusal written to catch it (#8).

Two halves, because they can fail independently:

* **What the page does**, driven through `counter_harness.js` against the shipped `app.js`. A
  `TestClient` runs no JavaScript, so the alternative was asserting that a literal string appears in
  the asset — which passes just as well against code that trims the field on every keystroke.
* **What the page is told**, driven through the ordinary client. The number the reader is shown has
  to be the number the server refuses on, so it is rendered from `web/config.py` rather than typed
  into a template.

Offline, isolated workspace per test; the fixtures live in `tests/web/conftest.py`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from requivo.web.config import MAX_ANSWERS_CHARS, MAX_REQUEST_CHARS
from requivo.web.templating import STATIC_DIR
from tests.web.conftest import HIGH_EXPLICIT, _make_session, engine_reply

_COUNTER_HARNESS = Path(__file__).parent / "counter_harness.js"

# The harness declares this ceiling on its fields. It is deliberately the same number the product
# uses, so the strings this file asserts on are the strings a reader actually sees — but it is the
# harness's own constant, not an import: what the *rendered page* declares is the other test's
# subject, and reading config here would let both tests agree with each other while disagreeing
# with the browser.
_HARNESS_LIMIT = 20_000


def test_the_harness_still_speaks_the_products_own_ceiling():
    """The literal strings the harness test asserts on ("16,000 / 20,000 characters") are only about
    the product while the harness declares the product's own ceiling. Change `MAX_INPUT_CHARS` and
    this goes red, naming the harness — rather than the behaviour test quietly going on passing about
    a number nothing uses any more."""
    assert _HARNESS_LIMIT == MAX_REQUEST_CHARS == MAX_ANSWERS_CHARS, (
        f"counter_harness.js declares a {_HARNESS_LIMIT}-character ceiling but the product now "
        f"refuses at {MAX_REQUEST_CHARS}/{MAX_ANSWERS_CHARS} — update the harness and the expected "
        f"strings below together")


def _counter_timeline() -> dict[str, dict]:
    """Execute the real `static/js/app.js` against a minimal DOM and return what the counter did.

    Node is the one thing here that is not guaranteed. When it is missing this **skips loudly** and
    names what went unasserted, rather than leaving a green run implying coverage it does not have.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so the counting-not-clipping rule in static/js/app.js was "
                    "NOT asserted in this run — it is browser behaviour and nothing else in this "
                    "suite can see it (#239)")
    app_js = STATIC_DIR / "js" / "app.js"
    proc = subprocess.run([node, str(_COUNTER_HARNESS), str(app_js)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, so nothing was observed:\n" + proc.stderr
    return {row["at"]: row for row in json.loads(proc.stdout)}


def test_the_character_counter_counts_and_warns_without_ever_touching_the_text():
    """The whole rule, on the shipped asset. The two assertions that carry it are the negative
    ones, each with a must-fire control in the same timeline so silence cannot pass for
    compliance: `writes` stays empty (the page never assigns to the field) and no clipping
    attribute is ever set -- asserted on rows where the counter demonstrably did fire, so a
    harness dispatching nothing would fail on the visible half first rather than the invisible."""
    t = _counter_timeline()

    # Below the threshold the affordance says nothing at all. A counter that is always on is
    # decoration; its *appearance* is the signal that the ceiling is close.
    assert t["initial"]["text"] == ""
    assert t["just under the threshold"]["text"] == "", (
        "the counter appeared below 80% of the ceiling — a counter that is always on carries no "
        "information when it matters")

    # must fire. Everything below is about wording and styling on a counter that has to exist first.
    at = t["at the threshold"]
    assert at["text"] == "16,000 / 20,000 characters", (
        "crossing 80% of the ceiling has to reveal the count — this is the whole affordance")
    assert at["className"] == "counter", "80% is a heads-up, not a refusal"
    assert at["live"] == "polite", "the count has to be announced, not only drawn"

    # The ceiling is the maximum *permitted* length, not the first refused one — the server accepts
    # exactly `MAX_REQUEST_CHARS` (`test_a_request_of_exactly_the_ceiling_is_accepted`), so styling
    # it as an error here would tell the reader a legal submission was about to be refused.
    ceiling = t["exactly at the ceiling"]
    assert ceiling["text"] == "20,000 / 20,000 characters"
    assert ceiling["className"] == "counter"

    over = t["one over the ceiling"]
    assert over["className"] == "counter danger"
    assert over["text"].startswith("20,001 / 20,000 characters")
    assert "refused" in over["text"], (
        "past the ceiling the copy has to say what will happen, not only turn red")

    # Deleting text takes the counter away again: the state is read off the field every time, never
    # latched.
    assert t["back down again"]["text"] == ""
    assert t["back down again"]["className"] == "counter"

    # A field the page did not have at load, arriving through a swap with the reader's refused text
    # already in it (#30). The count has to be right before a key is pressed.
    swapped = t["swapped in, already full"]
    assert swapped["text"] == "19,000 / 20,000 characters"

    # ── a ceiling the page cannot read ────────────────────────────────────────
    # The third state of the affordance, and the one the guard `!(limit > 0)` exists for. A counter
    # that invented a ceiling would be worse than none: it would warn about a submission the server
    # accepts, or stay quiet about one it refuses. So it declines — and declining has to mean *no
    # counter node at all*, not an empty one, because a node that exists is a node a later change can
    # start writing a guessed number into.
    for label in ("ceiling unparseable", "ceiling is zero", "no ceiling declared"):
        row = t[label]
        assert row["text"] is None, (
            f"at '{label}' the page built a counter against a ceiling it could not read — "
            f"got {row['text']!r}")

    # must fire, for that whole block: every one of those fields holds 19,000+ characters, which on a
    # readable ceiling is well past the threshold and demonstrably does produce a counter (the
    # "swapped in, already full" row above, same length, same code path). So the silence is the guard
    # firing, not the harness failing to reach these fields.
    assert t["swapped in, already full"]["length"] >= 19_000
    assert t["ceiling unparseable"]["length"] >= 19_000
    assert t["ceiling is zero"]["length"] >= 19_000

    # ── the two halves that must never fire ───────────────────────────────────
    for label, row in t.items():
        assert row["writes"] == [], (
            f"at '{label}' the page assigned to the field — the affordance may count and warn, and "
            f"must never alter what the reader typed (invariant 3, #8)")
        clipping = [name for name in row["fieldAttributes"] if "maxlength" in name.lower()]
        assert not clipping, (
            f"at '{label}' the page gave the field {clipping} — a browser then drops a paste past "
            f"the ceiling silently, which is exactly the failure this affordance replaces (#8)")


def test_the_limit_the_page_shows_is_the_limit_the_server_refuses_on(client, with_provider):
    """The number is rendered from `web/config.py`, never typed into a template. Two numbers
    hand-kept in two files drift invisibly in both directions: a page promising 20,000 against a
    server refusing at 10,000 warns too late, and the reverse warns about a submission that would
    have been accepted. Asserting the rendered attribute against the same constant the routes
    import makes that impossible."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT))
    _make_session()

    home = client.get("/")
    session = client.get("/sessions/leave-approval")
    assert home.status_code == 200 and session.status_code == 200

    # Positive control first: an errored or empty render satisfies every "in" assertion below by
    # rendering no field at all, and the silence would read as a pass.
    assert 'name="request_text"' in home.text
    assert 'name="answers"' in session.text

    assert f'data-limit="{MAX_REQUEST_CHARS}"' in home.text, (
        "the request field declares no ceiling, or declares one that is not the server's")
    assert f'data-limit="{MAX_ANSWERS_CHARS}"' in session.text, (
        "the answers field declares no ceiling, or declares one that is not the server's")
