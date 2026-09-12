"""Which hosts a local Requivo listener answers to -- the one cross-site check that applies to reads too (#508).
Binding to `127.0.0.1` is not a boundary: DNS rebinding makes the browser resolve another name to it and issue
same-origin requests at whatever is listening. Shared by every HTTP surface (`web/security.py`, `api/app.py`,
since #425 slice 4 including `check_request_origin`) rather than forked per surface, like `requivo.http` (#422),
framework-free. The synchronizer token, body cap and form parsing -- shaped by HTML forms the API has none of --
stay in `web/security.py`."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from requivo.core.errors import RequivoError

# A non-loopback bind is a deliberate act (`requivo web --host`), so any wider host is an explicit opt-in below.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# A compatibility surface (docs/web.md, cli.py's wildcard-bind warning) whose scope widened when the API joined.
ALLOWED_HOSTS_ENV = "REQUIVO_WEB_ALLOWED_HOSTS"


class CrossSiteRequestError(RequivoError):
    """A request did not prove it came from this app's own pages -- the family, not a code to raise (#52);
    every arm carries its own code and `details` shape, and `install_cross_site_guard` answers 403 for all.
    Pinned by `test_every_arm_has_its_own_code` and `test_the_family_base_is_not_raised_by_any_arm`."""

    code = "cross_site_request"


class UndeterminedHostError(CrossSiteRequestError):
    """No host could be read from the request at all -- absent, empty, or not an authority (#45, #51). `details`: `{host_header_present, host_header, hint}`."""

    code = "undetermined_host"


class HostNotAllowedError(CrossSiteRequestError):
    """The host was read and is not one this server answers to. `details`: `{host, hint}`."""

    code = "host_not_allowed"


def allowed_hosts() -> frozenset[str]:
    """Hostnames a listener accepts: loopback, plus any the operator listed in `REQUIVO_WEB_ALLOWED_HOSTS`."""
    extra = os.getenv(ALLOWED_HOSTS_ENV, "")
    return frozenset(LOOPBACK_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()})


# DNS letter-digit-hyphen plus IPv6 punctuation and `_`; anything else is a non-host string urlsplit handed back.
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_:%")


def hostname(value: str) -> str:
    """The bare hostname from a `Host` header or an origin URL, lowercased, port/brackets removed; `""` when
    undetermined, read by every caller as a refusal. Userinfo is refused, not stripped (#51), fixed in the
    parser since this is the third input it answered confidently about rather than refusing (#43, #45)."""
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
    """The host this request named, or a refusal -- the single definition every surface's guard calls. An
    undetermined `Host` is a **refusal**, not a skip (#45): the check this replaced treated an empty
    `hostname()` as "no check needed". Pinned by `test_an_unrecognised_host_is_refused_by_every_surface`."""
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


# ── the origin axis: who does the browser say sent this? (moved out of web/security.py, #425) ──

class CrossSiteFetchError(CrossSiteRequestError):
    """The browser's own `Sec-Fetch-Site` says this came from elsewhere. `details`: `{sec_fetch_site}`."""

    code = "cross_site_fetch"


class OpaqueOriginError(CrossSiteRequestError):
    """`Origin: null` -- a browser speaking and declining to attribute itself (#43). `details`: `{origin, host}`."""

    code = "opaque_origin"


class OriginMismatchError(CrossSiteRequestError):
    """The stated origin is not the same trust domain as the host addressed. `details`: `{origin, host}`."""

    code = "origin_mismatch"


# Sent verbatim: a browser declining to attribute itself, matched on the raw header, not `hostname()`.
OPAQUE_ORIGIN = "null"


def same_trust_domain(origin_host: str, host: str) -> bool:
    """Is a page served from `origin_host` the same trust domain as `host`? The same string always is; beyond
    that, only the loopback set (one interface, #43). **Deliberately port-blind**, since the request token
    (Web) and JSON content type (API) already gate the write against another loopback port -- pinned by
    `test_a_cross_port_loopback_origin_is_accepted_and_that_is_the_decision`. `REQUIVO_WEB_ALLOWED_HOSTS`
    entries do **not** join this class; an empty string on either side is never a match."""
    if not origin_host or not host:
        return False
    if origin_host == host:
        return True
    return origin_host in LOOPBACK_HOSTS and host in LOOPBACK_HOSTS


def check_request_origin(host: str, *, sec_fetch_site: str | None, origin: str | None,
                         referer: str | None) -> None:
    """The two origin-attribution checks every unsafe request faces, or a refusal -- called by both surfaces
    on whichever methods each decides (#425 slice 4). `Sec-Fetch-Site` first (unspoofable); `Origin: null`
    refused asymmetrically with an absent origin, since no origin means no browser speaking, while `null` is
    one declining to attribute itself (#43)."""
    fetch_site = sec_fetch_site or ""
    if fetch_site and fetch_site not in ("same-origin", "none"):
        raise CrossSiteFetchError(
            "this request came from another site", details={"sec_fetch_site": fetch_site})

    origin_header = origin or ""
    if origin_header.strip().lower() == OPAQUE_ORIGIN:
        raise OpaqueOriginError(
            "this request came from an opaque origin, which this server does not accept",
            details={"origin": OPAQUE_ORIGIN, "host": host})

    stated = origin_header or referer or ""
    if stated and not same_trust_domain(hostname(stated), host):
        raise OriginMismatchError(
            "this request came from another origin",
            details={"origin": hostname(stated), "host": host})
