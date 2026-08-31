"""One endpoint, and a header that picks the verb.

Modern AWS services expose a single `POST /` and select the operation with
`X-Amz-Target: <Service>.<Operation>`. That is unusual enough for HTTP that it is
worth keeping rather than "fixing" into REST paths: it is *why* the AWS SDKs look
the way they do, and mirroring it means everything you learn against your own
service transfers to the real API.

The dispatcher is generic over two things a service brings: the enum of actions
it answers, and whatever state its handlers need. The enum is not decoration.
A dispatcher that reaches into a namespace with a caller-supplied string
(`getattr(handlers, action)`) is one typo away from being an arbitrary-call
gadget, and an unknown action should be a clean `InvalidAction` rather than an
`AttributeError` in a stack trace — which, rendered by a debug handler, tells a
stranger your module layout.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from typing import Any

from .errors import InvalidAction, MissingAction
from .wire import TARGET_HEADER

__all__ = ["TargetDispatcher", "parse_target"]


def parse_target[A: enum.StrEnum](value: str | None, *, prefix: str, actions: type[A]) -> A:
    """Resolve `X-Amz-Target` to one of `actions`.

    `prefix` is the service's own — `AmazonSQS.` for SQS, `DynamoDB_20120810.`
    for DynamoDB (the date is the API version, and it is part of the string the
    SDK sends). Raises `MissingAction` when the header is absent and
    `InvalidAction` when it names something this service does not answer.

    This runs *before* any resource is looked up, so a caller who sends garbage
    learns only that the target was bad — not whether a queue by some name they
    guessed happens to exist.
    """
    if not value:
        raise MissingAction(f"missing {TARGET_HEADER} header")
    if not value.startswith(prefix):
        raise InvalidAction(f"{TARGET_HEADER} must be '{prefix}<Action>'")
    name = value[len(prefix) :]
    try:
        return actions(name)
    except ValueError:
        raise InvalidAction(f"unknown action {name!r}") from None


class TargetDispatcher[A: enum.StrEnum, S]:
    """A registry from action to handler, plus the lookup that drives it.

    Handlers all take `(body, state)` and return the response document. The
    uniform signature is what lets the table be a table: anything a handler needs
    beyond the request body reaches it through the service's own state object,
    the same one the routes module already assembles at startup.

        dispatcher = TargetDispatcher(prefix="AmazonSQS.", actions=Action)

        @dispatcher.on(Action.SEND_MESSAGE)
        async def _send(body: dict[str, Any], state: AppState) -> dict[str, Any]:
            ...

        result = await dispatcher.dispatch(request.headers.get(TARGET_HEADER), body, state)
    """

    def __init__(self, *, prefix: str, actions: type[A]) -> None:
        self.prefix = prefix
        self.actions = actions
        self._handlers: dict[A, Callable[[dict[str, Any], S], Awaitable[dict[str, Any]]]] = {}

    def on(
        self, action: A
    ) -> Callable[
        [Callable[[dict[str, Any], S], Awaitable[dict[str, Any]]]],
        Callable[[dict[str, Any], S], Awaitable[dict[str, Any]]],
    ]:
        """Register the handler for one action. Registering twice is a bug."""

        def register(
            handler: Callable[[dict[str, Any], S], Awaitable[dict[str, Any]]],
        ) -> Callable[[dict[str, Any], S], Awaitable[dict[str, Any]]]:
            if action in self._handlers:
                raise ValueError(f"{action} already has a handler")
            self._handlers[action] = handler
            return handler

        return register

    def resolve(self, target: str | None) -> A:
        return parse_target(target, prefix=self.prefix, actions=self.actions)

    @property
    def unregistered(self) -> list[A]:
        """Actions in the enum with no handler — the worklist, at startup.

        Worth asserting in a test. The failure this prevents is a service that
        advertises an operation in its enum, accepts the target header happily,
        and then 500s, which reads to a client as "the service is broken" rather
        than "that is not built yet".
        """
        return [action for action in self.actions if action not in self._handlers]

    async def dispatch(self, target: str | None, body: dict[str, Any], state: S) -> dict[str, Any]:
        """Resolve the target and run its handler."""
        action = self.resolve(target)
        handler = self._handlers.get(action)
        if handler is None:
            raise InvalidAction(f"{action} is not implemented by this service")
        return await handler(body, state)
