"""The Query protocol — AWS's first wire format, still load-bearing.

IAM and STS speak it, which makes it project **25**'s problem: the request is
form-encoded with the verb in an `Action` parameter, and the response is XML.
Nested data is expressed by **flattening the path into the parameter name**,
1-indexed:

    Action=CreateRole&RoleName=admin
      &Tags.member.1.Key=team&Tags.member.1.Value=infra
      &Tags.member.2.Key=env&Tags.member.2.Value=prod

That is a document encoded in a namespace with no nesting, which is exactly what
JSON later fixed — reading the two side by side is the fastest way to understand
why AWS JSON exists at all. The `member` segment is an artifact of the XML schema
the parameters were generated from; some services include it, some don't, so it
is skipped rather than required.

Two directions, both here: parse a flattened parameter list into the nested
document a handler wants, and render a handler's document back into the XML
envelope an SDK expects.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast
from urllib.parse import parse_qsl
from xml.etree import ElementTree as ET

from .errors import MissingAction

__all__ = [
    "parse_query_params",
    "parse_query_string",
    "render_query_response",
    "require_action",
]

# Not a required segment, just a common one. Dropping it means `Tags.member.1.Key`
# and `Tags.1.Key` parse to the same document, which is what a handler wants.
_LIST_INFIX = "member"


def parse_query_string(raw: str) -> dict[str, Any]:
    """Parse a form-encoded body (or query string) into a nested document."""
    return parse_query_params(parse_qsl(raw, keep_blank_values=True))


def parse_query_params(pairs: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Un-flatten `A.1.B=x` parameters into `{"A": [{"B": "x"}]}`.

    Values stay strings. Coercion is the handler's job, because only the handler
    knows whether `"0"` is a count, a flag or a queue named zero — a protocol
    layer that starts guessing types is a protocol layer that will eventually
    disagree with the service about what a parameter means.
    """
    tree: dict[str, Any] = {}
    for key, value in pairs:
        segments = [s for s in key.split(".") if s and s != _LIST_INFIX]
        if not segments:
            continue
        cursor: dict[str, Any] = tree
        for segment in segments[:-1]:
            existing: object = cursor.get(segment)
            if isinstance(existing, dict):
                cursor = cast(dict[str, Any], existing)
            else:
                fresh: dict[str, Any] = {}
                cursor[segment] = fresh
                cursor = fresh
        cursor[segments[-1]] = value
    return _collapse_indices(tree)


def _collapse_indices(node: dict[str, Any]) -> dict[str, Any]:
    """Turn `{"1": …, "2": …}` maps into lists, depth first.

    Done as a second pass rather than while walking: the parameters arrive in
    whatever order the client serialized them, so you cannot know a key is an
    index until you have seen all its siblings.
    """
    out: dict[str, Any] = {}
    for key, value in node.items():
        out[key] = _collapse_value(value)
    return out


def _collapse_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    node = cast(dict[str, Any], value)
    if node and all(key.isdigit() for key in node):
        return [_collapse_value(node[key]) for key in sorted(node, key=int)]
    return _collapse_indices(node)


def require_action(params: dict[str, Any]) -> str:
    """The `Action` parameter, or `MissingAction`.

    The Query protocol's equivalent of `X-Amz-Target`, and it fails the same way:
    before any resource is looked up, so a caller with a bad request learns
    nothing about what does or does not exist here.
    """
    action = params.get("Action")
    if not isinstance(action, str) or not action:
        raise MissingAction("the request is missing the Action parameter")
    return action


def render_query_response(
    action: str,
    result: dict[str, Any] | None,
    *,
    request_id: str,
    namespace: str | None = None,
    list_style: str = "member",
) -> str:
    """Wrap a result document in `<{Action}Response>` and serialize it.

    `list_style` exists because AWS is not consistent: IAM wraps list items in
    `<member>`, SQS repeats the element name. Pick the one your service's real
    counterpart uses and keep it — an SDK's parser is generated from the service
    model, so the wrong one produces an empty list rather than an error, which is
    the worst failure mode available.
    """
    attrs = {"xmlns": namespace} if namespace else {}
    root = ET.Element(f"{action}Response", attrs)
    if result is not None:
        _append_document(ET.SubElement(root, f"{action}Result"), result, list_style)
    metadata = ET.SubElement(root, "ResponseMetadata")
    ET.SubElement(metadata, "RequestId").text = request_id
    return ET.tostring(root, encoding="unicode")


def _append_document(parent: ET.Element, document: dict[str, Any], list_style: str) -> None:
    for key, value in document.items():
        _append_value(parent, key, value, list_style)


def _append_value(parent: ET.Element, key: str, value: Any, list_style: str) -> None:
    if value is None:
        # Absent, not empty. `<Foo/>` and "no Foo" mean different things to a
        # generated parser, and the real services omit rather than emit empties.
        return
    if isinstance(value, dict):
        _append_document(ET.SubElement(parent, key), cast(dict[str, Any], value), list_style)
        return
    if isinstance(value, list):
        items = cast(list[Any], value)
        if list_style == "member":
            container = ET.SubElement(parent, key)
            for item in items:
                _append_value(container, "member", item, list_style)
        else:
            for item in items:
                _append_value(parent, key, item, list_style)
        return
    ET.SubElement(parent, key).text = _scalar(value)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        # Python's `str(True)` is "True"; XML schema says "true", and a strict
        # client parses the capital as a string rather than a boolean.
        return "true" if value else "false"
    return str(value)
