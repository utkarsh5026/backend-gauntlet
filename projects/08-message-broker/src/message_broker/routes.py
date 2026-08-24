"""HTTP surface: produce, fetch, topic admin, and consumer-group offsets.

The routing, the request/response shapes, and the size/validation guards are
wired. What the handlers call into — `topic.produce` (V3 -> V1),
`partition.read_from` (V1 -> V2), `groups.commit`/`join` (V4) — is where the
`NotImplementedError`s live. Run as-is and `GET /healthz`, `GET /metrics`,
`POST /topics` and `GET /topics` all work; the first real produce, fetch or join
raises, which is the worklist.

**Wire format.** Record keys and values are carried as UTF-8 strings over JSON,
which is simple to `curl` and lossy for anything that is not text — a value round
trips through `str.encode()` on the way in and `bytes.decode(errors="replace")`
on the way out, so arbitrary bytes do *not* survive. Fixing that (base64, or a
length-prefixed binary protocol over raw TCP) is the "wire format documented"
horizontal item; whichever you choose, write it down.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from .errors import RecordTooLarge
from .record import Record
from .state import AppState

__all__ = ["router"]

# A fetch must never be askable for the whole log — that is a graded criterion,
# and an unbounded batch is how one client OOMs a broker for everybody.
DEFAULT_MAX_RECORDS = 100
MAX_MAX_RECORDS = 1000

# A key becomes a hash input and travels in every response. It has no business
# being large.
MAX_KEY_BYTES = 1024


def get_state(request: Request) -> AppState:
    """Pull the assembled runtime off the app. Set by the lifespan in `main`."""
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]

router = APIRouter()


# --- topic admin --------------------------------------------------------------


class CreateTopicRequest(BaseModel):
    name: str
    partitions: int | None = Field(default=None, ge=1)
    """`None` uses the broker's configured default. Fixed at create time: raising
    it later would remap every key (V3)."""


class TopicInfo(BaseModel):
    name: str
    partitions: int


class TopicList(BaseModel):
    topics: list[TopicInfo]


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/topics", status_code=status.HTTP_201_CREATED)
async def create_topic(body: CreateTopicRequest, state: StateDep) -> TopicInfo:
    """Create a topic and its N partition logs (V3).

    TODO(security): require the write credential here — topic creation makes
    directories on the broker's disk.
    """
    topic = await state.broker.create_topic(body.name, body.partitions)
    return TopicInfo(name=topic.name, partitions=topic.partition_count)


@router.get("/topics")
async def list_topics(state: StateDep) -> TopicList:
    return TopicList(
        topics=[
            TopicInfo(name=t.name, partitions=t.partition_count) for t in state.broker.list_topics()
        ]
    )


# --- produce ------------------------------------------------------------------


class RecordIn(BaseModel):
    value: str
    key: str | None = None
    """Optional partition key (V3): same key -> same partition."""
    timestamp: int | None = None
    """Producer timestamp (epoch millis); stamped now if absent."""


class ProduceRequest(BaseModel):
    records: list[RecordIn] = Field(min_length=1)
    """A *batch*. One record per request would pay the HTTP round trip for every
    message, which is why real producers batch and why this is graded."""


class ProduceResult(BaseModel):
    partition: int
    offset: int


class ProduceResponse(BaseModel):
    results: list[ProduceResult]


@router.post("/topics/{topic_name}/records")
async def produce(topic_name: str, body: ProduceRequest, state: StateDep) -> ProduceResponse:
    """Produce a batch (V3 partitioner -> V1 append).

    TODO(security): authenticate this before doing anything. An open produce
    endpoint is an open disk for the whole internet.
    """
    topic = state.broker.topic(topic_name)
    now_ms = int(time.time() * 1000)

    results: list[ProduceResult] = []
    for item in body.records:
        value = item.value.encode()
        key = item.key.encode() if item.key is not None else None
        # Enforce the per-record caps before anything touches the disk.
        if len(value) > state.max_record_bytes:
            raise RecordTooLarge()
        if key is not None and len(key) > MAX_KEY_BYTES:
            raise RecordTooLarge(f"key must be <= {MAX_KEY_BYTES} bytes")
        record = Record(
            value=value,
            key=key,
            timestamp_ms=item.timestamp if item.timestamp is not None else now_ms,
        )
        partition, offset = await topic.produce(record)
        results.append(ProduceResult(partition=partition, offset=offset))

    return ProduceResponse(results=results)


# --- fetch --------------------------------------------------------------------


class RecordOut(BaseModel):
    offset: int
    timestamp: int
    key: str | None
    value: str


class FetchResponse(BaseModel):
    records: list[RecordOut]
    next_offset: int
    """Where to continue from. The client never computes this itself — that is
    how a consumer skips or replays a record by accident."""


OffsetQuery = Annotated[int, Query(ge=0, description="First offset to read.")]
MaxRecordsQuery = Annotated[int, Query(ge=1, le=MAX_MAX_RECORDS)]


@router.get("/topics/{topic_name}/partitions/{partition_id}/records")
async def fetch(
    topic_name: str,
    partition_id: int,
    state: StateDep,
    offset: OffsetQuery = 0,
    max_records: MaxRecordsQuery = DEFAULT_MAX_RECORDS,
) -> FetchResponse:
    """Fetch a bounded batch starting at `offset` (V1 read + V2 seek)."""
    partition = state.broker.topic(topic_name).partition(partition_id)
    records = await partition.read_from(offset, max_records)

    # Continue from just past the last returned record; an empty batch means the
    # consumer has caught up to the log end, so it stays put and polls again.
    next_offset = records[-1].offset + 1 if records else offset

    return FetchResponse(
        records=[
            RecordOut(
                offset=r.offset,
                timestamp=r.timestamp_ms,
                key=r.key.decode(errors="replace") if r.key is not None else None,
                value=r.value.decode(errors="replace"),
            )
            for r in records
        ],
        next_offset=next_offset,
    )


# --- consumer groups (V4) -----------------------------------------------------


class JoinRequest(BaseModel):
    member_id: str
    topic: str


class JoinResponse(BaseModel):
    member_id: str
    assignment: list[int]


class CommitRequest(BaseModel):
    topic: str
    partition: int = Field(ge=0)
    offset: int = Field(ge=0)


class CommittedResponse(BaseModel):
    group: str
    topic: str
    partition: int
    committed: int | None
    """`None` means this group has never committed here — distinct from 0, which
    means it committed at the very beginning."""


@router.post("/groups/{group}/members")
async def join_group(group: str, body: JoinRequest, state: StateDep) -> JoinResponse:
    """Join a group; returns the partitions this member should now own."""
    topic = state.broker.topic(body.topic)
    assignment = await state.broker.groups.join(
        group, body.member_id, body.topic, topic.partition_count
    )
    return JoinResponse(member_id=body.member_id, assignment=list(assignment.partitions))


@router.delete("/groups/{group}/members/{member}", status_code=status.HTTP_204_NO_CONTENT)
async def leave_group(group: str, member: str, state: StateDep) -> Response:
    """Leave a group, triggering a rebalance across the remaining members."""
    await state.broker.groups.leave(group, member)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/groups/{group}/offsets", status_code=status.HTTP_204_NO_CONTENT)
async def commit_offset(group: str, body: CommitRequest, state: StateDep) -> Response:
    """Durably commit the group's progress (V4).

    A consumer calls this *after* processing. That ordering is the at-least-once
    guarantee — see `group.py`.
    """
    await state.broker.groups.commit(group, body.topic, body.partition, body.offset)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/groups/{group}/offsets")
async def fetch_offset(
    group: str,
    state: StateDep,
    topic: Annotated[str, Query()],
    partition: Annotated[int, Query(ge=0)],
) -> CommittedResponse:
    """The group's committed offset for one partition."""
    committed = await state.broker.groups.committed(group, topic, partition)
    return CommittedResponse(group=group, topic=topic, partition=partition, committed=committed)
