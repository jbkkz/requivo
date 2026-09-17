"""Shared CLI plumbing (#550, #556): what more than one verb calls, and no verb body.
"""
from __future__ import annotations

import ipaddress
import os
import sys

from requivo.core import persistence as store
from requivo.core.selectors import display_token
from requivo.render.terminal import render_usage
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionResolution, SessionService
from requivo.streams import safe_write

_USAGE_UNPRINTABLE = (
    "\n(the API usage summary for this run could not be encoded for this console)\n"
)


def _render_usage_safely(ledger) -> None:
    """`render_usage`, made unable to end the process: two of its call sites are outside the
    `UnicodeEncodeError` arm (#29), and a usage line degrades to a stated absence, never silence."""
    try:
        render_usage(ledger)
    except UnicodeEncodeError:
        safe_write(sys.stderr, _USAGE_UNPRINTABLE)


def _generator_service(a, client) -> tuple[str, DiscoveryService]:
    """Shared preamble: (slug, service), failing before the provider is billed for a typo'd slug.
    `accept_path=False`: every generator verb writes back into a session and opens no file (#402)."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session, accept_path=False)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    return slug, DiscoveryService(client=client, sessions=svc)


def _print_session_candidates(resolution: SessionResolution) -> None:
    """The candidate listing before a resolved default is acted on (#541); every field came off a
    persisted `session.json`, so it goes through `display_token` (#40, #70).
    `test_run_candidate_listing_cannot_be_made_to_print_a_line_a_session_wrote`."""
    print("Several sessions in this workspace:")
    for entry in resolution.candidates:
        marker = "→" if entry.slug == resolution.default else " "
        slug = display_token(entry.slug)
        # `entry.meta is not None`, not `entry.readable`: pyright narrows on the former.
        if entry.meta is not None:
            print(f"  {marker} {slug}  (revision {entry.meta.current_revision}, "
                  f"updated {display_token(entry.meta.updated_at)})")
        else:
            print(f"  {marker} {slug}  (unreadable: {display_token(entry.error or 'unknown')})")
    print(f"Using {display_token(resolution.default)} — pass a slug explicitly to choose another "
          "(see `requivo session list`).")


def _resolve_optional_session(svc: SessionService, ref: str | None, *, quiet: bool = False) -> str:
    """The CLI's half of #541's resolver: an explicit `ref` wins unexamined; otherwise the default
    session, candidates listed unless `quiet` (a `--json` caller, #246)."""
    if ref is not None:
        return ref
    resolution = svc.resolve_default_session()
    if resolution.candidates and not quiet:
        _print_session_candidates(resolution)
    return resolution.default


def _wrote(slug: str, result, label: str) -> None:
    """Say where a generated document went; the path goes through `artifact_path` (#36), since a
    printed path is a disclosure too."""
    _wrote_file(slug, result.status, label)


def _wrote_file(slug: str, status, label: str) -> None:
    """The same line over a bare `ArtifactStatus`, for `estimate`'s second document (#519)."""
    # Through the chokepoint (#36), direct rather than through the repository (#76): no seam hands back a path.
    print(f"\nWrote {label} → {store.artifact_path(slug, status.filename)}")


def _is_wildcard_bind_address(host: str) -> bool:
    """Does `host` name every interface, the IPv6 unspecified address in every spelling included?
    `ipaddress.ip_address(...).is_unspecified` sees `::0` where a literal check saw only `::`; a
    hostname raises `ValueError` and is never a wildcard."""
    if host == "0.0.0.0":
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _announce_bind(host: str, *, verb: str, exposure: str) -> None:
    """What both local HTTP surfaces say and do when asked to bind beyond loopback (#425, #508);
    `exposure` is the one sentence that differs. Loopback: nothing."""
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    if _is_wildcard_bind_address(host):
        # A wildcard is not a `Host` a browser sends, so it is not auto-allowlisted; the warning names
        # what to do next (#217). `test_a_wildcard_bind_is_not_auto_allowlisted_and_the_warning_names_the_env_var`.
        print(f"⚠  Binding to {host} (every interface): {exposure} A wildcard bind address is not "
              "a valid Host header, so it is NOT auto-allowlisted — every request will be refused "
              "until you set REQUIVO_WEB_ALLOWED_HOSTS to the hostname or IP LAN clients will "
              f"actually use, e.g.:\n"
              f"    REQUIVO_WEB_ALLOWED_HOSTS=192.168.1.50 requivo {verb} --host {host}",
              file=sys.stderr)
    else:
        print(f"⚠  Binding to {host}: {exposure} Prefer 127.0.0.1 unless you fully control the "
              "network.", file=sys.stderr)
        # A deliberate bind elsewhere is a real `Host`, recorded without widening the default.
        os.environ.setdefault("REQUIVO_WEB_ALLOWED_HOSTS", host)


# What `web` and `api serve` each say when their optional extra is missing, keyed by the extra's pip name (#556).
_OPTIONAL_EXTRA_HINTS: dict[str, tuple[str, str]] = {
    "web": ("web interface", "the CLI or Claude Code"),
    "api": ("HTTP API", "the CLI, Requivo Web, or Claude Code"),
}


def _missing_extra_message(surface: str, e: ImportError) -> str:
    """The one message for a missing optional extra, shared by `web` and `api serve`. Returns the
    text, not the `EngineError`, so the raise stays at the allowlisted call site.
    `test_the_missing_web_extra_keeps_its_published_error_code`."""
    label, also_free_for = _OPTIONAL_EXTRA_HINTS[surface]
    return (
        f"The {label} is not installed. Install it with `pip install 'requivo[{surface}]'` "
        f"(or `uv tool install 'requivo[{surface}]'`). You do NOT need it for {also_free_for}. "
        f"(import error: {e})")
