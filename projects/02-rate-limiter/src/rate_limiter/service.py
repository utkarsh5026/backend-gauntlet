"""The gRPC surface: a thin adapter between protobuf messages and the limiter.

Deliberately thin. Everything interesting is behind `RedisLimiter`; this file's
job is to unpack a request, hand it to the limiter, and turn the answer (or the
exception) back into something the wire understands. If logic starts
accumulating here, it belongs in a vertical module instead.

Wired and working as scaffolding: both RPCs validate, dispatch, and map errors to
the right status code. The `TODO(horizontal)` markers are checklist items from
the SPEC — deadlines, metrics, tracing — that you weave in as you go. They are
plain comments, not `NotImplementedError`, because the service *runs*: it is the
limiter underneath it that is still a worklist.
"""

from __future__ import annotations

import grpc
import grpc.aio
import structlog

from .errors import AppError, Internal, abort
from .limiter import Decision
from .pb import ratelimit_pb2 as pb
from .pb import ratelimit_pb2_grpc as rpc
from .redis_limiter import RedisLimiter

__all__ = ["RateLimiterService", "to_response"]

log = structlog.get_logger(__name__)

# `type` statements (PEP 695), not plain assignment: a bare `X = SomeType[...]`
# is a *variable* as far as a type checker is concerned, and using it in an
# annotation is an error under strict mode.
type CheckContext = grpc.aio.ServicerContext[pb.CheckRequest, pb.CheckResponse]
type PeekContext = grpc.aio.ServicerContext[pb.PeekRequest, pb.CheckResponse]


def to_response(decision: Decision) -> pb.CheckResponse:
    """Map an internal `Decision` onto the wire message."""
    return pb.CheckResponse(
        allowed=decision.allowed,
        remaining=decision.remaining,
        limit=decision.limit,
        # Milliseconds on the wire: the proto says so, and an integer count of
        # milliseconds survives every language's number handling intact.
        retry_after_ms=int(decision.retry_after * 1000),
    )


class RateLimiterService(rpc.RateLimiterServicer):
    """Implements `ratelimit.v1.RateLimiter`."""

    def __init__(self, limiter: RedisLimiter) -> None:
        self._limiter = limiter

    async def Check(  # noqa: N802 - the method name comes from the .proto
        self,
        request: pb.CheckRequest,
        context: CheckContext,
    ) -> pb.CheckResponse:
        """Account for one request against `request.key` and return the verdict."""
        # proto3 has no optional scalars: an unset `cost` arrives as 0, which the
        # contract defines as "one unit".
        cost = request.cost or 1

        # TODO(horizontal/protocols): honour the client's deadline.
        # `context.time_remaining()` is the seconds left (None if the client set
        # no deadline). If there isn't time to beat it, failing immediately with
        # DEADLINE_EXCEEDED is strictly better than doing the Redis round-trip
        # and answering into a socket nobody is reading — that is how a limiter
        # under load becomes the outage it was meant to prevent.

        # TODO(horizontal/observability): a span + structured log line per Check,
        # with the key **hashed or truncated** (never the raw key — it is an API
        # key or a user id), the decision, the remaining budget, and the backend
        # latency. Plus the counters: allowed vs denied, Redis errors, script
        # cache hits; and a histogram of decision latency, since p99 on this hot
        # path is the number the whole project is graded on.
        try:
            decision = await self._limiter.check(request.key, cost)
        except AppError as exc:
            await abort(context, exc)
        except NotImplementedError:
            # The scaffold's own state: let it surface untouched so the worklist
            # is obvious, rather than dressing it up as an internal error.
            raise
        except Exception as exc:  # noqa: BLE001 - the last line before the wire
            await abort(context, Internal(str(exc)))
        return to_response(decision)

    async def Peek(  # noqa: N802 - the method name comes from the .proto
        self,
        request: pb.PeekRequest,
        context: PeekContext,
    ) -> pb.CheckResponse:
        """Report `request.key`'s state without consuming any budget."""
        try:
            decision = await self._limiter.peek(request.key)
        except AppError as exc:
            await abort(context, exc)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - the last line before the wire
            await abort(context, Internal(str(exc)))
        return to_response(decision)
