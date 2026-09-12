"""Shared CLI plumbing -- called by more than one verb, and none of it a verb body itself.

Split out of `cli.py` by #550 (the lean pass, #548), as the mechanical first step ahead of #538's
`run`/`docs` reshaping: `_generator_service` (the shared preamble seven generator verbs share),
`_wrote`/`_wrote_file` (the one line every generator verb prints), `_announce_bind` and
`_is_wildcard_bind_address` (what `requivo web` and `requivo api serve` say when asked to bind
beyond loopback), and `_render_usage_safely` (`app()`'s own usage-line safety wrapper). The verb
bodies themselves -- `_cmd_discover`, `_cmd_brief`, `_cmd_web`, `app()` and the rest -- stay in
`cli.py`, byte-identical; only these helpers moved.
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
    """`render_usage`, made unable to end the process.

    Found by the audit on this branch, and it is the same ordering bug one call further out.
    `render_usage` prints a middle dot and an em dash, and two of its three call sites are *outside*
    the `UnicodeEncodeError` arm below -- one in the `RequivoError` handler, one after a wholly
    successful run. On a stream `configure_streams` could not reach, a successful `requivo brief`
    therefore still died at the usage line: after the provider call was billed and the revision
    applied, which is precisely the failure #29 exists to close.

    A usage summary is never worth that, so it degrades to a stated absence rather than an exception.
    Stated, not silent: a line nobody can read is a different thing from a run that made no calls,
    and the two must not print the same way.
    """
    try:
        render_usage(ledger)
    except UnicodeEncodeError:
        safe_write(sys.stderr, _USAGE_UNPRINTABLE)


def _generator_service(a, client) -> tuple[str, DiscoveryService]:
    """Shared preamble: (slug, service). Fails early if the session does not exist, so a typo'd slug
    never reaches the provider and gets billed for it.

    `accept_path=False`: every one of these seven verbs writes an artifact back into a session
    (`ArtifactService.save` refuses anything that is not `has_meta(slug)`), and none of them opens
    a file it is handed -- so a model.json path was never a meaningful input, and mining one for a
    slug used to report on, or silently operate on, a session the user never named (#402)."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session, accept_path=False)
    if not svc.exists(slug):
        raise svc.no_session(slug)
    return slug, DiscoveryService(client=client, sessions=svc)


def _print_session_candidates(resolution: SessionResolution) -> None:
    """The listing #541 requires before a resolved default is acted on -- every candidate, the
    default marked, so a wrong guess is visible before anything paid happens.

    Every field printed below came off a persisted `session.json` this process did not write, so it
    goes through `display_token` -- the same guard `session list` applies (#40/#70) -- or an
    `updated_at`/error carrying a newline plus a fabricated row forges a line of this listing. Found
    in review; pinned by
    `test_run_candidate_listing_cannot_be_made_to_print_a_line_a_session_wrote`."""
    print("Several sessions in this workspace:")
    for entry in resolution.candidates:
        marker = "→" if entry.slug == resolution.default else " "
        slug = display_token(entry.slug)
        # `entry.meta is not None`, not `entry.readable`: pyright narrows on the former and not on
        # the latter, which is a plain bool with no relationship the checker can see to `meta`.
        if entry.meta is not None:
            print(f"  {marker} {slug}  (revision {entry.meta.current_revision}, "
                  f"updated {display_token(entry.meta.updated_at)})")
        else:
            print(f"  {marker} {slug}  (unreadable: {display_token(entry.error or 'unknown')})")
    print(f"Using {display_token(resolution.default)} — pass a slug explicitly to choose another "
          "(see `requivo session list`).")


def _resolve_optional_session(svc: SessionService, ref: str | None, *, quiet: bool = False) -> str:
    """The CLI's half of #541's resolver: an explicit `ref` wins outright and is returned
    unexamined (it may be a path, for the two verbs that still accept one); otherwise resolve the
    workspace's default session, listing the candidates before anything paid happens -- unless
    `quiet`, which a `--json` caller sets because the payload already states the slug it answered
    for and a line beside it would break every pipe into `jq` (#246, see `_cmd_status`)."""
    if ref is not None:
        return ref
    resolution = svc.resolve_default_session()
    if resolution.candidates and not quiet:
        _print_session_candidates(resolution)
    return resolution.default


def _wrote(slug: str, result, label: str) -> None:
    """Say where a generated document went — the one line every generator verb shares.

    The path goes through `artifact_path` rather than being re-joined here (#36). Printing a path is
    still disclosing one, and `result.status.filename` is a plain `str` off an `ArtifactStatus` that
    nothing re-validates on the way out; that function carries the argument for why a display-only
    join is not exempt from the chokepoint, and which door is actually open."""
    _wrote_file(slug, result.status, label)


def _wrote_file(slug: str, status, label: str) -> None:
    """The same line over a bare `ArtifactStatus` — for the verb that writes two documents from one
    result (`estimate`, #519) and so has a second status that is not `result.status`."""
    # Through the chokepoint rather than joined here (#36), and direct rather than through the
    # repository (#76): `artifact_path` validates both halves of a name that came *off disk*, and a
    # printed path is a disclosure like any other. The repository's `load_artifact` is the read
    # seam; there is no seam that hands back a path, on purpose.
    print(f"\nWrote {label} → {store.artifact_path(slug, status.filename)}")


def _is_wildcard_bind_address(host: str) -> bool:
    """Does `host` name "every interface" — the IPv6 unspecified address as well as the IPv4 one?

    A literal check against `"0.0.0.0"` and `"::"` alone recognises exactly those two spellings and
    none of their equivalents: `::0`, the fully-expanded `0000:...:0000`, and every other all-zeros
    IPv6 literal name the identical bind address (`ipaddress.ip_address(...).is_unspecified` agrees
    they all are, and a socket layer binds them identically). Missing one meant `--host ::0` fell into
    the "real address" branch below, got auto-allowlisted verbatim, and reproduced #217's exact
    symptom under a spelling the original literal-string guard did not recognise — found by this
    diff's own review before it shipped.

    `ipaddress.ip_address` raises `ValueError` on anything that is not a literal IP at all — a
    hostname (`localhost`, `app.internal`), which is never a wildcard and is handled by the plain
    `"0.0.0.0"` check for IPv4's own single spelling (IPv4 has no equivalent-notation problem: unlike
    IPv6's abbreviation rules, "0.0.0.0" has no other literal spelling)."""
    if host == "0.0.0.0":
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _announce_bind(host: str, *, verb: str, exposure: str) -> None:
    """What both local HTTP surfaces say and do when asked to bind beyond loopback -- shared by
    `requivo web` and `requivo api serve` (#425 slice 4) rather than copied, since the host
    allowlist they both answer under is one definition (`requivo.host_policy`, #508) and the bind
    it has to be told about is the same bind. `exposure` is the one sentence that differs: what
    binding this surface wide actually exposes. Loopback: no warning, nothing written."""
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    if _is_wildcard_bind_address(host):
        # A wildcard bind address names every interface the machine has, not one a browser could
        # ever send back in a `Host` header — no client addresses a server as "0.0.0.0", it
        # addresses whatever IP or hostname it actually connected to. Auto-allowlisting the
        # literal wildcard string used to make `--host 0.0.0.0` *look* like it worked while every
        # LAN client got 403 `host_not_allowed` with no clue why. The guard staying fail-closed
        # here is right; the gap was that the one thing an operator actually needs to do next --
        # name the address LAN clients will use -- was never said. Pinned by
        # `test_a_wildcard_bind_is_not_auto_allowlisted_and_the_warning_names_the_env_var`.
        print(f"⚠  Binding to {host} (every interface): {exposure} A wildcard bind address is not "
              "a valid Host header, so it is NOT auto-allowlisted — every request will be refused "
              "until you set REQUIVO_WEB_ALLOWED_HOSTS to the hostname or IP LAN clients will "
              f"actually use, e.g.:\n"
              f"    REQUIVO_WEB_ALLOWED_HOSTS=192.168.1.50 requivo {verb} --host {host}",
              file=sys.stderr)
    else:
        print(f"⚠  Binding to {host}: {exposure} Prefer 127.0.0.1 unless you fully control the "
              "network.", file=sys.stderr)
        # The app only answers to hosts it recognises (the DNS-rebinding guard in host_policy.py),
        # and loopback is all it recognises by default. A deliberate bind elsewhere is the operator
        # saying this specific address is legitimate, so record it — without silently widening the
        # default. Unlike the wildcard case above, `host` here IS a real address a browser could
        # send as `Host`, so auto-allowlisting it is not the bug #217 found.
        os.environ.setdefault("REQUIVO_WEB_ALLOWED_HOSTS", host)
