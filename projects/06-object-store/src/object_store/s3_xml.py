"""The S3 XML wire format — emit listings, multipart bodies and errors; parse
`CompleteMultipartUpload`.

The handlers speak this dialect so real clients (the `aws` CLI, boto3, Arrow's
`object_store`) deserialize responses with no adapter. It is the difference
between "an HTTP file server" and "S3-compatible": an SDK will not accept JSON
for `ListBucketResult` no matter how well-formed it is.

## Hand-rolled emit, parsed decode

Encoding is string building because the schemas are small, fixed and ours —
there is no XML library whose output would be more correct, only more indirect.
The one thing that *is* dangerous about it is escaping, which is why every text
value goes through `escape` and why `escape` is not optional: an object key is
client input and can legitimately contain `&` or `<`, and a key like
`a<b&c` would otherwise produce a body no parser can read. That is a broken
listing for the whole bucket, caused by one filename.

Decoding uses `xml.etree.ElementTree` rather than string matching, because the
bodies arriving are *someone else's* output and may be namespaced, indented, or
element-ordered differently than we would write them.

## Untrusted input

`ElementTree` does not expand external entities or resolve DTDs, so the classic
billion-laughs and XXE file-read attacks do not apply to it — but the parser is
still the only thing between a request body and this process, so parse failures
become `InvalidRequest` rather than propagating as `ParseError`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree

from .errors import InvalidRequest
from .multipart import PartETag
from .objects import ETag

__all__ = [
    "ListBucketParams",
    "ListContent",
    "ParsedListBucket",
    "complete_multipart_result",
    "error_body",
    "escape",
    "initiate_multipart_result",
    "list_bucket_result",
    "parse_complete_multipart_body",
    "parse_error",
    "parse_list_bucket",
]

S3_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'


def escape(value: str) -> str:
    """Escape the five XML special characters in element text.

    `&` first — escaping it after the others would double-escape the ampersands
    they just introduced, turning `<` into `&amp;lt;`.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def quoted_etag(etag: str) -> str:
    """Quote an ETag the way S3 puts it on the wire: `"abc123"` or `"abc-2"`.

    Trims any quotes already present so a value that round-tripped through a
    client does not come back wearing two sets.
    """
    return f'"{etag.strip(chr(34))}"'


