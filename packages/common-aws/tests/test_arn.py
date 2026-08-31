"""Parsing the name services pass to each other."""

from __future__ import annotations

import pytest

from common_aws import Arn, InvalidParameterValue


def test_round_trip() -> None:
    raw = "arn:aws:sqs:us-east-1:123456789012:orders.fifo"
    arn = Arn.parse(raw)
    assert arn.service == "sqs"
    assert arn.region == "us-east-1"
    assert arn.account_id == "123456789012"
    assert arn.resource == "orders.fifo"
    assert str(arn) == raw


def test_a_resource_may_contain_colons() -> None:
    # Lambda qualifies a function with a version or alias, and splitting on every
    # colon would tear the name apart.
    arn = Arn.parse("arn:aws:lambda:us-east-1:123456789012:function:checkout:PROD")
    assert arn.resource == "function:checkout:PROD"
    assert arn.resource_type == "function"
    assert arn.resource_id == "checkout:PROD"


def test_a_slash_separated_resource_splits_too() -> None:
    arn = Arn.parse("arn:aws:dynamodb:us-east-1:123456789012:table/orders")
    assert arn.resource_type == "table"
    assert arn.resource_id == "orders"


def test_a_bare_resource_has_no_type() -> None:
    arn = Arn.parse("arn:aws:s3:::my-bucket")
    assert arn.resource_type is None
    assert arn.resource_id == "my-bucket"
    # S3 is global and account-less in an ARN; empty fields are legal.
    assert arn.region == "" and arn.account_id == ""


def test_account_scoping_is_one_call_because_it_is_the_one_people_skip() -> None:
    arn = Arn.parse("arn:aws:sqs:us-east-1:111111111111:orders")
    assert arn.is_same_account("111111111111")
    assert not arn.is_same_account("222222222222")


def test_an_arn_is_hashable_and_comparable() -> None:
    raw = "arn:aws:sqs:us-east-1:1:q"
    assert Arn.parse(raw) == Arn.parse(raw)
    assert len({Arn.parse(raw), Arn.parse(raw)}) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-arn",
        "arn:aws:sqs:us-east-1:123456789012",
        "aws:arn:sqs:us-east-1:123456789012:q",
        "arn:aws:sqs:us-east-1:123456789012:",
        "arn::sqs:us-east-1:123456789012:q",
    ],
)
def test_malformed_arns_are_refused(bad: str) -> None:
    with pytest.raises(InvalidParameterValue):
        Arn.parse(bad)
