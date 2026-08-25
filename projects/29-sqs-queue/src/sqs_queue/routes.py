"""The wire surface: one endpoint, and the action dispatch behind it.

The AWS JSON protocol puts every action on `POST /` and names it in a header, so
this module is a dispatch table rather than a router full of paths. That is not a
shortcut — it is what makes the protocol checklist's real bar reachable, because
`boto3` sends exactly this and nothing else.

The routing, the envelope parsing and the sequencing are wired. Every step that
*decides* something calls into a vertical, which raises until you build it — so
the scaffold boots, serves `/healthz` and `/metrics`, refuses a malformed target
correctly, and stops at the first real question.

**On authentication.** `REQUIRE_SIGV4` is off by default so the scaffold is
pokeable with curl. The security checklist turns it on, and when it is on the
gate belongs at the very top of `dispatch` — before the target is resolved,
before a queue is looked up, before anything reveals whether a queue exists. An
authentication check placed after a lookup is an existence oracle for anyone who
can reach the port.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, Request

from .errors import InvalidParameterValue, MissingParameter
from .protocol import TARGET_HEADER, Action, parse_batch_entries, parse_target
from .state import AppState, Queue

__all__ = ["public_router"]

log = structlog.get_logger(__name__)


def get_state(request: Request) -> AppState:
    """Pull the assembled runtime off the app. Set by the lifespan in `main`."""
    state = getattr(request.app.state, "app_state", None)
    if not isinstance(state, AppState):  # pragma: no cover - startup invariant
        raise RuntimeError("app state was not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]

public_router = APIRouter()

Handler = Callable[[dict[str, Any], AppState], Awaitable[dict[str, Any]]]


def _require(body: dict[str, Any], key: str) -> str:
    """Read a required string parameter, or raise the protocol's own error."""
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise MissingParameter(f"{key} is required")
    return value


def _resolve_queue(body: dict[str, Any], state: AppState) -> Queue:
    """Every data-plane action names its queue by URL. Plumbing."""
    return state.store.get_by_url(_require(body, "QueueUrl"))


# --- control plane (V6) ------------------------------------------------------


