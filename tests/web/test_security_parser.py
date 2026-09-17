"""`_hostname` refuses an authority it cannot determine a host from (#51), and each arm of the cross-site
guard carries its own error code (#52)."""

from __future__ import annotations

import pytest

from requivo.web.security import (
    ALLOWED_HOSTS_ENV,
    CSRF_HEADER,
    _hostname,
    _same_trust_domain,
    allowed_hosts,
    csrf_token,
)

# ── #51: what the parser must refuse ──────────────────────────────────────────

# Each case is an authority a caller can put on the wire and the reason it is not a determinable host.
UNDETERMINABLE = [
    ("evil.com@127.0.0.1", "userinfo: the host is the part after the @, and no browser sends one"),
    ("http://evil.com@127.0.0.1", "the same, spelled as an origin URL"),
    ("user:pass@127.0.0.1", "userinfo with a password"),
    ("@127.0.0.1", "empty userinfo — still an authority a browser never produces"),
    ("127.0.0.1 evil.com", "whitespace inside the authority: two things, not one host"),
    ("127.0.0.1@", "userinfo with nothing after it"),
    ("[::1", "an unbalanced bracket"),
    ("http:///", "a well-formed URL naming nobody"),
    ("", "nothing at all"),
    ("   ", "whitespace only"),
]

# The control set.
DETERMINABLE = [
    ("127.0.0.1:8765", "127.0.0.1"),
    ("127.0.0.1", "127.0.0.1"),
    ("localhost", "localhost"),
    ("LOCALHOST:8765", "localhost"),
    ("[::1]:8765", "::1"),
    ("http://localhost:3000", "localhost"),
    ("https://requivo.example.test", "requivo.example.test"),
    ("null", "null"),           # parsed, and refused later by name — see OPAQUE_ORIGIN
]


@pytest.mark.parametrize("value, why", UNDETERMINABLE)
def test_an_authority_that_is_not_a_host_is_refused_by_the_parser(value, why):
    assert _hostname(value) == "", why


@pytest.mark.parametrize("value, expected", DETERMINABLE)
def test_an_ordinary_authority_still_parses(value, expected):
    """Must fire. Returning `""` for everything satisfies every assertion above."""
    assert _hostname(value) == expected


def test_userinfo_no_longer_smuggles_a_loopback_host_past_the_allowlist():
    """The measured instance, stated as the property it violates rather than as a string."""
    assert _hostname("evil.com@127.0.0.1") not in allowed_hosts()
    assert _hostname("127.0.0.1") in allowed_hosts()          # must fire


def test_userinfo_no_longer_makes_an_origin_same_trust_domain():
    assert not _same_trust_domain(_hostname("http://evil.com@127.0.0.1"), "127.0.0.1")
    assert _same_trust_domain(_hostname("http://127.0.0.1:3000"), "127.0.0.1")   # must fire


def test_the_guard_refuses_a_host_header_carrying_userinfo(app):
    """End to end, at the header rather than at the helper — a parser fix nothing reads is not a fix."""
    from fastapi.testclient import TestClient

    c = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    assert c.get("/", headers={"Host": "evil.com@127.0.0.1:8765"}).status_code == 403
    assert c.get("/", headers={"Host": "127.0.0.1:8765"}).status_code == 200      # must fire


def test_the_guard_refuses_an_origin_carrying_userinfo(client):
    r = client.post("/sessions",
                    data={"request_text": "A leave approval system.", "provider": "create_only"},
                    headers={"Origin": "http://evil.com@127.0.0.1:8765"})
    assert r.status_code == 403


def test_an_operator_listed_host_with_userinfo_is_still_refused(monkeypatch, app):
    """A deliberate non-loopback bind does not widen the parser."""
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "requivo.example.test")
    from fastapi.testclient import TestClient

    c = TestClient(app, base_url="http://requivo.example.test", raise_server_exceptions=False)
    assert c.get("/", headers={"Host": "evil.com@requivo.example.test"}).status_code == 403
    assert c.get("/", headers={"Host": "requivo.example.test"}).status_code == 200   # must fire


