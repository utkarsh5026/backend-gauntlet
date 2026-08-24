"""V2 — Execution environments: the cold start as a lifecycle.

An execution environment has three phases, and both your bill and your weirdest
bug reports follow them:

    INIT     once per environment — imports, globals, connection pools
    INVOKE   many times, one at a time
    FREEZE   the microsecond your handler returns

**Freeze is the one that surprises people.** It is not "the process idles" — the
clock stops. A background task you fired off but did not await makes no progress
between invocations; it resumes, if ever, in the middle of a *later* request
belonging to a different caller. That single fact is why module-level state is
simultaneously the best optimisation available to a Lambda function and the source
of bugs that reproduce only under load.

Reuse is also why your warm p50 is 2ms and your cold p99 is 800ms — the same code,
a different phase.

Scaffold state: the states and the pool are modelled; the lifecycle transitions and
the acquire/release policy raise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from .config import Settings
from .models import FunctionConfig, FunctionName

__all__ = ["EnvironmentPool", "EnvironmentState", "ExecutionEnvironment"]

log = structlog.get_logger(__name__)


class EnvironmentState(StrEnum):
    """Where an environment is in its lifecycle.

    `FROZEN` and `IDLE` are deliberately the same state seen from two sides: the
    platform calls it idle (a candidate for reuse or reaping), the function
    experiences it as frozen (nothing runs). Keeping one name for it is a lie that
    makes the freeze semantics harder to reason about, so there are two.
    """

    INITIALISING = "initialising"
    IDLE = "idle"  # warm, available, frozen from the function's point of view
    BUSY = "busy"  # serving exactly one invocation
    FAILED = "failed"  # init blew up or the sandbox died — never reuse
    REAPED = "reaped"


@dataclass(slots=True)
class ExecutionEnvironment:
    """One sandboxed runtime, reusable across invocations of one function.

    The `state` field is not decoration: V2 grades on "exactly one invocation at a
    time", and this is where that is enforced.
    """

    environment_id: str
    function: FunctionConfig
    state: EnvironmentState = EnvironmentState.INITIALISING
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    invocation_count: int = 0
    # Measured once, during init, and reported on the first (cold) invocation only.
    init_duration_ms: float | None = None
    # Set for an environment kept warm by provisioned concurrency (V4): it is
    # pre-initialised, so its first real invocation is not a cold start.
    provisioned: bool = False

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used_at

    @property
    def is_reusable(self) -> bool:
        return self.state is EnvironmentState.IDLE


class EnvironmentPool:
    """The warm fleet: create, reuse, freeze, reap.

    This is the object that decides whether an invocation is cold or warm, which
    makes it the object that decides your p99. It is also bounded — `MAX_ENVIRONMENTS`
    exists because an unbounded warm pool is just a memory leak with good intentions.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._by_function: dict[FunctionName, list[ExecutionEnvironment]] = {}
        # TODO(V2): whatever you need to hand out an idle environment without two
        # callers getting the same one. Note the interesting race: "find an idle
        # environment" and "mark it busy" must be one atomic step, or under
        # concurrency two invocations land on one environment and V2's
        # one-at-a-time criterion fails intermittently — the worst kind of failure.

    async def acquire(self, function: FunctionConfig) -> tuple[ExecutionEnvironment, bool]:
        """Get an environment to run one invocation on. Returns `(env, cold)`.

        The heart of the vertical. Reuse an idle one if there is one (warm); create
        and initialise a new one if there is not (cold).
        """
        # TODO(V2): reuse-or-create. Things the SPEC will check:
        #
        #   * `cold` is reported from what actually happened, not guessed from a
        #     duration threshold;
        #   * init runs EXACTLY ONCE per environment, and `init_duration_ms` is
        #     measured around it;
        #   * an init that exceeds `init_timeout_seconds` leaves the environment
        #     FAILED and discarded, never IDLE;
        #   * the pool respects `max_environments` — decide deliberately what
        #     happens at the ceiling (wait? throttle? reap something?) and write it
        #     down, because that choice IS the node's overload behaviour.
        raise NotImplementedError("V2: reuse an idle environment or create and initialise one")

    async def release(self, environment: ExecutionEnvironment, *, healthy: bool) -> None:
        """Hand an environment back after an invocation. Freeze it, or retire it.

        `healthy` is the judgement call V2 grades: a handler that raised is fine —
        reuse it, and the next invocation must not see its mess. A sandbox that was
        OOM-killed, timed out mid-write, or exited is not fine; retire it.
        """
        # TODO(V2): freeze (state -> IDLE, stamp last_used_at) when healthy;
        # otherwise mark FAILED and destroy it. Getting this wrong in the generous
        # direction — reusing a corrupted environment — is how one bad invocation
        # poisons every one after it.
        raise NotImplementedError("V2: freeze a healthy environment, retire a broken one")

    async def reap_idle(self) -> int:
        """Destroy environments idle past the TTL. Returns how many went.

        Called on a timer by the lifespan in `main`. Reaping is observable on
        purpose: the SPEC asks that an idle fleet visibly shrinks rather than
        pinning memory forever.
        """
        # TODO(V2): find IDLE environments past `environment_idle_ttl_seconds`,
        # destroy their sandboxes, drop them. Two things to be careful about: never
        # reap a BUSY environment, and never reap a provisioned one — the whole
        # point of provisioned concurrency is that it does not go cold.
        raise NotImplementedError("V2: reap environments idle past the TTL")

    async def shutdown(self) -> None:
        """Destroy every environment. Called on SIGTERM."""
        raise NotImplementedError("V2: destroy every environment")

    def stats(self) -> dict[str, int]:
        """Fleet counts by state — the source of the warm-pool metrics."""
        counts: dict[str, int] = {state.value: 0 for state in EnvironmentState}
        for environments in self._by_function.values():
            for environment in environments:
                counts[environment.state.value] += 1
        counts["total"] = sum(len(e) for e in self._by_function.values())
        return counts
