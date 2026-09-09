"""The erasure-coding lab: GF(2⁸), RS(4,2), LRC, and the nines calculator."""

from __future__ import annotations

import itertools
import os

import pytest

from object_store.erasure import (
    DurabilityInput,
    Gf256,
    InvalidLayout,
    Lrc,
    ReedSolomon,
    Singular,
    TooManyErasures,
    compute_durability,
)
from object_store.erasure.reed_solomon import Shard

# ── GF(2⁸) ──────────────────────────────────────────────────────────────────


def test_addition_is_xor_and_is_its_own_inverse() -> None:
    for a, b in itertools.product(range(0, 256, 17), repeat=2):
        assert Gf256.add(a, b) == a ^ b
        assert Gf256.add(Gf256.add(a, b), b) == a


def test_xtime_matches_the_hand_worked_values() -> None:
    """Doubling, with the reduction when the high bit overflows (docs/12 §3)."""
    assert Gf256.xtime(0x01) == 0x02
    assert Gf256.xtime(0x40) == 0x80
    assert Gf256.xtime(0x80) == 0x1D  # overflowed → XOR 0x11D's low byte
    assert Gf256.xtime(0x87) == 0x13  # (0x87 << 1) & 0xFF = 0x0E, ^ 0x1D = 0x13


def test_multiplication_is_closed_commutative_and_has_an_identity() -> None:
    gf = Gf256()
    for a, b in itertools.product(range(0, 256, 13), repeat=2):
        product = gf.mul(a, b)
        assert 0 <= product <= 255
        assert product == gf.mul(b, a)
    for a in range(256):
        assert gf.mul(a, 1) == a
        assert gf.mul(a, 0) == 0


def test_every_nonzero_element_has_an_inverse() -> None:
    """This is what makes the reconstruction matrix invertible."""
    gf = Gf256()
    for a in range(1, 256):
        assert gf.mul(a, gf.inv(a)) == 1


def test_zero_has_no_inverse() -> None:
    with pytest.raises(Singular):
        Gf256().inv(0)


def test_division_undoes_multiplication() -> None:
    gf = Gf256()
    for a, b in itertools.product(range(1, 256, 29), repeat=2):
        assert gf.div(gf.mul(a, b), b) == a


def test_two_is_a_generator() -> None:
    """Doubling from 1 walks all 255 nonzero elements — why log tables work."""
    gf = Gf256()
    assert sorted(gf.antilog[:255]) == list(range(1, 256))


# ── Reed–Solomon ────────────────────────────────────────────────────────────


def test_encode_is_systematic() -> None:
    """The first k shards *are* the plaintext — an intact read decodes nothing."""
    coder = ReedSolomon.rs_4_2()
    payload = bytes(range(64))
    shards = coder.encode(payload)

    assert len(shards) == 6
    assert b"".join(shard.data for shard in shards[:4]) == payload


def test_rs_4_2_survives_any_two_erasures() -> None:
    """The SPEC's criterion, over *every* pair rather than a hand-picked one."""
    coder = ReedSolomon.rs_4_2()
    payload = bytes(range(256)) * 4
    shards = coder.encode(payload)

    for lost in itertools.combinations(range(6), 2):
        damaged: list[Shard | None] = [None if shard.id in lost else shard for shard in shards]
        assert coder.reconstruct(damaged) == payload, f"lost {lost}"


def test_rs_4_2_survives_any_single_erasure() -> None:
    coder = ReedSolomon.rs_4_2()
    payload = os.urandom(128)
    shards = coder.encode(payload)

    for lost in range(6):
        damaged: list[Shard | None] = [None if shard.id == lost else shard for shard in shards]
        assert coder.reconstruct(damaged) == payload


def test_three_erasures_are_beyond_the_code() -> None:
    """Honest failure, not a wrong answer: `m = 2` means two."""
    coder = ReedSolomon.rs_4_2()
    shards = coder.encode(b"x" * 64)
    damaged: list[Shard | None] = [None if shard.id in (0, 1, 4) else shard for shard in shards]

    with pytest.raises(TooManyErasures) as caught:
        coder.reconstruct(damaged)
    assert caught.value.need == 4
    assert caught.value.have == 3


def test_a_length_not_divisible_by_k_is_zero_padded() -> None:
    coder = ReedSolomon.rs_4_2()
    payload = b"seven!!"

    recovered = coder.reconstruct(
        [None, *coder.encode(payload)[1:]]  # type: ignore[list-item]
    )
    assert recovered.startswith(payload)
    assert len(recovered) % 4 == 0


