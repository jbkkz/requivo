"""Requivo Web: a failed first analysis is not a dead end (#207)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS
from tests.web.conftest import _make_session, engine_reply

# ── a failed first analysis is not a dead end (#207) ─────────────────────────


@pytest.fixture
def failing_analysis(monkeypatch):
    """A provider that claims the session fine and then fails the paid call (#207)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def boom(self, slug, *, surface="discover"):
        raise EngineError("Anthropic API unavailable (529).")

    monkeypatch.setattr(DiscoveryService, "run_discovery", boom)


def test_a_failed_first_analysis_lands_on_the_session_that_was_saved(client, with_provider,
                                                                     failing_analysis):
    """`start()` claims the session before the provider call, deliberately, so a refusal costs nothing."""
    with_provider()
    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "provider": "anthropic"}, follow_redirects=True)

    assert r.status_code == 200, "a failed first analysis still dead-ends on an error page"
    assert "Your request was saved" in r.text
    assert "Anthropic API unavailable" in r.text, "the cause was dropped, so the reader cannot act"
    assert "A leave approval system." in r.text, "the page does not show the request it saved"
    assert "Analyse request" in r.text, "the retry button the whole fix rests on is not on the page"

    metas = SessionService().list_sessions()
    assert [m.current_revision for m in metas] == [0]


def test_a_retry_exhausted_first_analysis_also_lands_on_the_saved_session(client, with_provider,
                                                                           monkeypatch):
    """`run_discovery`'s provider call can fail two ways."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with_provider("not json", "not json", "not json")

    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "provider": "anthropic"}, follow_redirects=True)

    assert r.status_code == 200, "a retry-exhausted first analysis dead-ends on an error page"
    assert "Your request was saved" in r.text
    assert "A leave approval system." in r.text, "the page does not show the request it saved"
    assert "Analyse request" in r.text

    metas = SessionService().list_sessions()
    assert [m.current_revision for m in metas] == [0]


def _long_contract_violation_reply() -> str:
    """Valid JSON, so this drives a real pydantic `ValidationError` rather than the shorter `no JSON object
    found in the reply` a non-JSON reply produces above."""
    payload = {"model": {}, "summary": {}}
    for i in range(7):
        payload[f"extra_field_{i}"] = "x" * 10
    return json.dumps(payload)


def test_a_retry_exhausted_analysis_carries_the_full_saved_reply_path_on_the_web_surface(
        client, with_provider, monkeypatch, tmp_path):
    """#362: #283 appends the saved-reply path to `ProviderOutputError.message`'s tail."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    long_reply = _long_contract_violation_reply()
    with_provider(long_reply, long_reply, long_reply)
    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "provider": "anthropic"}, follow_redirects=True)
    assert r.status_code == 200
    saved = list((tmp_path / ".requivo" / "debug").glob("*.txt"))
    assert len(saved) == 1, "the give-up exit must have written exactly one debug file"
    debug_path = str(saved[0])
    assert debug_path in r.text, (
        "the saved-reply path is the one artifact a bug report needs, and it must be reachable on "
        "the web surface -- not silently dropped by the notice's 300-char cap"
    )
    assert r.text.count(debug_path) == 1, (
        "the full path must appear exactly once -- a second occurrence would mean a truncated "
        "fragment is still leaking into the page alongside the complete one"
    )
    # Found in review: stripping only the path (and not the connector clause around it) left the notice ending "...was saved to" with nothing after it, immediately followed by the template's own "The reply that failed validation was saved to <path>." -- the same four words twice in a row across two lines.
    assert r.text.count("was saved to") == 1, (
        "the connector phrase must appear once, from the separately-rendered path sentence -- twice "
        "means the truncated notice still carries the dangling clause the path sentence repeats"
    )
    saved[0].unlink()  # isolate the second POST below to its own single debug file

    short_reply = "not json"
    with_provider(short_reply, short_reply, short_reply)
    r = client.post("/sessions", data={"request_text": "A different request entirely.",
                                       "provider": "anthropic"}, follow_redirects=True)
    assert r.status_code == 200
    saved2 = list((tmp_path / ".requivo" / "debug").glob("*.txt"))
    assert len(saved2) == 1
    debug_path2 = str(saved2[0])
    assert debug_path2 in r.text, "the shortest-cause message must not end mid-path either"
    # A truncated notice ending mid-path would leave an orphaned *prefix* of the filename sitting in the page next to nothing that resolves -- assert no such partial fragment survives once the complete path is rendered.
    fragment = Path(debug_path2).name[:20]
    assert r.text.count(fragment) == 1, (
        "the debug filename's own characters must appear only where the complete path does -- a "
        "second, partial occurrence is exactly the mid-path truncation this fix closes"
    )
    # This is the short-message case where the connector clause.
    assert r.text.count("was saved to") == 1, (
        "the connector phrase must appear once, from the separately-rendered path sentence -- twice "
        "means the truncated notice still carries the dangling clause the path sentence repeats"
    )


