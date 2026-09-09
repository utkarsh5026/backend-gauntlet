"""Bucket and key validation — the trust boundary.

These rules are the store's first line of defence, not cosmetics: the bucket
character set is what makes a bucket name safe as a directory component, and
`encode_key` is what makes an arbitrary key safe as a filename.
"""

from __future__ import annotations

import pytest

from object_store.errors import InvalidRequest
from object_store.naming import Bucket, Key, ObjectPath, encode_key


@pytest.mark.parametrize("name", ["abc", "photos", "my-bucket", "a1b2c3", "a" * 63])
def test_bucket_accepts_valid_names(name: str) -> None:
    assert Bucket(name) == name


@pytest.mark.parametrize("name", ["", "ab", "a" * 64])
def test_bucket_rejects_lengths_outside_bounds(name: str) -> None:
    with pytest.raises(InvalidRequest):
        Bucket(name)


@pytest.mark.parametrize("name", ["Photos", "my_bucket", "a/b", "../etc", "my.bucket", "café"])
def test_bucket_rejects_illegal_characters(name: str) -> None:
    """The character set is the traversal defence — `a/b` and `../etc` fail here."""
    with pytest.raises(InvalidRequest):
        Bucket(name)


@pytest.mark.parametrize("name", ["-photos", "photos-", "---"])
def test_bucket_rejects_edge_hyphens(name: str) -> None:
    with pytest.raises(InvalidRequest):
        Bucket(name)


@pytest.mark.parametrize(
    "key", ["a", "a/b/c.jpg", "with spaces & symbols!", "../../etc/passwd", "k" * 1024]
)
def test_key_accepts_anything_within_the_byte_cap(key: str) -> None:
    """A traversal-shaped key is a *valid key*; `encode_key` is what neutralises it."""
    assert Key(key) == key


def test_key_rejects_empty_and_over_length() -> None:
    with pytest.raises(InvalidRequest):
        Key("")
    with pytest.raises(InvalidRequest):
        Key("k" * 1025)


def test_key_counts_bytes_not_characters() -> None:
    """513 two-byte characters is 1026 bytes — over the cap despite `len` saying 513."""
    key = "é" * 513
    assert len(key) == 513
    with pytest.raises(InvalidRequest):
        Key(key)


def test_encode_key_passes_unreserved_characters_through() -> None:
    assert encode_key("beach.jpg") == "beach.jpg"
    assert encode_key("A-Za-z0-9._~") == "A-Za-z0-9._~"


def test_encode_key_percent_encodes_separators_and_spaces() -> None:
    assert encode_key("vacation/beach.jpg") == "vacation%2fbeach.jpg"
    assert encode_key("my file.txt") == "my%20file.txt"


def test_encode_key_neutralises_path_traversal() -> None:
    """The point: the output has no separators, so it cannot name a parent."""
    encoded = encode_key("../../etc/passwd")
    assert encoded == "..%2f..%2fetc%2fpasswd"
    assert "/" not in encoded


def test_encode_key_encodes_non_ascii_as_utf8_bytes() -> None:
    assert encode_key("café") == "caf%c3%a9"


def test_encoding_is_injective_for_keys_that_differ() -> None:
    """Two distinct keys must never collide on one index filename.

    `a/b` and `a%2fb` are different keys and would be the same file under a
    naive encoder that left `%` alone.
    """
    assert encode_key("a/b") != encode_key("a%2fb")


def test_object_path_validates_both_sides() -> None:
    path = ObjectPath("photos", "a/b.txt")
    assert path.bucket == "photos"
    assert path.key == "a/b.txt"
    with pytest.raises(InvalidRequest):
        ObjectPath("Bad", "k")
    with pytest.raises(InvalidRequest):
        ObjectPath("photos", "")


def test_as_filename_is_not_str_encode() -> None:
    """`Key` is a `str` subclass, so a method named `encode` would shadow the
    built-in for every caller — including `encode_key`, which needs it."""
    key = Key("a/b")
    assert key.as_filename() == "a%2fb"
    assert key.encode("utf-8") == b"a/b"


def test_from_trusted_skips_validation() -> None:
    """Rows we wrote are data, not requests — see `naming.ValidatedStr`."""
    assert Bucket.from_trusted("NOT-valid-AT-all") == "NOT-valid-AT-all"
