"""The shared vocabulary: functions, invocations, results.

Plumbing, fully implemented — these are the nouns every vertical passes around,
and re-deriving them per module is how six modules end up with six subtly
different ideas of what "an invocation" is.

The one thing worth reading closely is `Invocation.payload`: it is `bytes`, not a
parsed object. The platform is not allowed to interpret a caller's payload — it
carries it. Parsing it here would break the SPEC's "byte for byte, not
re-serialized" criterion the first time a handler cared about key order or float
formatting.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "FunctionConfig",
    "FunctionName",
    "Invocation",
    "InvocationResult",
    "InvocationType",
    "Outcome",
    "RequestId",
]

FunctionName = str
RequestId = str


class InvocationType(StrEnum):
    """How the caller wants to wait, chosen by `X-Amz-Invocation-Type`."""

    REQUEST_RESPONSE = "RequestResponse"  # synchronous: the caller blocks
    EVENT = "Event"  # asynchronous: 202 now, executed later (V5)
    DRY_RUN = "DryRun"  # validate and check permissions, never execute


class Outcome(StrEnum):
    """How an invocation ended. The SPEC grades on telling these apart."""

    SUCCESS = "success"
    FUNCTION_ERROR = "function_error"  # the handler raised — not the platform's fault
    TIMED_OUT = "timed_out"
    THROTTLED = "throttled"  # capacity, not failure — counted separately
    ENVIRONMENT_FAILURE = "environment_failure"  # init blew up, OOM kill, process died


@dataclass(frozen=True, slots=True)
class FunctionConfig:
    """A registered function. Immutable: a change publishes a new version."""

    name: FunctionName
    handler: str
    memory_mb: int
    timeout_seconds: float
    environment: dict[str, str] = field(default_factory=dict[str, str])
    # None means "no reservation": this function draws from the shared account
    # pool, and can therefore be starved by a noisy neighbour (V4).
    reserved_concurrency: int | None = None
    provisioned_concurrency: int = 0

    @property
    def arn(self) -> str:
        """The identifier handed to the runtime in `Lambda-Runtime-Invoked-Function-Arn`."""
        return f"arn:aws:lambda:local:000000000000:function:{self.name}"


@dataclass(slots=True)
class Invocation:
    """One unit of work, from admission to result.

    Created by the control plane, handed to exactly one polling runtime by V1, and
    completed by that runtime posting a response or an error.
    """

    function: FunctionConfig
    payload: bytes
    invocation_type: InvocationType = InvocationType.REQUEST_RESPONSE
    request_id: RequestId = field(default_factory=lambda: str(uuid.uuid4()))
    # monotonic, so the deadline survives a wall-clock adjustment mid-invocation.
    created_at: float = field(default_factory=time.monotonic)
    # Which attempt this is; only the async path (V5) ever exceeds 1.
    attempt: int = 1

    @property
    def deadline(self) -> float:
        """The monotonic instant the handler must have responded by."""
        return self.created_at + self.function.timeout_seconds

    def remaining_seconds(self) -> float:
        """Time left before the deadline; the runtime surfaces this to the handler."""
        return max(0.0, self.deadline - time.monotonic())


@dataclass(slots=True)
class InvocationResult:
    """What the caller gets back.

    `payload` is again bytes — whatever the handler returned, unchanged. `cold` and
    the duration fields are the same numbers the real service's `REPORT` line
    carries, and the SPEC's observability section grades on them being real rather
    than estimated.
    """

    request_id: RequestId
    outcome: Outcome
    payload: bytes = b""
    cold: bool = False
    # Init is billed separately from execution, and only a cold invocation has one.
    init_duration_ms: float | None = None
    duration_ms: float = 0.0
    # Set when `outcome is FUNCTION_ERROR`: the handler's own type and trace.
    error_type: str | None = None
    stack_trace: list[str] = field(default_factory=list[str])

    @property
    def is_function_error(self) -> bool:
        return self.outcome is Outcome.FUNCTION_ERROR
