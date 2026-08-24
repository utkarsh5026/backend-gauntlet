"""HTTP surface: index documents, search, refresh, merge, and read stats.

The routing, request and response shapes, and the input guards are wired. What
the handlers call into — `add_document`/`bulk` (V1→V2), `search` (V1→V5→V3),
`delete`/`force_merge` (V4) — is where the work lives. Run as-is and
`GET /healthz`, `GET /_stats`, `POST /_refresh` and `POST /_forcemerge` all work;
the first real index, search or delete raises a `NotImplementedError` and that
message is the worklist.

Document text is carried as UTF-8 over JSON. `_bulk` is newline-delimited JSON,
one document per line, like Elasticsearch's `_bulk`.

The JSON shapes here are a contract with `web/`, which is a pure client: field
names and nesting must stay exactly as they are, or the dashboard breaks.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from pydantic import ValidationError

from .doc import NewDocument
from .errors import BadRequest, NotFound
from .shard import EngineStats
from .state import AppState

__all__ = ["router"]

MAX_ID_LEN = 512
"""An external id also becomes a URL path segment, so it stays short and boring."""


def get_state(request: Request) -> AppState:
    """Pull the assembled runtime off the app. Set by the lifespan in `main`."""
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --- indexing -----------------------------------------------------------------
# TODO(security): `POST /documents`, `POST /_bulk`, `DELETE /documents/{id}`,
# `POST /_refresh` and `POST /_forcemerge` must all require a valid key from
# `settings.api_keys` before the handler runs. Search stays public — reads do not
# mutate the index.
#
# A FastAPI dependency is the right vehicle: one `Depends(require_api_key)` on
# the router covers every write route at once, and it runs *before* the handler,
# which is the criterion. Two things to get right — compare with
# `hmac.compare_digest`, not `==`, so the comparison time does not leak the
# prefix; and make sure the key never reaches a log line or an error body,
# including the FastAPI validation errors that echo inputs back by default.


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def index_document(state: StateDep, new: NewDocument) -> dict[str, int]:
    """Index one document (V1 analyze → buffered until the next refresh)."""
    if not new.text.strip():
        raise BadRequest("document text is empty")
    if new.id is not None and (not new.id or len(new.id) > MAX_ID_LEN):
        raise BadRequest(f"document id must be 1..={MAX_ID_LEN} characters")
    shard, doc_id = state.engine.add_document(new)
    return {"shard": shard, "doc_id": doc_id}


@router.post("/_bulk")
async def bulk(
    state: StateDep, body: Annotated[bytes, Body(media_type="application/x-ndjson")]
) -> dict[str, int]:
    """Index many documents, one JSON object per line (NDJSON)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadRequest("bulk body is not valid UTF-8") from exc

    docs: list[NewDocument] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            docs.append(NewDocument.model_validate_json(line))
        except ValidationError as exc:
            # `exc` echoes the offending input; only the line number goes out.
            raise BadRequest(f"bad document on line {lineno}") from exc

    return {"indexed": state.engine.bulk(docs)}


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(doc_id: str, state: StateDep) -> Response:
    """Tombstone a document by its external id (V4)."""
    if not await state.engine.delete(doc_id):
        raise NotFound()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- search -------------------------------------------------------------------


@router.get("/search")
async def search(
    state: StateDep,
    q: Annotated[str, Query(description="The query text, analyzed exactly like a document.")],
    size: Annotated[int, Query(ge=1, le=1000, description="How many hits to return.")] = 10,
) -> dict[str, Any]:
    """Rank documents for a query (V1 → V5 fan-out → V3 score). Public: reads do
    not mutate the index.

    `size` is bounded by `Query(le=1000)` at the edge, so a client can never ask
    for the whole corpus — the SPEC's bounded-result-set criterion, enforced
    before any handler code runs.

    `perf_counter` rather than `time.time` for the elapsed measurement: the
    latter is a wall clock and can step backwards under NTP, which would report a
    negative duration into the number the boss fight grades.
    """
    started = time.perf_counter()
    hits = await state.engine.search(q, size)
    return {
        "took_ms": round((time.perf_counter() - started) * 1000, 3),
        "total": len(hits),
        "hits": [hit.model_dump(exclude_none=True) for hit in hits],
    }


# --- admin --------------------------------------------------------------------


@router.post("/_refresh")
async def refresh(state: StateDep) -> dict[str, int]:
    """Flush buffered documents into segments so they become searchable (V2)."""
    return {"refreshed": await state.engine.refresh_all()}


@router.post("/_forcemerge")
async def force_merge(state: StateDep) -> dict[str, int]:
    """Compact every shard to a single segment, dropping tombstoned docs (V4)."""
    return {"merged_segments": await state.engine.force_merge()}


@router.get("/_stats")
async def stats(state: StateDep) -> EngineStats:
    """Per-shard and aggregate index stats. Fully wired, so it works on the bare
    scaffold — which makes it the endpoint that shows you a refresh really did
    create a segment."""
    return state.engine.stats()