def test_a_retry_exhausted_deferred_analysis_also_lands_on_the_saved_session(client, with_provider,
                                                                              monkeypatch):
    """The second door onto the same first analysis, same failure family."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with_provider()
    slug = SessionService().create_session("A leave approval system.", slug="leave").slug
    with_provider("not json", "not json", "not json")

    r = client.post(f"/sessions/{slug}/discover", follow_redirects=True)

    assert r.status_code == 200
    assert "Your request was saved" in r.text
    assert "Analyse request" in r.text


def test_a_failed_retry_from_the_pending_page_re_renders_it_rather_than_a_500(client, with_provider,
                                                                             failing_analysis):
    """The second door onto the same first analysis."""
    with_provider()
    slug = SessionService().create_session("A leave approval system.", slug="leave").slug

    r = client.post(f"/sessions/{slug}/discover", follow_redirects=True)

    assert r.status_code == 200
    assert "Your request was saved" in r.text and "Anthropic API unavailable" in r.text
    assert "Analyse request" in r.text


def test_the_revision_zero_gate_still_holds_after_a_failed_analysis(client, with_provider,
                                                                   monkeypatch):
    """The must-fire half: recovering from the failure must not have cost the gate."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    real = DiscoveryService.run_discovery
    monkeypatch.setattr(DiscoveryService, "run_discovery",
                        lambda self, slug, *, surface="discover": (_ for _ in ()).throw(
                            EngineError("Anthropic API unavailable (529).")))

    with_provider(engine_reply())
    client.post("/sessions", data={"request_text": "A leave approval system.",
                                   "provider": "anthropic"}, follow_redirects=True)
    slug = SessionService().list_sessions()[0].slug

    monkeypatch.setattr(DiscoveryService, "run_discovery", real)
    r = client.post(f"/sessions/{slug}/discover", follow_redirects=True)

    assert r.status_code == 200
    assert SessionService().meta(slug).current_revision == 1, (
        "the retry after a failed analysis did not land, so the recovery path is a cul-de-sac"
    )


def test_an_error_fragment_retargets_but_a_full_region_keeps_its_own_target(client, with_provider):
    """The server half of #203: where a swapped 4xx/5xx lands decides whether the fix helps or repeats #30."""
    # One reply, because the conflict below is only reached *after* the provider call (#205).
    with_provider(engine_reply())
    slug = _make_session()

    oversized = client.post(f"/sessions/{slug}/answers",
                            data={"answers": "x" * (MAX_ANSWERS_CHARS + 1), "expected_revision": "1"},
                            headers={"HX-Request": "true"})
    assert oversized.status_code == 413
    assert "HX-Retarget" not in oversized.headers, (
        "the full-region refusal was retargeted, so #30's preserved answers land in the flash strip "
        "and the form they were typed into is left holding the stale text"
    )
    assert "<textarea" in oversized.text and "x" * 300 in oversized.text

    conflict = client.post(f"/sessions/{slug}/answers",
                           data={"answers": "The HR lead approves.", "expected_revision": "0"},
                           headers={"HX-Request": "true"})
    assert conflict.status_code == 409
    assert conflict.headers["HX-Retarget"] == "#flash", (
        "the conflict notice would swap over #session-body and delete the answers form — #30 again, "
        "reintroduced by the very change that made errors visible"
    )
    assert conflict.headers["HX-Reswap"] == "innerHTML"
    assert "notice danger" in conflict.text


def test_every_page_carries_the_flash_region_the_retarget_aims_at(client):
    """`HX-Retarget: #flash` is a promise about the document, not about the response (#203)."""
    slug = _make_session()
    for path in ("/", f"/sessions/{slug}"):
        assert 'id="flash"' in client.get(path).text, f"{path} has no flash region to retarget into"
