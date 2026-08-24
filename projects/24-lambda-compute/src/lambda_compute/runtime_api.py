"""V1 — The Runtime API.

Everyone assumes Lambda *calls* your function. It does not. The runtime inside the
sandbox starts up and then **long-polls the platform** asking for work:

    GET  /2018-06-01/runtime/invocation/next          -> blocks until there is work
    POST /2018-06-01/runtime/invocation/{id}/response -> here is the result
    POST /2018-06-01/runtime/invocation/{id}/error    -> the handler raised

That inversion is why a custom runtime is a shell script with `curl` in a loop, why
init can be billed separately from invoke, and — the part that matters for V3 — why
the sandbox needs no inbound port and no credentials: it dials out, and the only
thing it can reach is this API.

Scaffold state: the router is wired to the real paths and header names, so a
runtime written against the real Runtime API will talk to it unmodified. The
**broker** — the thing that hands a pending invocation to exactly one poller and
matches the eventual response back to the caller waiting on it — is yours.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Header, Path, Request, Response

from .errors import InvalidRequestContent
from .models import FunctionName, Invocation, InvocationResult, RequestId

__all__ = ["InvocationBroker", "runtime_router"]

log = structlog.get_logger(__name__)

# The real header names. A runtime reads these off `/next` and expects nothing else.
HEADER_REQUEST_ID = "lambda-runtime-aws-request-id"
HEADER_DEADLINE_MS = "lambda-runtime-deadline-ms"
HEADER_FUNCTION_ARN = "lambda-runtime-invoked-function-arn"
HEADER_TRACE_ID = "lambda-runtime-trace-id"


class InvocationBroker:
    """The hand-off point between callers and polling runtimes.

    One of these per node. Two populations meet here and neither knows about the
    other: callers submitting invocations and blocking on a result, and runtimes
    long-polling for something to do.

    The whole vertical is in the invariants:

    * one invocation goes to **exactly one** poller — never two, never zero;
    * `next_invocation` **blocks** rather than returning empty, and holds no CPU
      while it does (the SPEC forbids a poll loop with a sleep in it);
    * a poller that dies mid-poll must not take the invocation with it;
    * a result posted for an unknown or already-completed id is **rejected**;
    * every submitted invocation ends *somehow* — a result, an error, or a
      deadline. Nothing may wait forever.
    """

    def __init__(self, *, max_pending: int = 10_000) -> None:
        self._max_pending = max_pending
        # TODO(V1): the broker's state. You need, at minimum:
        #
        #   * a queue of pending invocations PER FUNCTION — a runtime polling for
        #     `hello` must not be handed an invocation for `goodbye`;
        #   * a map of in-flight request id -> the waiter to complete when the
        #     result arrives (an `asyncio.Future` is the natural shape; the caller
        #     awaits it, the poster resolves it);
        #   * enough to answer "is this id one I actually handed out?", so a
        #     duplicate or forged response can be refused.
        #
        # Two traps worth knowing before you pick a shape:
        #
        #   * `asyncio.Queue.get()` is cancelled when its awaiting task is
        #     cancelled — which is exactly what happens when a polling runtime
        #     disconnects. If you pop the invocation and *then* get cancelled, that
        #     invocation is gone. Handle the cancellation, or don't remove it from
        #     the queue until it is safely handed over.
        #   * bound the pending queue. `max_pending` is here because an unbounded
        #     one turns a slow function into a memory leak.

    async def submit(self, invocation: Invocation) -> InvocationResult:
        """Queue an invocation and wait for its result. The caller's side."""
        # TODO(V1): enqueue for this function, then await the result. Enforce the
        # deadline HERE rather than trusting the runtime to self-police — a handler
        # in a tight loop will not cooperate, and V3 makes that concrete.
        raise NotImplementedError("V1: enqueue the invocation and await its result")

    async def next_invocation(self, function_name: FunctionName) -> Invocation:
        """Block until there is work for this function. The runtime's side."""
        # TODO(V1): wait for a pending invocation and hand it over. This must
        # BLOCK — a runtime with nothing to do holds one idle connection and burns
        # no CPU. If you find yourself writing `while True: ... await sleep(0.01)`,
        # that is the thing the SPEC is ruling out.
        raise NotImplementedError("V1: block until an invocation is available")

    def complete(self, request_id: RequestId, result: InvocationResult) -> None:
        """Resolve a waiting caller with the runtime's result."""
        # TODO(V1): look the id up, reject it if it is unknown or already
        # completed, and hand the result to whoever is waiting.
        raise NotImplementedError("V1: resolve the caller waiting on this request id")

    def pending_count(self, function_name: FunctionName) -> int:
        """Queued-but-not-yet-handed-out invocations — the async queue-depth metric."""
        raise NotImplementedError("V1: pending invocations for this function")


