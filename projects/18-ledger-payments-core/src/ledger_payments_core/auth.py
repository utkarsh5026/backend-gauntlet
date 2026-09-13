"""API-key authentication — the Security checklist's first item.

Moving money behind an open endpoint is not a payments API. The SPEC's criterion: a
request without a valid key is rejected *before the handler runs*, and a key never
appears in a log line or an error body.

In FastAPI, "before the handler runs" is a **dependency**. Attach
`Depends(require_api_key)` to the routes that need it — or to the router's
`dependencies=[…]` — and FastAPI resolves it, and lets it raise, before the endpoint
body is ever called. `/healthz` and `/metrics` stay outside it: an orchestrator's probe
and a Prometheus scraper don't carry keys.

Deliberately **not wired yet**. The SPEC's order of attack puts auth after the
verticals, and wiring an unbuilt dependency would put a `501` in front of V1.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Header

from .state import StateDep

__all__ = ["require_api_key"]


async def require_api_key(
    state: StateDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Reject the request unless it carries `Authorization: Bearer <key>` for one of the
    configured keys.

    TODO(security): parse the bearer token and check it against
    `state.settings.api_key_list` with `hmac.compare_digest` — `token in keys` is the
    same timing oracle V4's `verify` avoids. Raise `UnauthorizedError` on any failure,
    with one message for "missing" and "wrong" alike, and never log the header.
    """
    raise NotImplementedError(
        "security: API-key auth — reject a missing or invalid key before the handler runs"
    )
