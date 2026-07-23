//! Acceptance criteria for the **erasure-coding lab** (From the field).
//!
//! Offline codec tests — this lab is **not** on the S3 HTTP path yet. Drives
//! [`object_store::erasure`] only. All four layers (GF → RS → LRC → durability)
//! are implemented and green.
//!
//! Teach-yourself: `docs/12-how-erasure-coding-works.md`.
//!
//! ## Done when ALL true
//!
//! - [x] **GF(2⁸) tables.** `mul` / `inv` match the hand-worked products in docs/12 §4
//!   (`2·0x20=0x40`, `4·0x30=0xC0`, `8·0x40=0x3A`) and every nonzero has inverse.
//!   *Proof: `gf256_hand_worked_and_inverses` (and `src/erasure/gf256.rs` unit tests).*
//! - [x] **RS(4,2) round-trip.** Encode a blob into 6 shards; after deleting **any 2**,
//!   `reconstruct` returns the plaintext bit-exact.
//!   *Proof: `rs_4_2_survives_any_two_erasures`.*
//! - [x] **LRC single-shard fan-in.** With `(k=4,l=2,r=2)`, repairing one lost data
//!   shard reads ≈ `k/l = 2` shards (local), not `k = 4`.
//!   *Proof: `lrc_single_shard_repair_fan_in`.*
//! - [x] **Durability calculator.** `(k,m,AFR,repair_window)` → nines; Backblaze
//!   17+3 ≈ 11 nines, lab RS(4,2) ≈ 9 nines under the same AFR/window.
//!   *Proof: `durability_nines_in_expected_bands` + assumptions noted in bench doc.*

use object_store::erasure::durability::{compute_durability, DurabilityInput};
use object_store::erasure::gf256::Gf256;
use object_store::erasure::lrc::Lrc;
use object_store::erasure::reed_solomon::{ReedSolomon, Shard, RS_N};
use object_store::erasure::RepairStats;

#[test]
fn gf256_hand_worked_and_inverses() {
    let gf = Gf256::new();
    assert_eq!(gf.mul(2, 0x20), 0x40);
    assert_eq!(gf.mul(4, 0x30), 0xC0);
    assert_eq!(gf.mul(8, 0x40), 0x3A);
    for a in 1u8..=255 {
        assert_eq!(gf.mul(a, gf.inv(a).unwrap()), 1);
    }
}

#[test]
fn rs_4_2_survives_any_two_erasures() {
    let rs = ReedSolomon::rs_4_2();
    let plaintext: Vec<u8> = (0u8..128).map(|i| i.wrapping_mul(17)).collect();
    let shards = rs.encode(&plaintext).expect("encode");
    assert_eq!(shards.len(), RS_N);

    for i in 0..RS_N {
        for j in (i + 1)..RS_N {
            let mut present: Vec<Option<Shard>> = shards.iter().cloned().map(Some).collect();
            present[i] = None;
            present[j] = None;
            let out = rs
                .reconstruct(&present)
                .unwrap_or_else(|e| panic!("lost {i},{j}: {e}"));
            assert_eq!(out, plaintext, "lost shards {i} and {j}");
        }
    }
}

#[test]
fn lrc_single_shard_repair_fan_in() {
    let lrc = Lrc::lab();
    let plaintext: Vec<u8> = (0u8..64).collect();
    let shards = lrc.encode(&plaintext).expect("encode");
    let missing = 0usize;
    let mut present: Vec<Option<Shard>> = shards.into_iter().map(Some).collect();
    present[missing] = None;

    let (_rebuilt, stats): (_, RepairStats) = lrc.repair_one(&present, missing).expect("repair");
    assert!(stats.used_local);
    assert_eq!(stats.shards_read, lrc.params.local_repair_fan_in());
}

#[test]
fn durability_nines_in_expected_bands() {
    let bb = compute_durability(DurabilityInput::backblaze_17_3()).expect("17+3");
    assert!(
        bb.nines > 10.0 && bb.nines < 12.0,
        "Backblaze band: got {}",
        bb.nines
    );

    let lab = compute_durability(DurabilityInput::lab_rs_4_2()).expect("4+2");
    assert!(
        lab.nines > 8.0 && lab.nines < 10.5,
        "lab RS(4,2) band: got {}",
        lab.nines
    );
}