def test_a_host_we_could_not_read_is_a_different_arm_from_one_we_read_and_refused(app):
    """A guard that could not read its input must not print what a guard that read it and refused prints --
    the correction #43 and #45 each made one seam over, and now the codes carry it too."""
    from fastapi.testclient import TestClient

    c = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    unreadable = c.get("/", headers={"Host": "evil.com@127.0.0.1:8765"})
    refused = c.get("/", headers={"Host": "example.test"})

    assert unreadable.status_code == refused.status_code == 403
    assert "undetermined_host" in unreadable.text
    assert "host_not_allowed" in refused.text
    assert "evil.com@127.0.0.1:8765" not in unreadable.text     # the input is not reflected back


# ── #52: one code, one fact, one details shape ────────────────────────────────

# The six arms of `_enforce`, each with the code it must raise and the exact `details` keys that code guarantees.
ARMS = [
    ("undetermined_host", {"host_header_present", "host_header", "hint"}),
    ("host_not_allowed", {"host", "hint"}),
    ("cross_site_fetch", {"sec_fetch_site"}),
    ("opaque_origin", {"origin", "host"}),
    ("origin_mismatch", {"origin", "host"}),
    ("missing_request_token", set()),
]


def _arm_classes():
    from requivo.web import security

    return {c.code: c for c in security.CrossSiteRequestError.__subclasses__()}


def test_every_arm_has_its_own_code():
    """Before #52 all six raised `cross_site_request`."""
    codes = [code for code, _ in ARMS]
    assert set(_arm_classes()) == set(codes)
    assert len(set(codes)) == len(codes)            # must fire: six distinct codes, not one reused


def test_the_family_base_is_not_raised_by_any_arm():
    """`cross_site_request` survives as the family — the guard catches it and a caller may still want *any*
    cross-site refusal — but nothing raises it, so no payload carries it any more."""
    from requivo.web import security

    assert security.CrossSiteRequestError.code == "cross_site_request"
    assert "cross_site_request" not in _arm_classes()


def test_every_arm_code_is_still_a_403():
    from requivo.http import STATUS_BY_CODE

    for code, _ in ARMS:
        assert STATUS_BY_CODE[code] == 403, code
    assert STATUS_BY_CODE["cross_site_request"] == 403


@pytest.mark.parametrize("code, keys", ARMS)
def test_each_arm_carries_exactly_the_details_shape_its_code_promises(code, keys, raw_client,
                                                                     monkeypatch):
    """The point of the split, driven through the real middleware rather than by constructing the exception."""
    raised = {}
    from requivo.web import security

    real = security._enforce

    async def capture(request):
        try:
            await real(request)
        except security.CrossSiteRequestError as exc:
            raised["exc"] = exc
            raise

    monkeypatch.setattr(security, "_enforce", capture)

    trigger = {
        "undetermined_host": ({"Host": "evil.com@127.0.0.1"}, "get"),
        "host_not_allowed": ({"Host": "example.test"}, "get"),
        "cross_site_fetch": ({CSRF_HEADER: csrf_token(), "Sec-Fetch-Site": "cross-site"}, "post"),
        "opaque_origin": ({CSRF_HEADER: csrf_token(), "Origin": "null"}, "post"),
        "origin_mismatch": ({CSRF_HEADER: csrf_token(), "Origin": "http://example.test"}, "post"),
        "missing_request_token": ({}, "post"),
    }[code]
    headers, method = trigger

    if method == "get":
        raw_client.get("/", headers=headers)
    else:
        raw_client.post("/sessions", data={"request_text": "x", "provider": "create_only"},
                        headers=headers)

    exc = raised.get("exc")
    assert exc is not None, f"no cross-site refusal was raised for {code}"
    assert exc.code == code
    assert set(exc.details or {}) == keys, f"{code} carried {sorted(exc.details or {})}"


def test_a_legitimate_post_raises_no_arm_at_all(client):
    """Must fire. Six arms that all trigger unconditionally would satisfy every assertion above."""
    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "provider": "create_only"},
                    headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
                    follow_redirects=False)
    assert r.status_code == 303