runtime_router = APIRouter(prefix="/2018-06-01/runtime")

# A runtime identifies itself with the environment id it was started as. In real
# Lambda the Runtime API listens per-sandbox, so the identity is implicit; here one
# listener serves every environment, so it is explicit — and V3's "an environment
# cannot poll another's queue" is a check you have to actually write.
EnvironmentHeader = Annotated[str | None, Header(alias="Lambda-Runtime-Environment-Id")]


def _broker(request: Request) -> InvocationBroker:
    """Pull the broker off the app. Set by the lifespan in `main`."""
    broker = getattr(request.app.state, "broker", None)
    if not isinstance(broker, InvocationBroker):  # pragma: no cover - startup invariant
        raise RuntimeError("runtime app state was not initialised")
    return broker


@runtime_router.get("/invocation/next")
async def next_invocation(request: Request, environment_id: EnvironmentHeader = None) -> Response:
    """Long-poll for the next invocation.

    Returns the payload as the body, with the request id and deadline as headers —
    exactly the shape the real API uses, because the point of mirroring it is that
    a real runtime works here unchanged.
    """
    if not environment_id:
        raise InvalidRequestContent("missing Lambda-Runtime-Environment-Id header")
    broker = _broker(request)

    # TODO(V1): resolve which function this environment belongs to, then
    # `await broker.next_invocation(...)` for it — a runtime polling for `hello`
    # must never be handed an invocation for `goodbye`. Until V2 owns environments
    # there is nothing to resolve it against, which is why this raises rather than
    # guessing. Return the payload as the body with the headers named above.
    _ = broker
    raise NotImplementedError("V1: resolve the environment's function, then await /next")


@runtime_router.post("/invocation/{request_id}/response", status_code=202)
async def post_response(
    request: Request,
    request_id: Annotated[RequestId, Path()],
    environment_id: EnvironmentHeader = None,
) -> dict[str, str]:
    """The handler returned. The body is its return value, carried unchanged."""
    if not environment_id:
        raise InvalidRequestContent("missing Lambda-Runtime-Environment-Id header")
    payload = await request.body()
    broker = _broker(request)

    # TODO(V1): build a SUCCESS `InvocationResult` carrying `payload` unchanged and
    # `broker.complete(request_id, result)`. Reject an id this environment was
    # never handed — that check is what stops one environment from answering
    # another's invocation.
    _ = (payload, broker)
    raise NotImplementedError("V1: complete the invocation with the handler's response")


@runtime_router.post("/invocation/{request_id}/error", status_code=202)
async def post_error(
    request: Request,
    request_id: Annotated[RequestId, Path()],
    environment_id: EnvironmentHeader = None,
) -> dict[str, str]:
    """The handler raised. The body is `{errorType, errorMessage, stackTrace}`."""
    if not environment_id:
        raise InvalidRequestContent("missing Lambda-Runtime-Environment-Id header")
    payload = await request.body()
    broker = _broker(request)

    # TODO(V1): parse `{errorType, errorMessage, stackTrace}` out of `payload`,
    # build a FUNCTION_ERROR result preserving the handler's own error type and
    # stack trace, and complete the caller. Note this is not a platform failure —
    # see the FunctionError docstring in `errors.py`.
    _ = (payload, broker)
    raise NotImplementedError("V1: complete the invocation with the handler's error")


@runtime_router.post("/init/error", status_code=202)
async def post_init_error(request: Request, environment_id: EnvironmentHeader = None) -> Response:
    """Init failed before the runtime ever polled — the environment is unusable.

    Distinct from an invocation error on purpose: V2 requires an init failure to
    discard the environment rather than reuse it.
    """
    if not environment_id:
        raise InvalidRequestContent("missing Lambda-Runtime-Environment-Id header")
    _ = await request.body()
    raise NotImplementedError("V2: mark the environment failed and discard it")