def iso8601(moment: datetime) -> str:
    """The ISO-8601 form S3 uses inside XML — millisecond precision, `Z` suffix."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass(frozen=True, slots=True)
class ListContent:
    """One `<Contents>` row in a `ListBucketResult`.

    Version ids are deliberately absent: standard `ListObjectsV2` XML does not
    carry them, and inventing an element would break the SDKs this format exists
    to satisfy. Clients address versions with `?versionId=` instead.
    """

    key: str
    last_modified: datetime
    etag: str
    size: int


@dataclass(frozen=True, slots=True)
class ListBucketParams:
    """Everything one `ListObjectsV2` page needs to render."""

    name: str
    prefix: str
    delimiter: str | None
    max_keys: int
    is_truncated: bool
    next_continuation_token: str | None
    contents: list[ListContent]
    common_prefixes: list[str]


def list_bucket_result(params: ListBucketParams) -> str:
    """Serialise one `ListObjectsV2` page."""
    parts = [
        XML_DECLARATION,
        f'<ListBucketResult xmlns="{S3_NAMESPACE}">',
        f"<Name>{escape(params.name)}</Name>",
        f"<Prefix>{escape(params.prefix)}</Prefix>",
    ]
    if params.delimiter:
        parts.append(f"<Delimiter>{escape(params.delimiter)}</Delimiter>")
    parts.append(f"<MaxKeys>{params.max_keys}</MaxKeys>")
    parts.append(f"<IsTruncated>{'true' if params.is_truncated else 'false'}</IsTruncated>")
    if params.next_continuation_token:
        parts.append(
            "<NextContinuationToken>"
            f"{escape(params.next_continuation_token)}"
            "</NextContinuationToken>"
        )

    for content in params.contents:
        parts.append(
            "<Contents>"
            f"<Key>{escape(content.key)}</Key>"
            f"<LastModified>{iso8601(content.last_modified)}</LastModified>"
            f"<ETag>{escape(quoted_etag(content.etag))}</ETag>"
            f"<Size>{content.size}</Size>"
            "<StorageClass>STANDARD</StorageClass>"
            "</Contents>"
        )
    for prefix in params.common_prefixes:
        parts.append(f"<CommonPrefixes><Prefix>{escape(prefix)}</Prefix></CommonPrefixes>")

    parts.append("</ListBucketResult>")
    return "".join(parts)


def initiate_multipart_result(bucket: str, key: str, upload_id: str) -> str:
    """Serialise an `InitiateMultipartUpload` response."""
    return (
        f"{XML_DECLARATION}"
        f'<InitiateMultipartUploadResult xmlns="{S3_NAMESPACE}">'
        f"<Bucket>{escape(bucket)}</Bucket>"
        f"<Key>{escape(key)}</Key>"
        f"<UploadId>{escape(upload_id)}</UploadId>"
        "</InitiateMultipartUploadResult>"
    )


def complete_multipart_result(bucket: str, key: str, etag: str) -> str:
    """Serialise a `CompleteMultipartUpload` response.

    `etag` is the assembled multipart ETag (usually `hex-N`), quoted on the wire
    exactly like a single-PUT one — the suffix is inside the quotes, not after.
    """
    return (
        f"{XML_DECLARATION}"
        f'<CompleteMultipartUploadResult xmlns="{S3_NAMESPACE}">'
        f"<Bucket>{escape(bucket)}</Bucket>"
        f"<Key>{escape(key)}</Key>"
        f"<ETag>{escape(quoted_etag(etag))}</ETag>"
        "</CompleteMultipartUploadResult>"
    )


def error_body(code: str, message: str) -> str:
    """The S3 `<Error>` envelope. Mirrors `errors.error_xml`."""
    return (
        f"{XML_DECLARATION}"
        f"<Error><Code>{escape(code)}</Code>"
        f"<Message>{escape(message)}</Message></Error>"
    )


def _local_name(tag: str) -> str:
    """Strip a `{namespace}` prefix off an ElementTree tag.

    Clients differ on whether they namespace their request bodies, and both are
    valid, so matching on the local name is the only thing that works for all of
    them.
    """
    return tag.rpartition("}")[2]


def _find_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def _iter_children(element: ElementTree.Element, name: str):  # noqa: ANN202
    return (child for child in element if _local_name(child.tag) == name)


def parse_complete_multipart_xml(data: bytes) -> list[PartETag]:
    """Parse an S3 `CompleteMultipartUpload` body into part entries.

    Strips surrounding quotes from each ETag, because clients echo back exactly
    what we sent them and we sent quotes.
    """
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as err:
        raise InvalidRequest(f"invalid CompleteMultipartUpload XML: {err}") from None

    parts: list[PartETag] = []
    for element in _iter_children(root, "Part"):
        raw_number = _find_text(element, "PartNumber")
        raw_etag = _find_text(element, "ETag")
        if raw_number is None or raw_etag is None:
            raise InvalidRequest("Part requires both PartNumber and ETag")
        try:
            number = int(raw_number)
        except ValueError:
            raise InvalidRequest(f"invalid PartNumber {raw_number!r}") from None
        parts.append(PartETag(number, ETag(raw_etag.strip('"'))))
    return parts


def parse_complete_multipart_json(data: bytes) -> list[PartETag]:
    """Parse the web console's JSON complete body: `{"parts": [...]}`.

    Kept so the browser playground can finish an upload without hand-building S3
    XML in JavaScript. Real SDKs never take this path.
    """
    try:
        payload = json.loads(data)
        return [
            PartETag(int(part["partNumber"]), ETag(str(part["etag"]).strip('"')))
            for part in payload["parts"]
        ]
    except (ValueError, KeyError, TypeError) as err:
        raise InvalidRequest(f"invalid CompleteMultipartUpload JSON: {err}") from None


def parse_complete_multipart_body(content_type: str | None, data: bytes) -> list[PartETag]:
    """Dispatch `CompleteMultipartUpload` parsing on `Content-Type`.

    JSON only when the type is explicitly `application/json`; everything else —
    including a missing header — is treated as XML, which is what every SDK
    sends and what a client that sets no header almost certainly means.
    """
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        if base == "application/json":
            return parse_complete_multipart_json(data)
    return parse_complete_multipart_xml(data)


@dataclass(frozen=True, slots=True)
class ParsedError:
    """A parsed `<Error>` envelope — for tests and structured clients."""

    code: str
    message: str


def parse_error(data: bytes) -> ParsedError:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as err:
        raise InvalidRequest(f"invalid Error XML: {err}") from None
    return ParsedError(
        code=_find_text(root, "Code") or "",
        message=_find_text(root, "Message") or "",
    )


@dataclass(frozen=True, slots=True)
class ParsedContent:
    key: str
    etag: str
    size: int


@dataclass(frozen=True, slots=True)
class ParsedListBucket:
    """A parsed `ListBucketResult` — the subset tests and the console need."""

    contents: list[ParsedContent]
    common_prefixes: list[str]
    is_truncated: bool
    next_continuation_token: str | None

    @property
    def object_keys(self) -> list[str]:
        return [content.key for content in self.contents]


def parse_list_bucket(data: bytes) -> ParsedListBucket:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as err:
        raise InvalidRequest(f"invalid ListBucketResult XML: {err}") from None

    contents = [
        ParsedContent(
            key=_find_text(element, "Key") or "",
            etag=(_find_text(element, "ETag") or "").strip('"'),
            size=int(_find_text(element, "Size") or 0),
        )
        for element in _iter_children(root, "Contents")
    ]
    prefixes = [
        _find_text(element, "Prefix") or "" for element in _iter_children(root, "CommonPrefixes")
    ]
    return ParsedListBucket(
        contents=contents,
        common_prefixes=prefixes,
        is_truncated=(_find_text(root, "IsTruncated") or "").lower() == "true",
        next_continuation_token=_find_text(root, "NextContinuationToken"),
    )
