"""V3 — The sandbox: a tenant you assume is hostile.

The function is arbitrary code from someone you do not trust, running on a kernel
it shares with the platform supervising it. Every guarantee below has to hold
against code actively trying to break it, not merely against code with a bug:

  * a real **process boundary** — a segfault fails one invocation, not the node;
  * a **memory ceiling that kills** rather than swapping the node to death;
  * a **timeout enforced from outside**, because a handler in a tight non-yielding
    loop will never cooperate with a timer living inside itself;
  * a `/tmp` that is shared across invocations on one environment and invisible to
    every other environment;
  * a network boundary: the sandbox can reach the Runtime API and nothing else —
    not the control plane, not another tenant, not the internet.

Scaffold state: the spec and the handle are modelled; spawning, limiting, killing
and cleaning up raise.

> **A note on honesty.** `docs/24-design.md` is required to record what your
> isolation does *not* protect against. Process isolation plus rlimits is a real
> boundary and a genuinely weaker one than the microVM the real service moved to —
> naming the gap is the deliverable, not apologising for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from .config import Settings
from .models import FunctionConfig

__all__ = ["Sandbox", "SandboxSpec", "spawn_sandbox"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """Everything the supervisor decided before the process existed.

    Frozen because these are the limits: a sandbox that can renegotiate its own
    ceiling is not a sandbox.
    """

    environment_id: str
    function: FunctionConfig
    memory_mb: int
    tmp_dir: Path
    # Handed in as AWS_LAMBDA_RUNTIME_API. This plus the function's own env vars
    # are the ONLY channels into the sandbox — V3 grades on the parent's
    # environment, cwd and open fds not leaking in.
    runtime_api_address: str

    @property
    def cpu_share(self) -> float:
        """CPU proportional to memory — the real service's model, worth keeping.

        It is why "my function is slow, let me lower the memory to save money"
        backfires: at 1769 MB a function gets a full vCPU, and below that it gets a
        fraction. Cutting memory cuts CPU, the duration grows, and the bill with it.
        """
        return min(1.0, self.memory_mb / 1769)


class Sandbox:
    """A handle on one running execution environment's process.

    Owns the process and everything attached to it. The invariant the SPEC's leak
    test enforces: after `destroy()`, nothing survives — no orphan process, no open
    fd, no `/tmp` directory. A few hundred create/destroy cycles must leave the
    node's process and fd counts flat.
    """

    def __init__(self, spec: SandboxSpec) -> None:
        self.spec = spec
        # TODO(V3): the process handle and whatever you need to reclaim it.
        # `asyncio.create_subprocess_exec` gives you a `Process`; you will also
        # want its pid (for the out-of-band kill) and its stdout/stderr readers.
        #
        # Capture stdout/stderr — the observability checklist requires function
        # output attributed to the right request id and BOUNDED. A handler in a
        # `while True: print(...)` loop must not fill the node's disk, so read
        # continuously into a capped buffer rather than letting a pipe block:
        # a full pipe buffer deadlocks the child, which looks exactly like a hang.

    async def start(self) -> None:
        """Spawn the process under its limits."""
        # TODO(V3): spawn the runtime shim as a child process. The interesting
        # parts, in rough order of how easy they are to get subtly wrong:
        #
        #   * ENV: pass ONLY the function's env plus the runtime API address.
        #     `subprocess` inherits the parent's environment by default — that is
        #     the leak V3 tests for.
        #   * MEMORY: `resource.setrlimit(RLIMIT_AS, ...)` in a `preexec_fn` is the
        #     portable-ish answer; cgroups v2 is the real one. Whichever you pick,
        #     the failure must be a KILL with a distinct error naming the limit,
        #     not a slow death by swapping.
        #   * FDs: close inherited descriptors (`close_fds=True` is the default and
        #     is load-bearing here).
        #   * CWD: start it in its own `/tmp`, not in the platform's directory.
        raise NotImplementedError("V3: spawn the sandbox process under its resource limits")

    async def wait_healthy(self, timeout: float) -> None:
        """Block until the runtime is up, or fail the environment."""
        # TODO(V3): wait for the runtime to signal readiness — in practice, for it
        # to poll `/next` for the first time. A process that dies during init must
        # surface as an EnvironmentFailure here rather than as a mysterious
        # invocation timeout later.
        raise NotImplementedError("V3: wait for the runtime to become ready")

    async def kill(self, *, reason: str) -> None:
        """Terminate the process now. Used for timeout and OOM."""
        # TODO(V3): SIGKILL, not SIGTERM. The whole point of the enforced-from-
        # outside timeout is that the handler is not cooperating — a signal it can
        # catch and ignore is not enforcement. Then reap the child so it does not
        # become a zombie, and make sure this is idempotent: the timeout path and
        # the OOM path can both fire for the same sandbox.
        raise NotImplementedError("V3: kill the sandbox process and reap it")

    async def destroy(self) -> None:
        """Kill the process and reclaim everything it held."""
        # TODO(V3): kill (if alive), await the process, close the pipes, remove
        # `tmp_dir` recursively. Removing the directory is what makes "another
        # environment cannot see this one's /tmp" true after the fact, too.
        raise NotImplementedError("V3: destroy the sandbox and reclaim its resources")

    @property
    def alive(self) -> bool:
        """Whether the process is still running."""
        raise NotImplementedError("V3: report whether the sandbox process is alive")


async def spawn_sandbox(
    settings: Settings, *, environment_id: str, function: FunctionConfig
) -> Sandbox:
    """Build a spec, create the scratch directory, and start a sandbox.

    The one entry point `environments.py` uses, so the isolation policy lives in
    exactly one place rather than being re-decided per caller.
    """
    # TODO(V3): create `settings.sandbox_root / environment_id` with restrictive
    # permissions (0o700 — another tenant must not be able to read it even if it
    # somehow gets a path), build the SandboxSpec, start it, and wait for health.
    # Clean up the directory if the start fails, or a failed cold start leaks a
    # directory every time it happens.
    raise NotImplementedError("V3: create the scratch dir and start a sandbox for this environment")
