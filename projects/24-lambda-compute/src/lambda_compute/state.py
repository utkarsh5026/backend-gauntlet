"""The objects every handler needs, assembled once at startup.

Kept in its own module so `routes` can depend on the shape without importing
`main` (which imports `routes` — that would be a cycle).

Everything here is **plumbing**: registering a function and looking one up is not a
vertical. The interesting behaviour lives behind the objects this holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .async_invoke import AsyncInvocationQueue
from .concurrency import ConcurrencyGovernor
from .config import Settings
from .environments import EnvironmentPool
from .errors import ResourceConflict, ResourceNotFound
from .event_source import EventSourceMapping
from .models import FunctionConfig, FunctionName
from .runtime_api import InvocationBroker

__all__ = ["AppState", "FunctionRegistry"]


class FunctionRegistry:
    """The functions this node serves.

    Plumbing. Note that `register` is where a reservation is validated against the
    governor rather than merely stored — a registry that accepts a reservation the
    account cannot honour has already broken V4's guarantee before any invocation
    arrives.
    """

    def __init__(self, settings: Settings, governor: ConcurrencyGovernor) -> None:
        self._settings = settings
        self._governor = governor
        self._functions: dict[FunctionName, FunctionConfig] = {}

    def register(self, function: FunctionConfig) -> FunctionConfig:
        if function.name in self._functions:
            raise ResourceConflict(f"function {function.name!r} already exists")
        if function.reserved_concurrency is not None:
            self._governor.set_reserved(function.name, function.reserved_concurrency)
        self._functions[function.name] = function
        return function

    def get(self, name: FunctionName) -> FunctionConfig:
        try:
            return self._functions[name]
        except KeyError:
            raise ResourceNotFound(f"function {name!r} not found") from None

    def names(self) -> list[FunctionName]:
        return sorted(self._functions)

    def __len__(self) -> int:
        return len(self._functions)


@dataclass(slots=True)
class AppState:
    """Everything the control plane and the Runtime API share.

    Both ASGI apps hold a reference to the same instance — that is what lets a
    runtime polling on port 9002 be handed an invocation submitted on port 9001.
    """

    settings: Settings
    registry: FunctionRegistry
    governor: ConcurrencyGovernor
    pool: EnvironmentPool
    broker: InvocationBroker
    async_queue: AsyncInvocationQueue
    mappings: dict[str, EventSourceMapping] = field(default_factory=dict[str, EventSourceMapping])