async def _create_queue(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    name = _require(body, "QueueName")
    raw = body.get("Attributes")
    attributes = cast(dict[str, str], raw) if isinstance(raw, dict) else None

    # TODO(V6): validate the name (charset, length, the `.fifo` rule) before
    # anything else — it ends up in a URL and in every log line.
    # TODO(V6): normalize the attributes, then apply the idempotency rule:
    # an existing queue with identical attributes succeeds and returns the same
    # URL; with different attributes it is a `QueueNameExists` conflict.
    _ = (name, attributes, state)
    raise NotImplementedError("V6: create a queue idempotently")


async def _get_queue_url(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    """Name → URL. Plumbing, and complete: a lookup, not a decision."""
    queue = state.store.get(_require(body, "QueueName"))
    return {"QueueUrl": queue.url}


async def _list_queues(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    """Plumbing, and complete."""
    prefix = body.get("QueueNamePrefix")
    names = state.store.list_names(prefix if isinstance(prefix, str) else None)
    return {"QueueUrls": [state.store.get(n).url for n in names]}


async def _get_queue_attributes(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    # TODO(V6): render the attribute set on the wire, including the three
    # approximate counts and the age of the oldest message. They come from
    # `queue.counts` — maintained incrementally — never from a walk.
    _ = queue
    raise NotImplementedError("V6: report queue attributes")


async def _set_queue_attributes(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    raw = body.get("Attributes")
    if not isinstance(raw, dict):
        raise MissingParameter("Attributes is required")
    # TODO(V6): normalize and validate, then apply — including to messages
    # already in the queue, exactly where your application table says you should.
    _ = queue
    raise NotImplementedError("V6: set queue attributes")


async def _delete_queue(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    # TODO(V6): delete the queue and make sure nothing survives it — parked
    # waiters must be released, and scheduled deadlines for its messages must not
    # fire into a queue that is gone.
    _ = queue
    raise NotImplementedError("V6: delete a queue and everything hanging off it")


async def _purge_queue(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    # TODO(V6): drop every message. Note the question this forces: what happens
    # to messages currently in flight, whose handles are held by consumers that
    # are about to delete them?
    _ = queue
    raise NotImplementedError("V6: purge a queue")


# --- data plane (V1, V3, V4, V5) ---------------------------------------------


async def _send_message(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    message_body = _require(body, "MessageBody")

    # TODO: cap the body at the queue's `max_message_bytes` *before* doing
    # anything with it — hashing a body you are going to refuse is work an
    # attacker chose for you.
    # TODO(V4): a FIFO queue requires a `MessageGroupId`; refuse the send without
    # one rather than inventing a default group.
    # TODO(V5): consult the dedup window; a duplicate inside the window is a
    # success carrying the *original* message id, and nothing is enqueued.
    # TODO(V2): a message with a delay is `DELAYED`, not `AVAILABLE`, and its
    # transition is a scheduled deadline, not a check at receive time.
    # TODO(V3): after the message becomes available, notify the wait set — and
    # note that this call is what closes the lost-wakeup window, so where it goes
    # relative to the state change is the whole question.
    _ = (queue, message_body)
    raise NotImplementedError("V1/V4/V5: send a message")


async def _send_message_batch(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    entries = parse_batch_entries(body, state.settings.max_batch_entries)
    # TODO: send each entry independently and collect *both* outcomes. An entry
    # that fails validation belongs in `Failed` with its id and error code — the
    # response is a 200 either way. Failing the whole batch for one bad entry is
    # the mistake this API exists to avoid.
    _ = (queue, entries)
    raise NotImplementedError("V1: send a batch, with per-entry results")


async def _receive_message(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    # TODO: clamp `MaxNumberOfMessages` (1-10), `WaitTimeSeconds` (<= 20, default
    # from the queue) and `VisibilityTimeout` (<= 12h) here, at the edge.
    # TODO(V3): build the `ReceiveRequest` and park on the wait set.
    # TODO(V4): on a FIFO queue, only groups with nothing in flight are eligible.
    # TODO(V6): check the redrive policy before handing a message out — a message
    # that has hit `maxReceiveCount` goes to the DLQ instead of being delivered
    # one last time.
    # TODO(V1): mint a receipt handle per delivery. The consumer never gets a
    # bare message id it could delete by.
    _ = queue
    raise NotImplementedError("V1/V3/V4: receive messages")


async def _delete_message(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    handle = _require(body, "ReceiptHandle")
    # TODO(V1): delete by handle. Three outcomes, and only one of them may touch
    # a live delivery — see `DeleteOutcome`.
    # TODO(V4): a FIFO delete unblocks the message's group, which is what lets
    # the next message in that group be delivered.
    _ = (queue, handle)
    raise NotImplementedError("V1: delete a message by receipt handle")


async def _delete_message_batch(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    entries = parse_batch_entries(body, state.settings.max_batch_entries)
    _ = (queue, entries)
    raise NotImplementedError("V1: delete a batch, with per-entry results")


async def _change_message_visibility(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    handle = _require(body, "ReceiptHandle")
    timeout = body.get("VisibilityTimeout")
    if not isinstance(timeout, int | float):
        raise InvalidParameterValue("VisibilityTimeout must be a number")
    # TODO(V1): only the holder of the current handle may move the lease, and a
    # timeout of 0 hands the message straight back.
    _ = (queue, handle, float(timeout))
    raise NotImplementedError("V1: change a message's visibility timeout")


async def _change_message_visibility_batch(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    queue = _resolve_queue(body, state)
    entries = parse_batch_entries(body, state.settings.max_batch_entries)
    _ = (queue, entries)
    raise NotImplementedError("V1: change visibility for a batch, with per-entry results")


async def _start_message_move_task(body: dict[str, Any], state: AppState) -> dict[str, Any]:
    source = _require(body, "SourceArn")
    # TODO(V6): redrive from a DLQ back to its source. Think about what a redrive
    # resets (the receive count) and what it must not (the message id).
    _ = (source, state)
    raise NotImplementedError("V6: redrive messages out of a dead-letter queue")


HANDLERS: dict[Action, Handler] = {
    Action.CREATE_QUEUE: _create_queue,
    Action.GET_QUEUE_URL: _get_queue_url,
    Action.LIST_QUEUES: _list_queues,
    Action.GET_QUEUE_ATTRIBUTES: _get_queue_attributes,
    Action.SET_QUEUE_ATTRIBUTES: _set_queue_attributes,
    Action.DELETE_QUEUE: _delete_queue,
    Action.PURGE_QUEUE: _purge_queue,
    Action.SEND_MESSAGE: _send_message,
    Action.SEND_MESSAGE_BATCH: _send_message_batch,
    Action.RECEIVE_MESSAGE: _receive_message,
    Action.DELETE_MESSAGE: _delete_message,
    Action.DELETE_MESSAGE_BATCH: _delete_message_batch,
    Action.CHANGE_MESSAGE_VISIBILITY: _change_message_visibility,
    Action.CHANGE_MESSAGE_VISIBILITY_BATCH: _change_message_visibility_batch,
    Action.START_MESSAGE_MOVE_TASK: _start_message_move_task,
}


@public_router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Unauthenticated on purpose — a load balancer has no credentials."""
    return {"status": "ok"}


@public_router.post("/")
async def dispatch(request: Request, state: StateDep) -> dict[str, Any]:
    """The one endpoint. Resolve the action, parse the body, hand it over."""
    # TODO(security): when `require_sigv4` is on, authenticate here — first, before
    # the target is even resolved — and then authorize the action against the
    # queue it names. Sending and receiving are different permissions.
    if state.settings.require_sigv4:
        raise NotImplementedError("security: SigV4 verification via project 25")

    action = parse_target(request.headers.get(TARGET_HEADER))
    raw: object = await request.json() if await request.body() else {}
    if not isinstance(raw, dict):
        raise InvalidParameterValue("request body must be a JSON object")
    body = cast(dict[str, Any], raw)

    log.debug("dispatch", action=str(action))
    return await HANDLERS[action](body, state)
