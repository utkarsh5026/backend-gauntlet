"""URL validation + normalization for submitted long URLs (security checklist).

A shortener is an open redirector by construction: whatever you store here, the
service will later hand to a browser as a `Location`. That makes the create path
an SSRF surface - someone shortens `https://169.254.169.254/latest/meta-data/`
and then gets *your users* (or your own link-preview crawler) to fetch it. So the
host is inspected before anything is stored.

What this does **not** do is resolve DNS. Only the host as written is checked, so
a public name that resolves to a private address (DNS rebinding) still gets
through. Catching that means resolving at fetch time and pinning the address,
which is a different piece of machinery than a validator.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

from .errors import BadRequest

__all__ = ["MAX_URL_LEN", "validate_long_url"]

MAX_URL_LEN = 2048
"""Longest normalized URL we are willing to store, in bytes."""

_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def validate_long_url(raw: str) -> str:
    """Parse, validate, and normalize a submitted long URL for storage.

    Only `https` is accepted, and hosts that point at internal infrastructure are
    rejected. On success the URL is normalized - the fragment is dropped and a
    redundant `:443` removed - so equivalent inputs dedupe to the same string.

    Raises:
        BadRequest: empty, unparseable, not https, hostless, internal, or longer
            than :data:`MAX_URL_LEN` once normalized.
    """
    trimmed = raw.strip()
    if not trimmed:
        raise BadRequest("invalid URL")

    try:
        parts = urlsplit(trimmed)
    except ValueError as exc:  # malformed IPv6 literal, bad port, ...
        raise BadRequest("invalid URL") from exc

    if parts.scheme.lower() != "https":
        raise BadRequest("only https URLs are allowed")

    try:
        # `.hostname` is the reason this reads so plainly: it lower-cases the
        # host and *strips the brackets* off an IPv6 literal. Using the raw
        # netloc instead would hand `[::1]` to the IP parser, which rejects it -
        # and a rejected parse would fall through to the hostname rules and pass.
        # That is the exact SSRF bypass this guards against.
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise BadRequest("invalid URL") from exc

    if not hostname:
        raise BadRequest("invalid URL")

    if _is_blocked_host(hostname):
        raise BadRequest("internal URLs are not allowed")

    # Fragment is never sent on a redirect; drop it so equivalent URLs dedupe.
    netloc = parts.netloc
    if port == 443:
        netloc = _strip_default_port(parts.netloc)
    normalized = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))

    if len(normalized) > MAX_URL_LEN:
        raise BadRequest("URL too long")
    return normalized


def _strip_default_port(netloc: str) -> str:
    """Remove a trailing `:443` from an authority, IPv6 brackets intact."""
    head, _, _ = netloc.rpartition(":")
    return head


def _is_blocked_host(hostname: str) -> bool:
    """True when the host is not safely routable to the public internet."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return _is_blocked_hostname(hostname)

    # `::ffff:127.0.0.1` is a loopback address wearing an IPv6 costume, and it is
    # "global" as far as the v6 rules are concerned. Unwrap before judging.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    # One question instead of a list of ranges. `is_global` is False for
    # loopback, private, link-local (incl. the 169.254.169.254 cloud metadata
    # endpoint), unspecified, reserved, multicast, and CGNAT (100.64.0.0/10) -
    # and it stays correct as the registries change, which a hand-rolled
    # enumeration does not.
    return not ip.is_global


def _is_blocked_hostname(hostname: str) -> bool:
    """Block names that conventionally resolve to local/internal endpoints."""
    host = hostname.rstrip(".").lower()
    return host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES)
