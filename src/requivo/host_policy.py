"""Which hosts a local Requivo listener answers to -- the one cross-site check that applies to reads,
shared by every HTTP surface this project ships (#508).

Binding to `127.0.0.1` is not a security boundary, and the reason is not about forms. A hostile page
the user has open can make the browser resolve `evil.example.com` to `127.0.0.1` and then issue
same-origin requests at whatever is listening there -- DNS rebinding, which is transport-level and
therefore applies to any local listener whether or not it has a form, a cookie or a token. That is
why `web/security.py` calls this check "the only one that applies to reads as well": every other arm
of that module's guard runs on unsafe methods only, and a rebound origin's *reads* sail past all of
them.

`api/app.py` (#425) is a second local listener whose reads are the client's verbatim request and the
understanding built from it, and it shipped with no host check at all: `GET /api/v1/sessions` with
`Host: evil.example.com` answered 200 where Requivo Web answered 403. This module is where the check
lives now, so a third surface inherits it instead of copying it -- the same move, for the same
reason, as `requivo.security_headers` (#503) and `requivo.http` (#422), and the general form
`CLAUDE.md` already states about a neutral concept trapped in the module that happens to own it:
move the concept out, never write the next consumer a second copy.

Framework-free by construction, like its two siblings: `check_host` takes the raw `Host` header as a
string and returns or raises. Nothing here imports fastapi or starlette, so the base install can
reach it with neither extra present, and `tests/test_boundaries.py` scans `api/` and `web/` as
surfaces rather than needing an allowlist entry for this module.

**What this module deliberately does not hold:** the `Sec-Fetch-Site`, `Origin`/`Referer` and
synchronizer-token checks. Those run on unsafe methods, they are shaped by the fact that Requivo Web
serves HTML forms, and the API's answer for its own future writes is argued separately in
`docs/decisions/0004-the-http-api-facade.md` §5 -- a JSON content-type requirement rather than a
token. Sharing the host axis is what #508 asked for; sharing the rest would be assuming an answer
that record has not reached.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from requivo.core.errors import RequivoError

# Where the server may legitimately be addressed. A non-loopback bind is a deliberate act (`requivo
# web --host`), so it is an explicit opt-in here too rather than a hole left open by default.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Named for the surface that introduced it and read by every surface since. Renaming it to something
# neutral would be a user-facing break for one word: it is documented in `docs/web.md`, promised in
# `docs/compatibility.md`'s environment-variable section, and `cli.py` prints it in the warning a
# wildcard bind produces. The name is the compatibility surface; the scope is what widened.
ALLOWED_HOSTS_ENV = "REQUIVO_WEB_ALLOWED_HOSTS"


class CrossSiteRequestError(RequivoError):
    """A request did not prove it came from this app's own pages — the family, not a code to raise.

    Every arm below carries its own code, and that is #52. This one error was raised for six distinct
    facts whose `details` payloads had five different shapes between them, against the rule
    `docs/compatibility.md` states in this repository for exactly this reason (#35): **a code carries
    one fact and one `details` shape**. A consumer matching `cross_site_request` and reading
    `details["origin"]` gets a `KeyError` from the host arm, and the shape it was written against was
    never the contract.

    The counter-argument, which is real and which this rejects: nothing serializes `details` on the
    Web surface — a refusal renders as HTML — so no consumer can observe the inconsistency today, and
    an argued exception in the policy was the other defensible answer. What decides it is that the
    cost is already being paid. Both #43 and #45 had to distinguish their new arm **by message**,
    because the code could not tell them apart, and the same policy says never to match on the
    message. So the only handle a caller has for the distinction is the one it is told not to use.
    That is a present cost, not a future one, and `empty_selector_token` was split for the identical
    shape one release ago.

    The family is kept because `install_cross_site_guard` catches it and answers 403 for every arm,
    and because a caller that wants *any* cross-site refusal should not have to enumerate six names.
    Nothing raises it directly.

    It lives here rather than in `web/security.py` because two of its arms do (#508). The four that
    are about an unsafe method and a form stay in that module; the base has to sit with whichever
    half is reachable from a surface that has no forms, and that is this one.
    """

    code = "cross_site_request"


class UndeterminedHostError(CrossSiteRequestError):
    """No host could be read from the request at all — absent, empty, or not an authority (#45, #51).

    `details`: `{host_header_present, host_header, hint}`. `host_header_present` is what separates
    *no header was sent* from *a header was sent and could not be read*; both are the same fact here
    — nobody could attribute this request — and the same shape, so they share a code.
    """

    code = "undetermined_host"


class HostNotAllowedError(CrossSiteRequestError):
    """The host was read and is not one this server answers to. `details`: `{host, hint}`."""

    code = "host_not_allowed"


def allowed_hosts() -> frozenset[str]:
    """Hostnames a Requivo listener accepts in a `Host` header: loopback, plus any the operator listed
    in `REQUIVO_WEB_ALLOWED_HOSTS` (comma-separated) when deliberately binding elsewhere."""
    extra = os.getenv(ALLOWED_HOSTS_ENV, "")
    return frozenset(LOOPBACK_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()})


# What a determined host may contain once `urlsplit` has lowercased it and removed the port and any
# IPv6 brackets: the letter-digit-hyphen set of a DNS name, plus what an IPv6 literal leaves behind
# (`:` between groups, `%` before a zone id) and `_`, which is not legal in a DNS hostname but does
# occur in internal names an operator may deliberately bind to. Anything else means `urlsplit`
# handed back a string that is not a host, and this returns the third state instead of that string.
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_:%")


def hostname(value: str) -> str:
    """The bare hostname from a `Host` header (`[::1]:8765`) or an origin URL (`http://evil.com`),
    lowercased, port and IPv6 brackets removed — so the two are comparable. `""` when this could not
    determine a host at all, which every caller reads as a refusal.

    **Userinfo is refused rather than stripped, and that is #51.** `urlsplit` is a URL parser and
    correctly discards the `user@` part of an authority, so `Host: evil.com@127.0.0.1` resolved to
    `127.0.0.1`, passed `allowed_hosts()`, and `Origin: http://evil.com@127.0.0.1` came out
    same-trust-domain. Not reachable from a browser — none of `Host`, `Origin` or `Referer` is ever
    serialized with userinfo, and RFC 7231 requires a `Referer` to have it removed — so this closes
    a hole with no attacker who benefits.

    It is fixed for the class, not the instance. This is the third time this parser answered
    *confidently* about an input it should have refused: #43 was the opaque origin parsing to the
    plausible hostname `"null"`, #45 was an undetermined host read as *no host check needed*, and this
    is the same shape again. The first two were closed with checks at the caller. **This one is closed
    in the parser**, because a caller-side check is a guarantee the next caller inherits without
    re-checking — and it has three callers now, on two different headers and two surfaces.

    The charset test is the general form of the same rule and is why `Host: 127.0.0.1 evil.com` now
    refuses too: it previously came back as that whole string, which is not a hostname, and was
    refused only by happening to miss the allowlist. A parser that returns a non-host and relies on a
    later equality test to reject it is answering where it should be declining.

    Known residue, stated rather than implied: an **unbracketed** IPv6 literal (`Host: fe80::1`) still
    parses to `fe80` with the rest read as a port. It is malformed as an authority, no browser emits
    it, and it fails the allowlist — but the parser does answer, so this docstring does not claim the
    class is empty.
    """
    raw = value.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "//" in raw else "//" + raw)
        host = (parts.hostname or "").lower()
        if parts.username is not None:      # any userinfo at all, including an empty `@127.0.0.1`
            return ""
    except ValueError:
        return ""
    if not host or set(host) - _HOST_CHARS:
        return ""
    return host


def check_host(raw_host: str | None) -> str:
    """The host this request named, or a refusal. The single definition every surface's guard calls.

    An undetermined `Host` is a **refusal**, not a skip. `hostname` returns `""` when it could not
    find a host at all — an absent header, an empty or whitespace-only one, or one that will not
    parse — and the check this replaced read that as *no host check needed* (`if host and host not in
    allowed_hosts()`). So the one request nobody could attribute walked past the only check that also
    runs on reads, and the guard reported nothing while it was off. Observed rather than reasoned:
    against the 0.10.1 candidate, `GET / HTTP/1.0` with no `Host` and `GET / HTTP/1.1` with an empty
    one both answered 200, because h11 requires `Host` on 1.1 only and passes an empty one straight
    through.

    This refuses a `GET`, which is a real behaviour change and the intended one. HTTP/1.1 requires a
    `Host` and every browser, `curl`, httpx and requests sends one; nothing here documents HTTP/1.0
    support; and a caller able to craft a hostless request can open a socket to this port directly,
    so it gains nothing from the skip that it did not already have. The cost is a caller that does
    not exist. What it buys is the third state stated instead of silently folded into the clean one:
    *could not determine the host* now reads differently from *determined it and was happy* (#45).

    Since #51 the undetermined arm also covers a header that *was* sent and is not an authority —
    userinfo, or a character no hostname carries. The wording says "could not read" rather than "did
    not state" so it is true of both; `host_header_present` in `details` is what tells them apart,
    which is why they are one code and one shape rather than two (#52).

    Pinned across both surfaces by `test_an_unrecognised_host_is_refused_by_every_surface`.
    """
    host = hostname(raw_host or "")
    if not host:
        raise UndeterminedHostError(
            "this request did not name a host this server could read — send a Host header naming "
            "this server",
            details={"host_header_present": raw_host is not None, "host_header": raw_host or "",
                     "hint": "HTTP/1.1 requires a Host header; HTTP/1.0 without one is not supported"})
    if host not in allowed_hosts():
        raise HostNotAllowedError(
            f"this server does not answer to host {host!r}",
            details={"host": host, "hint": f"set {ALLOWED_HOSTS_ENV} to bind elsewhere on purpose"})
    return host
