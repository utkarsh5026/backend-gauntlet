"""Scaffold smoke tests — proof the wiring is sound before any vertical exists.

These are deliberately *not* acceptance tests for V1-V6. They assert the plumbing
(the app boots, the protocol envelope is enforced, the store works, the counters
are `O(1)`) and they pin the scaffold's contract: every action that decides
something raises `NotImplementedError` until you build it.

When you implement a vertical, the matching test at the bottom of this file is
the first one that should fail — delete it then, and write the real acceptance
test the SPEC's Proof line names.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from sqs_queue.config import Settings
from sqs_queue.errors import (
    BatchEntryIdsNotDistinct,
    EmptyBatchRequest,
    InvalidParameterValue,
    QueueDeletedRecently,
    QueueDoesNotExist,
)
from sqs_queue.models import MessageState, QueueAttributes, QueueKind
from sqs_queue.protocol import Action, parse_batch_entries, parse_target
from sqs_queue.state import AppState, MessageCounts, Queue, QueueStore

# Mirrors the `call` fixture in conftest. Declared here rather than imported so
# the test modules stay importable on their own.
Call = Callable[..., Awaitable[httpx.Response]]


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_endpoint_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


# --- the protocol envelope: plumbing, so it works on the scaffold ------------


async def test_missing_target_is_refused(client: httpx.AsyncClient) -> None:
    """No `X-Amz-Target` means the caller did not name an action."""
    response = await client.post("/", json={})
    assert response.status_code == 400
    assert response.headers["x-amzn-errortype"] == "InvalidParameterValue"


async def test_unknown_action_is_refused(call: Call) -> None:
    response = await call("DefinitelyNotAnAction")
    assert response.status_code == 400
    assert response.json()["__type"] == "InvalidParameterValue"


def test_parse_target_accepts_the_real_shape() -> None:
    assert parse_target("AmazonSQS.SendMessage") is Action.SEND_MESSAGE


def test_batch_envelope_rules() -> None:
    """Structural batch checks — empty, oversized, duplicate ids."""
    with pytest.raises(EmptyBatchRequest):
        parse_batch_entries({"Entries": []}, 10)
    with pytest.raises(InvalidParameterValue):
        parse_batch_entries({"Entries": [{"Id": str(i)} for i in range(11)]}, 10)
    with pytest.raises(BatchEntryIdsNotDistinct):
        parse_batch_entries({"Entries": [{"Id": "a"}, {"Id": "a"}]}, 10)

    entries = parse_batch_entries({"Entries": [{"Id": "a"}, {"Id": "b"}]}, 10)
    assert [e.entry_id for e in entries] == ["a", "b"]


# --- the queue store: plumbing, so it works on the scaffold ------------------


def test_store_creates_and_resolves_queues(settings: Settings) -> None:
    store = QueueStore(settings)
    queue = store.create("orders", QueueAttributes())

    assert queue.kind is QueueKind.STANDARD
    assert store.get("orders") is queue
    assert store.get_by_url(queue.url) is queue
    assert queue.arn.endswith(":orders")

    with pytest.raises(QueueDoesNotExist):
        store.get("nope")


def test_fifo_ness_comes_from_the_name(settings: Settings) -> None:
    """The `.fifo` suffix is the contract, visible at every call site."""
    store = QueueStore(settings)
    assert store.create("orders.fifo", QueueAttributes()).is_fifo
    assert not store.create("orders", QueueAttributes()).is_fifo


def test_deleted_queue_name_is_reserved(settings: Settings) -> None:
    """A stale URL must not resolve into a *new* queue that reused the name."""
    store = QueueStore(settings)
    store.create("orders", QueueAttributes(), now=1000.0)
    store.delete("orders", now=1000.0)

    with pytest.raises(QueueDoesNotExist):
        store.get("orders")
    with pytest.raises(QueueDeletedRecently):
        store.create("orders", QueueAttributes(), now=1030.0)

    # Past the cooldown the name is free again.
    assert store.create("orders", QueueAttributes(), now=1100.0).name == "orders"


def test_counts_are_maintained_not_computed() -> None:
    """The three approximate gauges move by transition, never by walking messages."""
    counts = MessageCounts()
    counts.apply(None, MessageState.AVAILABLE)
    counts.apply(MessageState.AVAILABLE, MessageState.INFLIGHT)
    assert (counts.available, counts.inflight, counts.delayed) == (0, 1, 0)

    counts.apply(MessageState.INFLIGHT, MessageState.DELETED)
    assert (counts.available, counts.inflight) == (0, 0)


async def test_get_queue_url_and_list_are_wired(
    call: Call, make_queue: Callable[..., Queue]
) -> None:
    """Two lookups that are plumbing, so they work before any vertical exists."""
    queue = make_queue("orders")

    response = await call("GetQueueUrl", {"QueueName": "orders"})
    assert response.status_code == 200
    assert response.json() == {"QueueUrl": queue.url}

    listed = await call("ListQueues")
    assert listed.json() == {"QueueUrls": [queue.url]}


async def test_unknown_queue_is_a_400_not_a_404(call: Call) -> None:
    """The AWS JSON protocol's convention: the transport worked, the request was bad."""
    response = await call("GetQueueUrl", {"QueueName": "nope"})
    assert response.status_code == 400
    assert response.json()["__type"] == "QueueDoesNotExist"


