"""S3-style bucket and key naming — validated strings + path encoding.

Construct `Bucket` / `Key` at the trust boundary and hand the *type* downstream:
nothing past this module re-runs validation, because having the type is the
proof that validation happened.

## Why `str` subclasses and not wrapper dataclasses

Rust made these newtypes over `String` and then had to write `Deref`, `AsRef`,
`Display`, `PartialEq<str>` and two `TryFrom`s to make them usable. In Python a
`str` subclass gets all of that for free — f-strings, `startswith`, dict keys,
slicing, sorting — while pyright still refuses a plain `str` where a `Bucket` is
required, which is the half that actually matters. The validation lives in
`__new__`, so `Bucket("Photos")` raises at the boundary and there is no way to
hold one that was never checked.

The exception is `from_trusted`, for values coming back off our own disk: the
index filenames *are* the encoded keys, so a round-tripped name has already been
validated once and re-checking it would turn a rule change into a data-loss bug.
Pydantic deserialization goes through that same door on purpose (see
`__get_pydantic_core_schema__`) — it mirrors Rust's `#[serde(transparent)]`,
which also did not re-validate.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

from .errors import InvalidRequest

__all__ = [
    "Bucket",
    "Key",
    "ObjectPath",
    "ValidatedStr",
    "encode_key",
]

MAX_KEY_LEN = 1024
"""S3's key ceiling, in **UTF-8 bytes** — not characters. See `Key`."""

MIN_BUCKET_LEN = 3
MAX_BUCKET_LEN = 63

_BUCKET_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
"""Every character an S3 bucket name may contain. `issuperset` over this set is
both the "lowercase ASCII only" rule and the "no dots, no underscores, no
slashes" rule at once — which is why `photos.jpg`, `my_bucket` and `../etc` all
fail here rather than needing a separate traversal check."""


class ValidatedStr(str):
    """A `str` that can only exist if it passed `validate`.

    Subclasses override `validate` and get construction-time checking plus a
    pydantic schema that loads *without* re-checking (`from_trusted`).
    """

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        cls.validate(value)
        return super().__new__(cls, value)

    @classmethod
    def validate(cls, value: str) -> None:
        """Raise `InvalidRequest` describing the first rule `value` breaks."""

    @classmethod
    def from_trusted(cls, value: str) -> Self:
        """Wrap a value already known to be valid — skips `validate`.

        For names read back out of our own layout (index filenames, a manifest,
        an internal RPC body). Never for client input.
        """
        return str.__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # Load through `from_trusted`, matching Rust's `#[serde(transparent)]`:
        # a row we wrote is data, not a request, and a stricter rule shipped
        # later must not make yesterday's objects unreadable. Serialization
        # needs no hook — these *are* strings.
        return core_schema.no_info_after_validator_function(
            cls.from_trusted, core_schema.str_schema()
        )


class Bucket(ValidatedStr):
    """A validated S3 bucket name: 3–63 chars, `[a-z0-9-]`, no edge hyphen.

    Doubles as the path-traversal defence. `/`, `.` and `_` are all rejected, so
    a `Bucket` can only ever name a single directory component under the index
    root — there is no spelling of one that escapes.
    """

    __slots__ = ()

    @classmethod
    def validate(cls, value: str) -> None:
        if not MIN_BUCKET_LEN <= len(value) <= MAX_BUCKET_LEN:
            raise InvalidRequest(
                f"bucket name must be {MIN_BUCKET_LEN}–{MAX_BUCKET_LEN} characters"
            )
        if not _BUCKET_CHARS.issuperset(value):
            raise InvalidRequest(
                "bucket name may only contain lowercase letters, digits, and hyphens"
            )
        if value.startswith("-") or value.endswith("-"):
            raise InvalidRequest("bucket name may not start or end with a hyphen")


class Key(ValidatedStr):
    """A validated object key: non-empty, at most 1024 UTF-8 **bytes**.

    The keyspace is flat. A `/` inside a key is an ordinary character that only
    `ListObjectsV2`'s delimiter logic ever attaches meaning to — it is never a
    directory. Use `encode` before a key touches a filesystem path.
    """

    __slots__ = ()

    @classmethod
    def validate(cls, value: str) -> None:
        if not value:
            raise InvalidRequest("object key must not be empty")
        # S3 counts bytes, and so must we: 513 two-byte characters is over the
        # cap even though `len(value)` says 513.
        size = len(str.encode(value, "utf-8"))
        if size > MAX_KEY_LEN:
            raise InvalidRequest(f"object key must be at most {MAX_KEY_LEN} bytes, got {size}")

    def as_filename(self) -> str:
        """Percent-encode into one safe filename component (see `encode_key`).

        Deliberately *not* named `encode`: `Key` is a `str` subclass, and an
        `encode` here would shadow `str.encode` for every caller — including
        `encode_key` itself, which needs the real one to get at the UTF-8 bytes.
        """
        return encode_key(self)


class ObjectPath:
    """A validated `(bucket, key)` pair — the usual object address."""

    __slots__ = ("bucket", "key")

    def __init__(self, bucket: str, key: str) -> None:
        self.bucket: Bucket = Bucket(bucket)
        self.key: Key = Key(key)

    def __repr__(self) -> str:
        return f"ObjectPath({self.bucket!r}, {self.key!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ObjectPath):
            return NotImplemented
        return self.bucket == other.bucket and self.key == other.key

    def __hash__(self) -> int:
        return hash((self.bucket, self.key))


_UNRESERVED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~")


def encode_key(key: str) -> str:
    """Percent-encode a key into a single safe filename component.

    This is the traversal defence for keys, and it works by *removing the
    concept of a path* rather than by inspecting for `..`: every byte outside
    the RFC 3986 unreserved set — `/` very much included — becomes `%xx`, so the
    result has no separators at all and cannot address a parent directory no
    matter what the client sent. Encoding is over UTF-8 bytes, so a non-ASCII
    key produces one escape per byte and the output is pure ASCII.

    Deliberately not `urllib.parse.quote`: its default `safe="/"` keeps slashes,
    which is the exact opposite of what is wanted here.
    """
    out: list[str] = []
    # `str.encode` explicitly, not `key.encode`: a `Key` is a `str` subclass and
    # a method of that name on it would be picked up here instead.
    for byte in str.encode(key, "utf-8"):
        char = chr(byte)
        out.append(char if char in _UNRESERVED else f"%{byte:02x}")
    return "".join(out)
