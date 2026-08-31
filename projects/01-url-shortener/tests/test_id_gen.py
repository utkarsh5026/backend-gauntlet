"""V1 - the id generator's acceptance criteria, as tests.

Everything here is pure arithmetic, so it needs no services and runs everywhere.
The property tests are the interesting ones: uniqueness under concurrency is the
kind of claim that a handful of hand-picked examples cannot support.
"""

from __future__ import annotations

import threading

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from url_shortener.id_gen import (
    BASE62_ALPHABET,
    MAX_SEQUENCE,
    IdGenerator,
    assemble_id,
    base62_decode,
    base62_encode,
    decode,
)

# --------------------------------------------------------------------------- #
# base62
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0"), (1, "1"), (10, "A"), (36, "a"), (61, "z"), (62, "10"), (3_844, "100")],
)
def test_base62_encodes_known_values(value: int, expected: str) -> None:
    assert base62_encode(value) == expected


@given(st.integers(min_value=0, max_value=2**63 - 1))
def test_base62_round_trips(value: int) -> None:
    encoded = base62_encode(value)
    assert encoded
    assert all(char in BASE62_ALPHABET for char in encoded)
    assert base62_decode(encoded) == value


def test_base62_decode_rejects_non_alphabet() -> None:
    with pytest.raises(ValueError, match="not a base62 string"):
        base62_decode("abc/def")


def test_slug_is_url_safe() -> None:
    slug = IdGenerator(0).next_slug()
    assert slug
    assert all(char in BASE62_ALPHABET for char in slug)


# --------------------------------------------------------------------------- #
# bit packing
# --------------------------------------------------------------------------- #


@given(
    timestamp=st.integers(min_value=0, max_value=(1 << 41) - 1),
    sequence=st.integers(min_value=0, max_value=MAX_SEQUENCE - 1),
    node_id=st.integers(min_value=0, max_value=1023),
)
def test_assemble_and_decode_preserve_every_field(
    timestamp: int, sequence: int, node_id: int
) -> None:
    parts = decode(assemble_id(timestamp, sequence, node_id))
    assert parts.timestamp_ms == timestamp
    assert parts.node_id == node_id
    assert parts.sequence == sequence


def test_rejects_out_of_range_node_id() -> None:
    with pytest.raises(ValueError, match="10 bits"):
        IdGenerator(1024)
    with pytest.raises(ValueError, match="10 bits"):
        IdGenerator(-1)


def test_ids_embed_the_node_id() -> None:
    generator = IdGenerator(123)
    assert all(decode(generator.next_id()).node_id == 123 for _ in range(50))


# --------------------------------------------------------------------------- #
# the SPEC's "Done when ALL true" list
# --------------------------------------------------------------------------- #


def test_ids_are_strictly_increasing() -> None:
    """Time-ordered: for any two ids from one node, the later one is greater."""
    generator = IdGenerator(42)
    ids = [generator.next_id() for _ in range(2_000)]
    assert all(later > earlier for earlier, later in zip(ids, ids[1:], strict=False))


def test_ids_are_unique() -> None:
    generator = IdGenerator(42)
    ids = [generator.next_id() for _ in range(5_000)]
    assert len(set(ids)) == len(ids)


def test_different_node_ids_never_collide() -> None:
    a = {IdGenerator(1).next_id() for _ in range(200)}
    b = {IdGenerator(2).next_id() for _ in range(200)}
    assert a.isdisjoint(b)


def test_same_millisecond_burst_exhausts_the_sequence_then_waits() -> None:
    """A frozen clock forces every id into one millisecond.

    With the clock stuck, the generator must hand out exactly `MAX_SEQUENCE`
    distinct ids for that millisecond and then have nowhere left to go - which is
    what proves the sequence is the thing making them unique, not luck.
    """
    generator = IdGenerator(3, clock=lambda: 1_000)
    ids = [generator.next_id() for _ in range(MAX_SEQUENCE)]

    assert len(set(ids)) == MAX_SEQUENCE
    assert {decode(i).timestamp_ms for i in ids} == {1_000}
    assert {decode(i).sequence for i in ids} == set(range(MAX_SEQUENCE))


def test_burst_rolls_into_the_next_millisecond() -> None:
    """Past the sequence width the generator waits for the next ms rather than
    reusing a value. The clock advances only when asked, so this cannot pass by
    accident of real time elapsing."""
    ticks = iter([1_000] * MAX_SEQUENCE + [1_001] * 8)
    generator = IdGenerator(3, clock=lambda: next(ticks))

    ids = [generator.next_id() for _ in range(MAX_SEQUENCE + 1)]

    assert len(set(ids)) == MAX_SEQUENCE + 1
    assert decode(ids[-1]).timestamp_ms == 1_001
    assert decode(ids[-1]).sequence == 0


@settings(deadline=None, max_examples=15, suppress_health_check=[HealthCheck.too_slow])
@given(
    threads=st.integers(min_value=2, max_value=8),
    per_thread=st.integers(min_value=50, max_value=300),
)
def test_concurrent_ids_are_unique(threads: int, per_thread: int) -> None:
    """The uniqueness proof the SPEC asks for.

    Real OS threads, not tasks: the lock inside `next_id` is what is under test,
    and coroutines on one event loop would never contend for it.
    """
    generator = IdGenerator(7)
    results: list[list[int]] = []
    lock = threading.Lock()

    def mint() -> None:
        ids = [generator.next_id() for _ in range(per_thread)]
        with lock:
            results.append(ids)

    workers = [threading.Thread(target=mint) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    flat = [id_ for chunk in results for id_ in chunk]
    assert len(flat) == threads * per_thread
    assert len(set(flat)) == len(flat), "duplicate id under concurrency"


def test_next_id_and_slug_agree() -> None:
    """The slug must be the id, not a second draw from the generator - otherwise
    the row's primary key and its public short code describe different things."""
    id_, slug = IdGenerator(9).next_id_and_slug()
    assert base62_decode(slug) == id_