def test_state_is_assembled_with_every_vertical(state: AppState) -> None:
    """The dependency graph in `build_state` actually built everything."""
    assert state.store is not None
    assert state.inflight is not None
    assert state.deadlines is not None
    assert state.waiters is not None
    assert state.groups is not None
    assert state.dedup is not None
    assert state.control is not None


# --- the worklist, pinned ----------------------------------------------------
#
# One per vertical. Each is the front door to its challenge; delete it when the
# real acceptance test replaces it.


async def test_create_queue_is_still_a_todo(call: Call) -> None:
    """V6 — idempotent creation is the interesting half."""
    with pytest.raises(NotImplementedError):
        await call("CreateQueue", {"QueueName": "orders"})


async def test_send_message_is_still_a_todo(call: Call, make_queue: Callable[..., Queue]) -> None:
    """V1/V4/V5 — the send path runs through dedup, groups and the wait set."""
    queue = make_queue("orders")
    with pytest.raises(NotImplementedError):
        await call("SendMessage", {"QueueUrl": queue.url, "MessageBody": "hello"})


async def test_receive_message_is_still_a_todo(
    call: Call, make_queue: Callable[..., Queue]
) -> None:
    """V1/V3/V4 — the receive path is where three verticals meet."""
    queue = make_queue("orders")
    with pytest.raises(NotImplementedError):
        await call("ReceiveMessage", {"QueueUrl": queue.url, "MaxNumberOfMessages": 1})


async def test_delete_message_is_still_a_todo(call: Call, make_queue: Callable[..., Queue]) -> None:
    """V1 — and the receipt handle is the whole point of this one."""
    queue = make_queue("orders")
    with pytest.raises(NotImplementedError):
        await call("DeleteMessage", {"QueueUrl": queue.url, "ReceiptHandle": "anything"})


async def test_change_visibility_validates_before_it_raises(
    call: Call, make_queue: Callable[..., Queue]
) -> None:
    """Parameter shape is checked at the edge, so this fails cleanly on the scaffold."""
    queue = make_queue("orders")
    response = await call(
        "ChangeMessageVisibility",
        {"QueueUrl": queue.url, "ReceiptHandle": "x", "VisibilityTimeout": "soon"},
    )
    assert response.status_code == 400
    assert response.json()["__type"] == "InvalidParameterValue"


def test_deadline_engine_is_still_a_todo(state: AppState) -> None:
    """V2 — one structure behind every "later" in the service."""
    with pytest.raises(NotImplementedError):
        state.deadlines.next_due_at()


def test_dedup_window_is_still_a_todo(state: AppState) -> None:
    """V5 — bounded memory is the criterion, not the lookup."""
    with pytest.raises(NotImplementedError):
        state.dedup.check("orders.fifo", "dedup-1", 0.0)


def test_group_index_is_still_a_todo(state: AppState) -> None:
    """V4 — ordering within a group, parallelism across them."""
    with pytest.raises(NotImplementedError):
        state.groups.selectable(10)


def test_wait_set_is_still_a_todo(state: AppState) -> None:
    """V3 — ten thousand parked waiters, and nothing burning."""
    with pytest.raises(NotImplementedError):
        state.waiters.waiter_count()
