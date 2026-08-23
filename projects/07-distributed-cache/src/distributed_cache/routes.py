"""HTTP surface: the public client API + the internal node-to-node RPC.

The routing and body handling are wired. The public `/cache` handlers call the
coordinator (V4), which decides local-vs-forward; the `/internal/cache` handlers
hit the *local* store only (they are the endpoint a peer coordinator forwards
to). Run as-is and `GET /healthz` + `GET /cluster` work; the first real cache op
raises a V1/V2/V4 NotImplementedError, which is the worklist.

Public and internal are deliberately separate paths (SPEC: internal RPC must not
be spoofable as a client request).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from .errors import InvalidRequest, NotFound, ValueTooLarge
from .membership import Member
from .state import AppState

__all__ = ["internal_router", "public_router"]

# Max key length. A key also goes into a URL and, once forwarded, into another
# URL — so it stays short and boring. A cheap guard, not the whole security story.
MAX_KEY_LEN = 512

OCTET_STREAM = "application/octet-stream"


def get_state(request: Request) -> AppState:
    """Pull the assembled runtime off the app. Set by the lifespan in `main`."""
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]
TtlQuery = Annotated[int | None, Query(ge=0, description="Time-to-live in seconds.")]


def check_key(key: str) -> str:
    """Validate a key from the path before it touches the ring or the store."""
    if not key or len(key.encode()) > MAX_KEY_LEN:
        raise InvalidRequest(f"key must be 1..={MAX_KEY_LEN} bytes")
    # TODO(security): bound the charset too — control characters and separators
    # have no business in a key that becomes part of a forwarded URL.
    return key


public_router = APIRouter(tags=["cache"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])


@public_router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@public_router.get("/cluster")
async def cluster(state: StateDep) -> list[Member]:
    """This node's membership view (observability + convergence tests)."""
    return state.membership.snapshot()


@public_router.get("/cache/{key}", response_class=Response)
async def get_cache(key: str, state: StateDep) -> Response:
    """Resolve via the coordinator (local or forwarded)."""
    value = await state.coordinator.get(check_key(key))
    if value is None:
        raise NotFound()
    return Response(content=value, media_type=OCTET_STREAM)


@public_router.put("/cache/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def put_cache(key: str, request: Request, state: StateDep, ttl: TtlQuery = None) -> None:
    """Store a value, replicated to the key's owners.

    TODO(security): require the cluster auth token here before writing.
    """
    check_key(key)
    body = await request.body()
    if len(body) > state.max_value_bytes:
        raise ValueTooLarge()
    await state.coordinator.put(key, body, ttl)


@public_router.delete("/cache/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cache(key: str, state: StateDep) -> None:
    """Evict from all replicas."""
    await state.coordinator.delete(check_key(key))


# --- internal RPC: local store only (no routing) ------------------------------
# TODO(security): gate this whole subtree behind the cluster auth token so an
# outsider can't inject values by pretending to be a peer.


@internal_router.get("/cache/{key}", response_class=Response)
async def internal_get(key: str, state: StateDep) -> Response:
    value = state.coordinator.local_get(key)
    if value is None:
        raise NotFound()
    return Response(content=value, media_type=OCTET_STREAM)


@internal_router.put("/cache/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def internal_put(key: str, request: Request, state: StateDep, ttl: TtlQuery = None) -> None:
    state.coordinator.local_put(key, await request.body(), ttl)


@internal_router.delete("/cache/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def internal_delete(key: str, state: StateDep) -> None:
    state.coordinator.local_delete(key)