def test_encoding_empty_input_is_rejected() -> None:
    with pytest.raises(InvalidLayout):
        ReedSolomon.rs_4_2().encode(b"")


def test_reconstruct_rejects_a_wrong_slot_count() -> None:
    with pytest.raises(InvalidLayout):
        ReedSolomon.rs_4_2().reconstruct([None, None])


# ── LRC ─────────────────────────────────────────────────────────────────────


def test_lrc_encodes_eight_shards() -> None:
    shards = Lrc.lab().encode(bytes(range(64)))
    assert len(shards) == 8
    assert [shard.id for shard in shards] == list(range(8))


def test_lrc_single_shard_repair_fan_in_beats_plain_rs() -> None:
    """The whole claim of LRC, measured rather than assumed.

    A lost data shard costs 2 reads (its local group) instead of the 4 plain RS
    would pay — `k/l` versus `k`.
    """
    lrc = Lrc.lab()
    payload = bytes(range(256))
    shards = lrc.encode(payload)

    for lost in range(4):
        damaged: list[Shard | None] = [None if shard.id == lost else shard for shard in shards]
        rebuilt, stats = lrc.repair_one(damaged, lost)

        assert rebuilt.data == shards[lost].data
        assert stats.used_local
        assert stats.shards_read == lrc.params.local_repair_fan_in == 2


def test_a_lost_local_parity_also_repairs_locally() -> None:
    lrc = Lrc.lab()
    shards = lrc.encode(bytes(range(128)))

    for lost in (4, 5):
        damaged: list[Shard | None] = [None if shard.id == lost else shard for shard in shards]
        rebuilt, stats = lrc.repair_one(damaged, lost)
        assert rebuilt.data == shards[lost].data
        assert stats.used_local


def test_a_lost_global_parity_costs_the_full_fan_in() -> None:
    """Globals span every group, so they belong to none — hence `k` reads."""
    lrc = Lrc.lab()
    shards = lrc.encode(bytes(range(128)))

    for lost in (6, 7):
        damaged: list[Shard | None] = [None if shard.id == lost else shard for shard in shards]
        rebuilt, stats = lrc.repair_one(damaged, lost)
        assert rebuilt.data == shards[lost].data
        assert not stats.used_local
        assert stats.shards_read == lrc.params.k


def test_an_incomplete_local_group_falls_back_to_the_globals() -> None:
    lrc = Lrc.lab()
    shards = lrc.encode(bytes(range(128)))
    # Lose a data shard *and* its group's local parity, so the cheap path
    # cannot be taken.
    damaged: list[Shard | None] = [None if shard.id in (0, 4) else shard for shard in shards]

    rebuilt, stats = lrc.repair_one(damaged, 0)
    assert rebuilt.data == shards[0].data
    assert not stats.used_local
    assert stats.shards_read == lrc.params.k


# ── durability ──────────────────────────────────────────────────────────────


def test_backblaze_lands_around_eleven_nines() -> None:
    report = compute_durability(DurabilityInput.backblaze_17_3())
    assert 10.0 <= report.nines <= 13.0


def test_the_lab_code_lands_around_nine_nines() -> None:
    report = compute_durability(DurabilityInput.lab_rs_4_2())
    assert 8.0 <= report.nines <= 11.0


def test_more_parity_means_more_nines() -> None:
    base = DurabilityInput(k=10, m=2, annual_failure_rate=0.005, repair_window_hours=72)
    more = DurabilityInput(k=10, m=4, annual_failure_rate=0.005, repair_window_hours=72)

    assert compute_durability(more).nines > compute_durability(base).nines


def test_a_faster_repair_window_dominates() -> None:
    """It appears twice in the model — halving it beats adding a parity shard."""
    slow = DurabilityInput(k=10, m=2, annual_failure_rate=0.005, repair_window_hours=336)
    fast = DurabilityInput(k=10, m=2, annual_failure_rate=0.005, repair_window_hours=24)

    assert compute_durability(fast).nines > compute_durability(slow).nines


def test_impossible_inputs_are_rejected() -> None:
    from object_store.erasure import Unsupported

    for spec in (
        DurabilityInput(k=0, m=2, annual_failure_rate=0.01, repair_window_hours=24),
        DurabilityInput(k=4, m=2, annual_failure_rate=0.01, repair_window_hours=0),
        DurabilityInput(k=4, m=2, annual_failure_rate=1.5, repair_window_hours=24),
    ):
        with pytest.raises(Unsupported):
            compute_durability(spec)
