"""Cursors: opaque, tamper-evident, and expiring."""

from __future__ import annotations

import pytest

from common_aws import CursorCodec, InvalidParameterValue


def codec(secret: bytes = b"service-key", ttl: int | None = 3600) -> CursorCodec:
    return CursorCodec(secret, ttl_seconds=ttl)


def test_round_trip() -> None:
    subject = codec()
    token = subject.encode({"last_key": "user#42", "shard": 3})
    assert subject.decode(token) == {"last_key": "user#42", "shard": 3}


def test_the_payload_is_not_readable_as_the_token() -> None:
    # Opaque to a caller reading it off the wire — the point is that they are not
    # invited to construct one.
    token = codec().encode({"last_key": "user#42"})
    assert "user#42" not in token


def test_an_edited_token_is_refused() -> None:
    subject = codec()
    token = subject.encode({"shard": 1})
    body, _, tag = token.partition(".")
    forged = f"{body[:-2]}AA.{tag}"
    with pytest.raises(InvalidParameterValue):
        subject.decode(forged)


def test_a_token_from_another_service_is_refused() -> None:
    token = codec(b"service-a").encode({"shard": 1})
    with pytest.raises(InvalidParameterValue):
        codec(b"service-b").decode(token)


def test_every_rejection_says_the_same_thing() -> None:
    # Distinguishing "expired" from "forged" tells someone probing which half of
    # their guess was right.
    subject = codec()
    messages: set[str] = set()
    for bad in ["", "garbage", "a.b", subject.encode({"x": 1}, now=0)]:
        with pytest.raises(InvalidParameterValue) as caught:
            subject.decode(bad, now=10_000_000)
        messages.add(str(caught.value))
    assert len(messages) == 1


def test_an_expired_token_is_refused() -> None:
    subject = codec(ttl=60)
    token = subject.encode({"shard": 1}, now=1000)
    assert subject.decode(token, now=1050) == {"shard": 1}
    with pytest.raises(InvalidParameterValue):
        subject.decode(token, now=1100)


def test_a_ttl_less_codec_never_expires() -> None:
    subject = codec(ttl=None)
    token = subject.encode({"shard": 1}, now=0)
    assert subject.decode(token, now=10_000_000) == {"shard": 1}


def test_small_clock_drift_is_tolerated_but_a_future_token_is_not() -> None:
    subject = codec()
    token = subject.encode({"shard": 1}, now=1000)
    assert subject.decode(token, now=970) == {"shard": 1}
    with pytest.raises(InvalidParameterValue):
        subject.decode(token, now=500)


def test_a_codec_needs_a_secret() -> None:
    with pytest.raises(ValueError, match="secret"):
        CursorCodec(b"")
