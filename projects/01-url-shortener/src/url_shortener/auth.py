"""Security - API-key auth for the write/stats endpoints.

A FastAPI dependency rather than middleware, for a reason worth knowing: a
dependency is attached to *routes*, so the public redirect simply never has it.
Middleware runs for every request and would have to re-derive which paths it
guards from the URL - a string-matching exercise that is one refactor away from
accidentally guarding (or accidentally exposing) the wrong endpoint.

The dependency returns the validated token, so the rate limiter downstream can
bucket on it without re-parsing the header. FastAPI caches a dependency's result
per request, so it is parsed exactly once no matter how many things ask for it.
"""

from __future__ import annotations

from fastapi import Request

from .errors import Unauthorized
from .state import get_state

__all__ = ["BEARER_PREFIX", "require_api_key"]

BEARER_PREFIX = "Bearer "


async def require_api_key(request: Request) -> str:
    """Return the caller's validated API key, or reject the request.

    Expects `Authorization: Bearer <key>`. The comparison is exact - no
    trimming, no case-folding on the token - so a key with a stray trailing
    space is a different key, not a near miss.

    TODO(security): the "auth timing-safety is a documented decision" and "key
    at-rest story" boxes in the SPEC are still open. This is a `set` membership
    test on plaintext keys held in memory: fast, and *not* constant-time - the
    comparison can short-circuit on the first differing byte. Whether that is
    exploitable over a network here (and what `hmac.compare_digest` plus hashed
    keys would cost) is the call to make and write down in `docs/01-design.md`.

    Raises:
        Unauthorized: header missing, wrong scheme, or unknown token.
    """
    header = request.headers.get("authorization")
    if header is None or not header.startswith(BEARER_PREFIX):
        raise Unauthorized

    token = header[len(BEARER_PREFIX) :]
    if token not in get_state(request).api_keys:
        raise Unauthorized
    return token
