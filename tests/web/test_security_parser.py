"""`_hostname` refuses an authority it cannot determine a host from (#51), and each arm of the
cross-site guard carries its own error code (#52).

Both are findings the review on PR #48 reported for filing rather than folding in, and both are about
the same module answering *confidently* where it should have refused or distinguished.

**#51 is filed for the class, not the instance.** `Host: evil.com@127.0.0.1` resolves to `127.0.0.1`
and passes the allowlist — but no browser serializes userinfo into a `Host`, an `Origin` or a
`Referer`, so nothing reachable from the surface the guard defends is exploited by it. What makes it
worth a fix is that it is the third time this parser has produced a plausible answer for an input it
should have refused: #43 was the opaque origin, #45 the undetermined host, and this is the same shape
again. The first two were closed with caller-side checks; this one is closed in the parser, because a
caller-side check is what the next caller inherits without re-checking.
"""

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

# The control set. If a fix refuses these too it has closed the hole by closing the door.
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
    """End to end, at the header rather than at the helper — a parser fix nothing reads is not a fix.

    The host check is the one that also runs on reads, so a GET is enough to show it.
    """
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
    """A deliberate non-loopback bind does not widen the parser. The operator listed a hostname, not
    an authority that merely ends in one."""
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "requivo.example.test")
    from fastapi.testclient import TestClient

    c = TestClient(app, base_url="http://requivo.example.test", raise_server_exceptions=False)
    assert c.get("/", headers={"Host": "evil.com@requivo.example.test"}).status_code == 403
    assert c.get("/", headers={"Host": "requivo.example.test"}).status_code == 200   # must fire


def test_a_host_we_could_not_read_is_a_different_arm_from_one_we_read_and_refused(app):
    """A guard that could not read its input must not print what a guard that read it and refused
    prints — the correction #43 and #45 each made one seam over, and now the codes carry it too.

    The header itself is deliberately **not** reflected into the page. It is unvalidated
    caller-controlled bytes, `details` is not serialized on this surface, and the operator has the
    request in the terminal they started the server in. Distinguishing the arm is the diagnostic;
    echoing the input is not.
    """
    from fastapi.testclient import TestClient

    c = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    unreadable = c.get("/", headers={"Host": "evil.com@127.0.0.1:8765"})
    refused = c.get("/", headers={"Host": "example.test"})

    assert unreadable.status_code == refused.status_code == 403
    assert "undetermined_host" in unreadable.text
    assert "host_not_allowed" in refused.text
    assert "evil.com@127.0.0.1:8765" not in unreadable.text     # the input is not reflected back


# ── #52: one code, one fact, one details shape ────────────────────────────────

# The six arms of `_enforce`, each with the code it must raise and the exact `details` keys that code
# guarantees. This table *is* the contract `docs/compatibility.md` states, written where it can fail.
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
    """Before #52 all six raised `cross_site_request`, so the only way to tell *bad token* from
    *wrong host* was the message — which `docs/compatibility.md` says never to match on.

    The one code was raised for six distinct facts whose `details` payloads had five different
    shapes between them, against the rule `docs/compatibility.md` states for exactly this reason
    (#35): a code carries one fact and one `details` shape. A consumer matching `cross_site_request`
    and reading `details["origin"]` got a `KeyError` from the host arm, and the shape it was written
    against was never the contract.

    The counter-argument, which is real and which the split rejects: nothing serializes `details` on
    the Web surface — a refusal renders as HTML — so no consumer could observe the inconsistency,
    and an argued exception in the policy was the other defensible answer. What decided it is that
    the cost was already being paid: both #43 and #45 had to distinguish their new arm by message,
    because the code could not tell them apart. So the only handle a caller had for the distinction
    was the one it is told not to use — a present cost, not a future one — and
    `empty_selector_token` had been split for the identical shape one release earlier.
    """
    codes = [code for code, _ in ARMS]
    assert set(_arm_classes()) == set(codes)
    assert len(set(codes)) == len(codes)            # must fire: six distinct codes, not one reused


def test_the_family_base_is_not_raised_by_any_arm():
    """`cross_site_request` survives as the family — the guard catches it and a caller may still want
    *any* cross-site refusal — but nothing raises it, so no payload carries it any more."""
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
    """The point of the split, driven through the real middleware rather than by constructing the
    exception: a consumer matching a code and reading a key out of `details` has to work for every
    payload carrying it."""
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
