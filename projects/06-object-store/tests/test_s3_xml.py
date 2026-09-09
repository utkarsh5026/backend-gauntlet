"""The S3 XML wire format — escaping, quoting, and parsing other people's output."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from object_store.errors import InvalidRequest
from object_store.s3_xml import (
    ListBucketParams,
    ListContent,
    complete_multipart_result,
    escape,
    initiate_multipart_result,
    list_bucket_result,
    parse_complete_multipart_body,
    parse_error,
    parse_list_bucket,
)

MOMENT = datetime(2024, 6, 1, 12, 30, 45, tzinfo=UTC)


def test_escape_handles_all_five_specials() -> None:
    assert escape("a&b<c>d\"e'f") == "a&amp;b&lt;c&gt;d&quot;e&apos;f"


def test_escape_does_not_double_escape_ampersands() -> None:
    """`&` must be replaced first, or `<` becomes `&amp;lt;`."""
    assert escape("<") == "&lt;"
    assert escape("&lt;") == "&amp;lt;"


def test_a_key_with_xml_specials_round_trips() -> None:
    """One `&` in a filename would otherwise break the listing for the bucket."""
    body = list_bucket_result(
        ListBucketParams(
            name="photos",
            prefix="",
            delimiter=None,
            max_keys=1000,
            is_truncated=False,
            next_continuation_token=None,
            contents=[ListContent("a&b<c>.txt", MOMENT, "abc123", 42)],
            common_prefixes=[],
        )
    )

    assert parse_list_bucket(body.encode()).object_keys == ["a&b<c>.txt"]


def test_etags_are_quoted_on_the_wire() -> None:
    body = list_bucket_result(
        ListBucketParams(
            name="photos",
            prefix="",
            delimiter=None,
            max_keys=1000,
            is_truncated=False,
            next_continuation_token=None,
            contents=[ListContent("a.txt", MOMENT, "abc123", 1)],
            common_prefixes=[],
        )
    )

    assert "<ETag>&quot;abc123&quot;</ETag>" in body
    assert parse_list_bucket(body.encode()).contents[0].etag == "abc123"


def test_a_multipart_etag_keeps_its_suffix_inside_the_quotes() -> None:
    body = complete_multipart_result("photos", "big.bin", "abc123-4")
    assert "&quot;abc123-4&quot;" in body


def test_the_listing_omits_optional_elements_when_unset() -> None:
    body = list_bucket_result(
        ListBucketParams(
            name="photos",
            prefix="",
            delimiter=None,
            max_keys=1000,
            is_truncated=False,
            next_continuation_token=None,
            contents=[],
            common_prefixes=[],
        )
    )

    assert "<Delimiter>" not in body
    assert "<NextContinuationToken>" not in body


def test_initiate_carries_the_upload_id() -> None:
    body = initiate_multipart_result("photos", "big.bin", "upload-123")
    assert "<UploadId>upload-123</UploadId>" in body


def test_complete_body_parses_a_namespaced_document() -> None:
    """Clients differ on namespacing, and both spellings are valid."""
    body = b"""<?xml version="1.0"?>
    <CompleteMultipartUpload xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Part><PartNumber>1</PartNumber><ETag>"aaa"</ETag></Part>
      <Part><PartNumber>2</PartNumber><ETag>bbb</ETag></Part>
    </CompleteMultipartUpload>"""

    parts = parse_complete_multipart_body("application/xml", body)

    assert [(p.part_number, str(p.etag)) for p in parts] == [(1, "aaa"), (2, "bbb")]


def test_complete_body_parses_an_unnamespaced_document() -> None:
    body = (
        b"<CompleteMultipartUpload><Part><PartNumber>1</PartNumber>"
        b"<ETag>abc</ETag></Part></CompleteMultipartUpload>"
    )
    parts = parse_complete_multipart_body(None, body)
    assert parts[0].part_number == 1


def test_complete_body_accepts_the_console_json_shape() -> None:
    body = b'{"parts": [{"partNumber": 1, "etag": "\\"abc\\""}]}'
    parts = parse_complete_multipart_body("application/json; charset=utf-8", body)

    assert parts[0].part_number == 1
    assert str(parts[0].etag) == "abc"


def test_malformed_bodies_are_client_errors_not_crashes() -> None:
    for content_type, body in (
        ("application/xml", b"<not closed"),
        ("application/json", b"{not json"),
        (
            "application/xml",
            b"<CompleteMultipartUpload><Part><PartNumber>x</PartNumber>"
            b"<ETag>a</ETag></Part></CompleteMultipartUpload>",
        ),
    ):
        with pytest.raises(InvalidRequest):
            parse_complete_multipart_body(content_type, body)


def test_a_part_missing_its_etag_is_a_client_error() -> None:
    body = (
        b"<CompleteMultipartUpload><Part><PartNumber>1</PartNumber></Part>"
        b"</CompleteMultipartUpload>"
    )
    with pytest.raises(InvalidRequest):
        parse_complete_multipart_body("application/xml", body)


def test_error_bodies_round_trip() -> None:
    from object_store.errors import error_xml

    parsed = parse_error(error_xml("NoSuchKey", "no such key").encode())
    assert parsed.code == "NoSuchKey"
    assert parsed.message == "no such key"


def test_a_malformed_error_body_is_a_client_error() -> None:
    with pytest.raises(InvalidRequest):
        parse_error(b"<Error><Code>Broken")
