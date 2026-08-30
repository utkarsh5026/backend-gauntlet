"""HTTP surface: the ingest endpoint, the live SSE feed, and historical query.

The routing and body handling are wired; what the handlers call into is where the
`NotImplementedError`s live. Run as-is and `GET /healthz` + `GET /metrics` work,
`POST /ingest` raises the V1 parse todo, and `GET /stream` the V4 SSE todo. The
consumer side (rollup -> sink) is driven out of band by `pipeline.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from . import parse, sink, sse
from .errors import BadRequest, StoreUnavailable
from .model import RollupRow
from .state import AppState

__all__ = ["router"]


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
    """Liveness only: is the process up and serving?

    TODO(observability): add a separate `GET /readyz` that reports the broker and
    ClickHouse connections. Liveness and readiness answering the same question is
    how a dependency blip turns into an orchestrator restart loop — the process
    was fine, it just had nowhere to write.
    """
    return {"status": "ok"}


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(request: Request, state: StateDep) -> dict[str, int]:
    """Accept a line-protocol body, validate it, and publish it to the durable
    stream.

    `202 Accepted` is the honest code: the points are durably enqueued, not yet
    rolled up or stored.

    TODO(security): authenticate this (an API key) before publishing — an open
    `/ingest` lets anyone forge metrics or blow up your cardinality. Cap the body
    size *before* reading it too: `await request.body()` buffers the whole
    payload in memory, so an unbounded body is an unbounded allocation. The
    `content-length` header is the cheap pre-check; a streaming read with a
    running total is the honest one.
    """
    body = await request.body()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadRequest("body is not valid UTF-8") from exc

    # V1: parse here to reject malformed input early with a 400, rather than
    # letting a bad line into the durable stream.
    points = parse.parse(text)

    # The durable log holds the raw line as the source of truth; the consumer
    # re-parses it (see `pipeline.py`). Publishing the bytes keeps the wire
    # format authoritative — and means a parser fix can be replayed over history.
    await state.producer.publish(body)

    return {"accepted": len(points)}


@router.get("/stream")
async def stream(
    state: StateDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    """Server-Sent Events feed of closed rollup windows (V4).

    FastAPI reads `Last-Event-ID` straight off the request headers — that is the
    header a browser's `EventSource` replays automatically on reconnect, and
    honouring it is what makes a dropped connection a gap-free resume.
    """
    return await sse.stream(state.feed, last_event_id)


@router.get("/query")
async def query(
    state: StateDep,
    series: Annotated[int, Query(description="Series fingerprint (see V1).")],
    start: Annotated[int, Query(alias="from", description="Unix seconds, inclusive.")],
    end: Annotated[int, Query(alias="to", description="Unix seconds, exclusive.")],
) -> list[RollupRow]:
    """Historical rollups for one series over a time range (the V3 read path).

    This is the dashboard's initial paint, before the SSE stream takes over.
    """
    if state.clickhouse is None:
        raise StoreUnavailable("not connected to ClickHouse")
    if end <= start:
        raise BadRequest("`to` must be after `from`")

    try:
        start_at = datetime.fromtimestamp(start, tz=UTC)
        end_at = datetime.fromtimestamp(end, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise BadRequest("timestamp out of range") from exc

    # TODO(security): cap the span (and require auth) — an unbounded range is a
    # free full-table scan for any caller.
    return await sink.query_range(state.clickhouse, state.rollup_table, series, start_at, end_at)
