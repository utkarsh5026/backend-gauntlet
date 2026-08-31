"""The two-list envelope, and the structural rules that guard it."""

from __future__ import annotations

import pytest

from common_aws import (
    AwsError,
    BatchResult,
    InvalidParameterValue,
    ValidationException,
    parse_batch_entries,
)


class TooBig(AwsError):
    status_code = 400
    error_code = "InvalidMessageContents"
    message = "the message body is too large"


def test_entries_are_parsed_with_their_correlation_ids() -> None:
    entries = parse_batch_entries(
        {"Entries": [{"Id": "a", "MessageBody": "1"}, {"Id": "b", "MessageBody": "2"}]}
    )
    assert [entry.entry_id for entry in entries] == ["a", "b"]
    assert entries[0].body["MessageBody"] == "1"


def test_an_empty_batch_is_rejected() -> None:
    with pytest.raises(ValidationException):
        parse_batch_entries({"Entries": []})
    with pytest.raises(ValidationException):
        parse_batch_entries({})


def test_a_batch_over_the_cap_is_rejected() -> None:
    with pytest.raises(InvalidParameterValue):
        parse_batch_entries({"Entries": [{"Id": str(n)} for n in range(11)]})


def test_duplicate_ids_are_rejected_because_results_are_matched_by_them() -> None:
    with pytest.raises(InvalidParameterValue, match="duplicate"):
        parse_batch_entries({"Entries": [{"Id": "a"}, {"Id": "a"}]})


def test_an_unbounded_id_is_rejected_because_it_is_echoed_back() -> None:
    with pytest.raises(InvalidParameterValue):
        parse_batch_entries({"Entries": [{"Id": "x" * 81}]})


def test_a_partial_failure_is_a_200_carrying_both_lists() -> None:
    result = BatchResult()
    result.succeed("a", MessageId="m-1")
    result.fail("b", TooBig())

    assert result.to_response() == {
        "Successful": [{"Id": "a", "MessageId": "m-1"}],
        "Failed": [
            {
                "Id": "b",
                "Code": "InvalidMessageContents",
                "Message": "the message body is too large",
                "SenderFault": True,
            }
        ],
    }


def test_both_lists_are_present_even_when_one_is_empty() -> None:
    # A client indexes into them; an absent key is a KeyError in someone's worker.
    assert BatchResult().to_response() == {"Successful": [], "Failed": []}


def test_a_server_side_entry_failure_is_not_the_senders_fault() -> None:
    class Wobble(AwsError):
        status_code = 500
        error_code = "InternalFailure"

    result = BatchResult()
    result.fail("a", Wobble("the shard holding this entry is unavailable"))
    entry = result.failed[0]
    assert entry["SenderFault"] is False
    # And the 5xx scrubbing rule still applies inside a batch.
    assert entry["Message"] == Wobble.message


def test_the_success_key_is_configurable_for_services_that_differ() -> None:
    assert "Results" in BatchResult().to_response("Results")
