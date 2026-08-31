"""V4 — Health checking & circuit breaking.

Module: `src/api_gateway/health.py`. A dead backend you keep calling is worse
than no backend at all: every request to it waits the full upstream deadline, and
while it waits it holds a connection, a task, and a slot in whatever concurrency
limit you set. Enough of those and the *gateway* is down, not just the backend.
Two mechanisms stop that:

  * a **circuit breaker** per backend (`CLOSED -> OPEN -> HALF_OPEN`) so a run of
    failures makes calls fail fast instead of each paying the timeout, and
  * an **active health checker** that probes every backend on an interval and
    ejects a dead one / re-admits a recovered one with no restart.

The scaffold gives you the state enum, the breaker's shape, and the interface the
balancer (V3) and proxy (V1) already call — `allow()`, `record_success()`,
`record_failure()`. The state machine and the probe loop are yours.

## Two things this file must get right in Python

**Use a monotonic clock.** Every timing decision here — has the cooldown elapsed?
how long since the last failure? — must come from `time.monotonic()`, never
`time.time()`. A wall clock can jump backwards (NTP correction, a VM resuming
from a snapshot), and a breaker that computes a negative elapsed time will either
refuse to ever close or spring open at the worst moment. Monotonic time only ever
moves forward, which is the only property this file actually needs.

**You do not need a lock, and you should not reach for one.** Rust used atomics
here because several OS threads genuinely race. This gateway runs one event loop:
handlers are coroutines interleaved at `await` points, so a sequence of plain
attribute reads and writes with **no `await` between them** cannot be interrupted.
`self._failures += 1` is safe; `self._failures += 1` with an `await` in the middle
of the decision is not. Keep every state transition synchronous and the whole
breaker is race-free without a single `asyncio.Lock` — which matters because
`allow()` is called on the hot path of every request, and a lock there would cost
a scheduler round-trip per call.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoids the import cycle router -> balancer -> health
    import httpx

    from .router import Router

__all__ = ["CircuitBreaker", "CircuitState", "HealthChecker"]


class CircuitState(StrEnum):
    """The three circuit-breaker states.

    A `StrEnum` rather than a plain `Enum` so the value drops straight into a log
    line, a JSON body, or a Prometheus label with no conversion — this state is
    graded as *observable* in V4, and a type that stringifies to `CircuitState.OPEN`
    makes that harder than it needs to be.
    """

    CLOSED = "closed"
    """Traffic flows normally; failures are counted."""

    OPEN = "open"
    """Tripped: calls fail fast without touching the backend."""

    HALF_OPEN = "half_open"
    """Cooldown elapsed: a limited number of trial calls may pass through to test recovery."""


class CircuitBreaker:
    """A per-backend circuit breaker.

    One of these hangs off every `Backend`. The balancer consults `allow()` before
    selecting (V3) and the proxy reports the outcome of every upstream call back
    into `record_success` / `record_failure` (V1), which is what feeds the machine.

    The two constructor arguments are the pair of numbers V4 asks you to justify
    in `docs/10-design.md`, and they trade off against each other: a low threshold
    trips on a blip, a high one keeps feeding a corpse; a short cooldown flaps, a
    long one leaves a recovered backend idle. You will want more state than this
    to implement the machine — a failure run length, the instant it opened, how
    many half-open trials are outstanding — and adding those fields is part of the
    work.
    """

    def __init__(self, failure_threshold: int = 5, open_cooldown: float = 5.0) -> None:
        self.failure_threshold = failure_threshold
        """Consecutive failures that trip the breaker from closed to open."""

        self.open_cooldown = open_cooldown
        """Seconds to stay open before admitting a half-open trial request."""

        self._state = CircuitState.CLOSED

    @property
    def state(self) -> CircuitState:
        """The current state, for `/metrics` and structured logs.

        Cheap and side-effect-free on purpose: a metrics scrape must be able to
        read this without nudging the machine. The *timed* transition
        (open -> half-open once the cooldown elapses) belongs in `allow()`, which
        is on the request path and is allowed to have effects.
        """
        return self._state

    def allow(self) -> bool:
        """May a request go to this backend right now?

        Called by `Balancer.pick` (V3) before selecting a backend, and on the proxy
        hot path (V1). Synchronous and non-blocking by design — see the module
        docstring on why that is what makes it race-free.

        TODO(V4): implement the read side of the machine —
          * `CLOSED` -> `True`;
          * `OPEN` -> `False`, *unless* `time.monotonic()` says `open_cooldown`
            has elapsed since it opened, in which case transition to `HALF_OPEN`
            and let this one through;
          * `HALF_OPEN` -> `True` for a **limited** number of concurrent trials,
            `False` for the rest. This cap is the whole point of half-open: if
            every waiting request stampedes a backend that just came back, you
            knock it over again and re-open the circuit. Track how many trials are
            outstanding and hand out at most that many permits.
        """
        raise NotImplementedError("V4: gate requests on the circuit state (fail fast when open)")

    def record_success(self) -> None:
        """Record a successful upstream call.

        TODO(V4): reset the failure run when closed; in `HALF_OPEN`, enough
        successes should **close** the circuit and return the backend to rotation.
        """
        raise NotImplementedError("V4: count success; close the circuit from half-open")

    def record_failure(self) -> None:
        """Record a failed or timed-out upstream call.

        TODO(V4): past `failure_threshold` consecutive failures, **open** the
        circuit and stamp the instant with `time.monotonic()`. A failure while
        `HALF_OPEN` must re-open it immediately — the trial answered the question.

        This is also the passive-outlier half of V4: the proxy calls this on live
        traffic, so a backend can be pulled from rotation between active probes.
        """
        raise NotImplementedError("V4: count failure; open the circuit past the threshold")


class HealthChecker:
    """The background active health checker.

    Probes every backend on an interval and flips its circuit so the balancer
    stops selecting a dead one and starts selecting a recovered one, with no
    restart and no operator involved. Constructed in `main.py`'s lifespan and
    spawned as an `asyncio.Task` — see the `TODO(V4)` there.

    **The concurrency shape matters.** The probe round must be concurrent
    (`asyncio.gather(*probes, return_exceptions=True)`), not a `for` loop of
    `await`s: sequentially probing 30 backends that each take up to the probe
    timeout makes a "2-second" interval take 60 seconds, and a health checker that
    lags the failure it is meant to detect is decoration. `return_exceptions=True`
    is what keeps one unreachable backend from cancelling the whole round — an
    exception *is* the answer for that backend, not an error in the checker.

    **The loop must also be cancellable.** The lifespan cancels this task on
    shutdown, which raises `asyncio.CancelledError` inside whatever it is
    awaiting. Let it propagate: catching it broadly (`except Exception` is fine,
    `except BaseException` is not) is what turns a clean `docker stop` into a
    ten-second wait for the kill.
    """

    def __init__(self, router: Router, client: httpx.AsyncClient, interval: float) -> None:
        self.router = router
        self.client = client
        self.interval = interval
        """Seconds between probe rounds. The floor on how long a dead backend keeps
        receiving traffic, so it is a real availability number, not a knob."""

    async def run(self) -> None:
        """The probe loop.

        TODO(V4): every `self.interval` seconds, probe each backend in
        `self.router.backends()` concurrently and feed the outcome into its
        breaker (`record_success` / `record_failure`), so that a dead one stops
        being selected by `Balancer.pick` and a recovered one comes back.

        Points worth deciding deliberately, because they are what the SPEC grades:
          * **Probe timeout.** Bound each probe well under `self.interval`, or a
            round overruns the next tick and the interval stops meaning anything.
          * **What counts as healthy.** A TCP connect? A `GET /healthz`? Any status
            under 500? Write the answer in `docs/10-design.md` — "the backend
            answered 404 quickly" is a judgement call, not an obvious truth.
          * **Log the transitions**, not every probe. V4 grades circuit changes as
            observable; a line per backend per two seconds is noise that will bury
            the one line you needed.
        """
        raise NotImplementedError(
            "V4: probe every backend on an interval; eject dead, re-admit recovered"
        )
