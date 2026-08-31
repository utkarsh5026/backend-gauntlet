"""Target parsing and the action table."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

import pytest

from common_aws import InvalidAction, MissingAction, TargetDispatcher, parse_target


class Action(enum.StrEnum):
    SEND_MESSAGE = "SendMessage"
    RECEIVE_MESSAGE = "ReceiveMessage"


@dataclass
class State:
    sent: list[str]


def test_parse_target_resolves_the_operation() -> None:
    assert parse_target("AmazonSQS.SendMessage", prefix="AmazonSQS.", actions=Action) is (
        Action.SEND_MESSAGE
    )


def test_a_versioned_prefix_works_too() -> None:
    # DynamoDB's prefix carries the API version, and the SDK sends it verbatim.
    class Ddb(enum.StrEnum):
        PUT_ITEM = "PutItem"

    assert parse_target("DynamoDB_20120810.PutItem", prefix="DynamoDB_20120810.", actions=Ddb) is (
        Ddb.PUT_ITEM
    )


def test_a_missing_header_is_missing_action() -> None:
    with pytest.raises(MissingAction):
        parse_target(None, prefix="AmazonSQS.", actions=Action)
    with pytest.raises(MissingAction):
        parse_target("", prefix="AmazonSQS.", actions=Action)


def test_a_foreign_or_unknown_target_is_invalid_action() -> None:
    with pytest.raises(InvalidAction):
        parse_target("AmazonSNS.Publish", prefix="AmazonSQS.", actions=Action)
    with pytest.raises(InvalidAction):
        parse_target("AmazonSQS.DropTable", prefix="AmazonSQS.", actions=Action)


def test_an_unknown_target_says_nothing_about_what_exists() -> None:
    # The message names the action the caller sent and nothing else — no list of
    # valid actions, no hint about resources.
    with pytest.raises(InvalidAction) as caught:
        parse_target("AmazonSQS.Nope", prefix="AmazonSQS.", actions=Action)
    assert "ReceiveMessage" not in str(caught.value)


async def test_dispatch_routes_to_the_registered_handler() -> None:
    dispatcher: TargetDispatcher[Action, State] = TargetDispatcher(
        prefix="AmazonSQS.", actions=Action
    )

    async def send(body: dict[str, Any], state: State) -> dict[str, Any]:
        state.sent.append(str(body["MessageBody"]))
        return {"MessageId": "m-1"}

    dispatcher.on(Action.SEND_MESSAGE)(send)

    state = State(sent=[])
    result = await dispatcher.dispatch("AmazonSQS.SendMessage", {"MessageBody": "hi"}, state)
    assert result == {"MessageId": "m-1"}
    assert state.sent == ["hi"]


async def test_a_known_action_with_no_handler_is_invalid_action_not_a_500() -> None:
    dispatcher: TargetDispatcher[Action, State] = TargetDispatcher(
        prefix="AmazonSQS.", actions=Action
    )
    with pytest.raises(InvalidAction):
        await dispatcher.dispatch("AmazonSQS.ReceiveMessage", {}, State(sent=[]))


def test_unregistered_lists_the_worklist() -> None:
    dispatcher: TargetDispatcher[Action, State] = TargetDispatcher(
        prefix="AmazonSQS.", actions=Action
    )
    assert dispatcher.unregistered == [Action.SEND_MESSAGE, Action.RECEIVE_MESSAGE]

    async def send(_body: dict[str, Any], _state: State) -> dict[str, Any]:
        return {}

    dispatcher.on(Action.SEND_MESSAGE)(send)
    assert dispatcher.unregistered == [Action.RECEIVE_MESSAGE]


def test_registering_an_action_twice_is_a_bug() -> None:
    dispatcher: TargetDispatcher[Action, State] = TargetDispatcher(
        prefix="AmazonSQS.", actions=Action
    )

    async def handler(_body: dict[str, Any], _state: State) -> dict[str, Any]:
        return {}

    dispatcher.on(Action.SEND_MESSAGE)(handler)
    with pytest.raises(ValueError, match="already has a handler"):
        dispatcher.on(Action.SEND_MESSAGE)(handler)
