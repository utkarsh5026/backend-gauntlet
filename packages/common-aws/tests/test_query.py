"""The Query protocol: flattened parameters in, XML out."""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

import pytest

from common_aws import (
    MissingAction,
    parse_query_params,
    parse_query_string,
    render_query_response,
    require_action,
)


def test_scalars_parse_flat() -> None:
    assert parse_query_string("Action=GetUser&UserName=alice") == {
        "Action": "GetUser",
        "UserName": "alice",
    }


def test_indexed_parameters_become_a_list_in_order() -> None:
    parsed = parse_query_string(
        "Tags.member.1.Key=team&Tags.member.1.Value=infra"
        "&Tags.member.2.Key=env&Tags.member.2.Value=prod"
    )
    assert parsed["Tags"] == [
        {"Key": "team", "Value": "infra"},
        {"Key": "env", "Value": "prod"},
    ]


def test_the_member_infix_is_optional() -> None:
    with_infix = parse_query_string("A.member.1.B=x")
    without = parse_query_string("A.1.B=x")
    assert with_infix == without == {"A": [{"B": "x"}]}


def test_indices_order_by_number_not_by_string() -> None:
    # "10" sorts before "2" as a string, which would silently reorder a batch.
    parsed = parse_query_string("&".join(f"Ids.{n}=v{n}" for n in range(1, 12)))
    assert parsed["Ids"] == [f"v{n}" for n in range(1, 12)]


def test_parameters_can_arrive_in_any_order() -> None:
    parsed = parse_query_params(
        [("Tags.2.Key", "env"), ("Tags.1.Key", "team"), ("Tags.2.Value", "prod")]
    )
    assert parsed["Tags"] == [{"Key": "team"}, {"Key": "env", "Value": "prod"}]


def test_values_stay_strings() -> None:
    # Coercion belongs to the handler: only it knows whether "0" is a count or a
    # queue named zero.
    parsed = parse_query_string("MaxItems=0&Truncated=false")
    assert parsed == {"MaxItems": "0", "Truncated": "false"}


def test_require_action_reads_the_verb() -> None:
    assert require_action({"Action": "GetUser"}) == "GetUser"
    with pytest.raises(MissingAction):
        require_action({"UserName": "alice"})


def test_the_response_envelope_names_the_action() -> None:
    xml = render_query_response("GetUser", {"User": {"UserName": "alice"}}, request_id="r-1")
    root = ET.fromstring(xml)
    assert root.tag == "GetUserResponse"
    result = root.find("GetUserResult")
    assert result is not None
    user = result.find("User/UserName")
    assert user is not None and user.text == "alice"
    request_id = root.find("ResponseMetadata/RequestId")
    assert request_id is not None and request_id.text == "r-1"


def test_lists_render_as_members_by_default() -> None:
    xml = render_query_response("ListUsers", {"Users": ["a", "b"]}, request_id="r")
    root = ET.fromstring(xml)
    assert [element.text for element in root.findall("ListUsersResult/Users/member")] == ["a", "b"]


def test_flattened_lists_repeat_the_element_name() -> None:
    xml = render_query_response(
        "ListQueues", {"QueueUrl": ["u1", "u2"]}, request_id="r", list_style="flattened"
    )
    root = ET.fromstring(xml)
    assert [element.text for element in root.findall("ListQueuesResult/QueueUrl")] == ["u1", "u2"]


def test_booleans_use_the_xml_spelling() -> None:
    xml = render_query_response("GetX", {"IsTruncated": False}, request_id="r")
    assert "<IsTruncated>false</IsTruncated>" in xml


def test_none_is_omitted_rather_than_emitted_empty() -> None:
    xml = render_query_response("GetX", {"Present": "y", "Absent": None}, request_id="r")
    assert "Absent" not in xml
    assert "<Present>y</Present>" in xml


def test_values_are_escaped() -> None:
    hostile: dict[str, Any] = {"Name": "<script>&"}
    xml = render_query_response("GetX", hostile, request_id="r")
    assert "<script>" not in xml
    root = ET.fromstring(xml)
    name = root.find("GetXResult/Name")
    assert name is not None and name.text == "<script>&"


def test_a_result_less_action_still_carries_metadata() -> None:
    root = ET.fromstring(render_query_response("DeleteUser", None, request_id="r-9"))
    assert root.find("DeleteUserResult") is None
    request_id = root.find("ResponseMetadata/RequestId")
    assert request_id is not None and request_id.text == "r-9"
