//! Systematic Reed–Solomon RS(k, m) — encode / reconstruct.
//!
//! Lab target: **RS(4,2)** — 4 data shards, 2 parity, 6 total. Overhead 1.5×;
//! survives any 2 losses. Start with the hand-checkable **P+Q** construction
//! (docs/12 §4) before generalizing to an arbitrary Vandermonde/Cauchy matrix.
//!
//! ## Observable (SPEC)
//!
//! A blob split into 6 shards reconstructs **bit-exact** after **any 2** are
//! deleted. Proof: unit tests below + `tests/erasure_acceptance.rs`.
//!
//! ## Layout (RS(4,2) P+Q)
//!
//! - Split plaintext into `k` equal-length data shards (zero-pad so `len % k == 0`).
//! - **P** (shard 4): `d0 ⊕ d1 ⊕ d2 ⊕ d3` (coefficients `[1,1,1,1]`).
//! - **Q** (shard 5): `1·d0 ⊕ 2·d1 ⊕ 4·d2 ⊕ 8·d3` (Vandermonde row base 2).
//! - Reconstruct: pick any `k` survivors, invert their `k×k` submatrix of `G`,
//!   multiply to recover data shards (zero-pad from encode is returned as-is).
//!
//! Do not wire into [`crate::store::Store`] until the codec round-trips offline.

use super::gf256::Gf256;
use super::ErasureError;

/// Data shards in the lab RS(4,2) code.
pub const RS_K: usize = 4;
/// Parity shards in the lab RS(4,2) code.
pub const RS_M: usize = 2;
/// Total shards `k + m`.
pub const RS_N: usize = RS_K + RS_M;

/// Q-row coefficients `[2⁰, 2¹, 2², 2³]` for the lab RS(4,2) code.
const Q_COEFF: [u8; RS_K] = [1, 2, 4, 8];

/// Index of a shard in the codeword (`0..n`).
///
/// For systematic RS(4,2): `0..3` are data, `4` is P (XOR), `5` is Q (Vandermonde).
pub type ShardId = usize;

/// One shard of an erasure-coded blob.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Shard {
    pub id: ShardId,
    pub data: Vec<u8>,
}

impl Shard {
    pub fn new(id: ShardId, data: Vec<u8>) -> Self {
        Self { id, data }
    }
}

/// Systematic Reed–Solomon coder over [`Gf256`].
#[derive(Debug, Clone)]
pub struct ReedSolomon {
    pub k: usize,
    pub m: usize,
    pub gf: Gf256,
}

impl ReedSolomon {
    /// Fixed lab code: RS(4,2) with the P+Q generator from docs/12 §4.
    pub fn rs_4_2() -> Self {
        Self {
            k: RS_K,
            m: RS_M,
            gf: Gf256::new(),
        }
    }

    /// `n = k + m` total shards in a codeword.
    #[inline]
    pub const fn n(&self) -> usize {
        self.k + self.m
    }

    /// Encode plaintext → `n` shards (systematic: first `k` are data slices).
    ///
    /// Zero-pads the plaintext so its length is a multiple of `k`, then splits
    /// into `k` contiguous equal-length data shards and appends P and Q.
    ///
    /// # Errors
    ///
    /// Returns [`ErasureError::InvalidLayout`] for empty input, or
    /// [`ErasureError::Unsupported`] if this is not the lab RS(4,2) shape.
    pub fn encode(&self, plaintext: &[u8]) -> Result<Vec<Shard>, ErasureError> {
        assert_eq!(self.k, RS_K, "only RS(4,2) P+Q encode is implemented");
        assert_eq!(self.m, RS_M, "only RS(4,2) P+Q encode is implemented");

        if plaintext.is_empty() {
            return Err(ErasureError::InvalidLayout(
                "plaintext must be non-empty".into(),
            ));
        }

        let (data_shards, shard_len) = {
            let mut padded = plaintext.to_vec();

            let rem = padded.len() % self.k;
            if rem != 0 {
                padded.resize(padded.len() + (self.k - rem), 0);
            }

            let shard_len = padded.len() / self.k;
            let data_shards: Vec<Vec<u8>> = padded.chunks(shard_len).map(<[u8]>::to_vec).collect();
            (data_shards, shard_len)
        };

        // P+Q parity (docs/12 §4): for each byte column i across the k data shards,
        //   P[i] = d0[i] ⊕ d1[i] ⊕ d2[i] ⊕ d3[i]                 // coeffs [1,1,1,1]
        //   Q[i] = 1·d0[i] ⊕ 2·d1[i] ⊕ 4·d2[i] ⊕ 8·d3[i]         // coeffs [2⁰,2¹,2²,2³]
        // All · / ⊕ are GF(2⁸) (mul via tables; add = XOR).
        let (p, q) = {
            let mut p = vec![0u8; shard_len];
            let mut q = vec![0u8; shard_len];
            for i in 0..shard_len {
                let mut p_acc = 0u8;
                let mut q_acc = 0u8;
                for (c, shard) in data_shards.iter().enumerate() {
                    p_acc = Gf256::add(p_acc, shard[i]);
                    q_acc = Gf256::add(q_acc, self.gf.mul(Q_COEFF[c], shard[i]));
                }
                p[i] = p_acc;
                q[i] = q_acc;
            }
            (p, q)
        };

        let mut shards = data_shards
            .into_iter()
            .enumerate()
            .map(|(id, bytes)| Shard::new(id, bytes))
            .collect::<Vec<Shard>>();
        shards.push(Shard::new(self.k, p));
        shards.push(Shard::new(self.k + 1, q));
        Ok(shards)
    }

    /// Reconstruct plaintext from a codeword with erasures.
    ///
    /// `shards[i] == None` means shard `i` is missing/corrupt. Need at least
    /// `k` `Some` entries. Output must equal the original plaintext bit-exact
    /// (for inputs whose length was already a multiple of `k`; zero-padded
    /// trailing bytes from encode are returned as-is).
    ///
    /// # Errors
    ///
    /// [`ErasureError::InvalidLayout`] for wrong slice length / mismatched shard
    /// sizes; [`ErasureError::TooManyErasures`] if fewer than `k` survivors;
    /// [`ErasureError::Singular`] if the survivor submatrix is not invertible.
    pub fn reconstruct(&self, shards: &[Option<Shard>]) -> Result<Vec<u8>, ErasureError> {
        assert_eq!(self.k, RS_K, "only RS(4,2) reconstruct is implemented");
        assert_eq!(self.m, RS_M, "only RS(4,2) reconstruct is implemented");

        if shards.len() != self.n() {
            return Err(ErasureError::InvalidLayout(format!(
                "expected {} shard slots, got {}",
                self.n(),
                shards.len()
            )));
        }

        let survivors = shards
            .iter()
            .enumerate()
            .filter_map(|(id, slot)| slot.as_ref().map(|s| (id, s)))
            .collect::<Vec<(ShardId, &Shard)>>();
        if survivors.len() < self.k {
            return Err(ErasureError::TooManyErasures {
                need: self.k,
                have: survivors.len(),
            });
        }

        // Any k survivors form G' · d = y (docs/12 §2.1). Prefer lowest ids.
        let chosen = &survivors[..self.k];
        let shard_len = chosen[0].1.data.len();
        for &(_, shard) in chosen {
            if shard.data.len() != shard_len {
                return Err(ErasureError::InvalidLayout(
                    "survivor shards have unequal lengths".into(),
                ));
            }
        }

        let mut g_prime = [[0u8; RS_K]; RS_K];
        for (row, &(id, _)) in chosen.iter().enumerate() {
            g_prime[row] = Self::generator_row(id)?;
        }
        let inv = self.invert_square(&g_prime)?;

        let mut data_shards = vec![vec![0u8; shard_len]; self.k];
        for col in 0..shard_len {
            let mut y = [0u8; RS_K];
            for (row, &(_, shard)) in chosen.iter().enumerate() {
                y[row] = shard.data[col];
            }
            for (r, data_row) in data_shards.iter_mut().enumerate() {
                let mut acc = 0u8;
                for c in 0..self.k {
                    acc = Gf256::add(acc, self.gf.mul(inv[r][c], y[c]));
                }
                data_row[col] = acc;
            }
        }

        Ok(data_shards.into_iter().flatten().collect())
    }

    /// Row `id` of the systematic P+Q generator matrix G (n×k).
    fn generator_row(id: ShardId) -> Result<[u8; RS_K], ErasureError> {
        match id {
            0 => Ok([1, 0, 0, 0]),
            1 => Ok([0, 1, 0, 0]),
            2 => Ok([0, 0, 1, 0]),
            3 => Ok([0, 0, 0, 1]),
            4 => Ok([1, 1, 1, 1]), // P
            5 => Ok(Q_COEFF),      // Q
            other => Err(ErasureError::InvalidLayout(format!(
                "shard id {other} out of range for RS(4,2)"
            ))),
        }
    }

    /// Invert a `k×k` matrix over GF(2⁸) via Gauss–Jordan (`[A|I] → [I|A⁻¹]`).
    fn invert_square(&self, a: &[[u8; RS_K]; RS_K]) -> Result<[[u8; RS_K]; RS_K], ErasureError> {
        let mut m = [[0u8; RS_K * 2]; RS_K];
        for (i, row) in a.iter().enumerate() {
            m[i][..RS_K].copy_from_slice(row);
            m[i][RS_K + i] = 1;
        }

        for col in 0..RS_K {
            let pivot = (col..RS_K).find(|&row| m[row][col] != 0).ok_or_else(|| {
                ErasureError::Singular(format!("zero pivot in column {col} while inverting G'"))
            })?;
            if pivot != col {
                m.swap(pivot, col);
            }

            let inv_pivot = self.gf.inv(m[col][col])?;
            for cell in &mut m[col] {
                *cell = self.gf.mul(*cell, inv_pivot);
            }

            for row in 0..RS_K {
                if row == col {
                    continue;
                }
                let factor = m[row][col];
                if factor == 0 {
                    continue;
                }
                let pivot_row = m[col];
                for (cell, &p) in m[row].iter_mut().zip(pivot_row.iter()) {
                    *cell = Gf256::add(*cell, self.gf.mul(factor, p));
                }
            }
        }

        let mut inv = [[0u8; RS_K]; RS_K];
        for (i, row) in inv.iter_mut().enumerate() {
            row.copy_from_slice(&m[i][RS_K..]);
        }
        Ok(inv)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn present_all(shards: &[Shard]) -> Vec<Option<Shard>> {
        shards.iter().cloned().map(Some).collect()
    }

    fn erase(shards: &[Shard], missing: &[usize]) -> Vec<Option<Shard>> {
        let mut present = present_all(shards);
        for &i in missing {
            present[i] = None;
        }
        present
    }

    /// Docs §4.1 single-byte worked example: d=[0x10,0x20,0x30,0x40] → P=0x40, Q=0xAA.
    #[test]
    fn hand_worked_encode_pq() {
        let rs = ReedSolomon::rs_4_2();
        let shards = rs.encode(&[0x10, 0x20, 0x30, 0x40]).expect("encode");
        assert_eq!(shards.len(), RS_N);
        assert_eq!(shards[0].data, vec![0x10]);
        assert_eq!(shards[1].data, vec![0x20]);
        assert_eq!(shards[2].data, vec![0x30]);
        assert_eq!(shards[3].data, vec![0x40]);
        assert_eq!(shards[4].data, vec![0x40], "P");
        assert_eq!(shards[5].data, vec![0xAA], "Q");
    }

    /// Docs §4.2: lose d0 and d1, rebuild from d2,d3,P,Q.
    #[test]
    fn hand_worked_reconstruct_lost_d0_d1() {
        let rs = ReedSolomon::rs_4_2();
        let plaintext = vec![0x10, 0x20, 0x30, 0x40];
        let shards = rs.encode(&plaintext).expect("encode");
        let out = rs
            .reconstruct(&erase(&shards, &[0, 1]))
            .expect("reconstruct");
        assert_eq!(out, plaintext);
    }

    #[test]
    fn reconstruct_with_no_erasures() {
        let rs = ReedSolomon::rs_4_2();
        let plaintext: Vec<u8> = (0u8..32).collect();
        let shards = rs.encode(&plaintext).expect("encode");
        let out = rs.reconstruct(&present_all(&shards)).expect("reconstruct");
        assert_eq!(out, plaintext);
    }

    #[test]
    fn reconstruct_lost_both_parities() {
        let rs = ReedSolomon::rs_4_2();
        let plaintext: Vec<u8> = (0u8..16).collect();
        let shards = rs.encode(&plaintext).expect("encode");
        // G' is identity — should just concat d0..d3.
        let out = rs
            .reconstruct(&erase(&shards, &[4, 5]))
            .expect("reconstruct");
        assert_eq!(out, plaintext);
    }

    #[test]
    fn reconstruct_lost_one_data_and_one_parity() {
        let rs = ReedSolomon::rs_4_2();
        let plaintext = vec![0x11, 0x22, 0x33, 0x44];
        let shards = rs.encode(&plaintext).expect("encode");
        for data_id in 0..RS_K {
            for parity_id in RS_K..RS_N {
                let out = rs
                    .reconstruct(&erase(&shards, &[data_id, parity_id]))
                    .unwrap_or_else(|e| panic!("lost {data_id},{parity_id}: {e}"));
                assert_eq!(out, plaintext, "lost {data_id},{parity_id}");
            }
        }
    }

    #[test]
    fn reconstruct_single_erasure() {
        let rs = ReedSolomon::rs_4_2();
        let plaintext: Vec<u8> = (10u8..42).collect(); // 32 bytes, multiple of k
        let shards = rs.encode(&plaintext).expect("encode");
        for missing in 0..RS_N {
            let out = rs
                .reconstruct(&erase(&shards, &[missing]))
                .unwrap_or_else(|e| panic!("lost {missing}: {e}"));
            assert_eq!(out, plaintext, "lost shard {missing}");
        }
    }

    #[test]
    fn any_two_erasures_round_trip() {
        let rs = ReedSolomon::rs_4_2();
        let plaintext: Vec<u8> = (0u8..64).collect();
        let shards = rs.encode(&plaintext).expect("encode");
        for i in 0..RS_N {
            for j in (i + 1)..RS_N {
                let out = rs
                    .reconstruct(&erase(&shards, &[i, j]))
                    .unwrap_or_else(|e| panic!("lost {i},{j}: {e}"));
                assert_eq!(out, plaintext, "lost shards {i},{j}");
            }
        }
    }

    #[test]
    fn reconstruct_multi_byte_shards() {
        let rs = ReedSolomon::rs_4_2();
        // 4 shards × 5 bytes each
        let plaintext: Vec<u8> = (0u8..20)
            .map(|i| i.wrapping_mul(13).wrapping_add(7))
            .collect();
        let shards = rs.encode(&plaintext).expect("encode");
        assert!(shards.iter().all(|s| s.data.len() == 5));
        let out = rs
            .reconstruct(&erase(&shards, &[1, 4]))
            .expect("reconstruct");
        assert_eq!(out, plaintext);
    }

    #[test]
    fn reconstruct_padded_encode_returns_padded_plaintext() {
        let rs = ReedSolomon::rs_4_2();
        let shards = rs.encode(&[0x10, 0x20, 0x30]).expect("encode");
        let out = rs
            .reconstruct(&erase(&shards, &[0, 5]))
            .expect("reconstruct");
        // encode zero-pads to 4 bytes; reconstruct does not strip
        assert_eq!(out, vec![0x10, 0x20, 0x30, 0x00]);
    }

    #[test]
    fn reconstruct_rejects_wrong_slot_count() {
        let rs = ReedSolomon::rs_4_2();
        let shards = rs.encode(&[1, 2, 3, 4]).expect("encode");
        let too_short: Vec<Option<Shard>> = shards.iter().take(4).cloned().map(Some).collect();
        assert!(matches!(
            rs.reconstruct(&too_short),
            Err(ErasureError::InvalidLayout(_))
        ));
    }

    #[test]
    fn reconstruct_rejects_too_many_erasures() {
        let rs = ReedSolomon::rs_4_2();
        let shards = rs.encode(&[1, 2, 3, 4]).expect("encode");
        // Only 3 survivors (< k=4)
        let present = erase(&shards, &[0, 1, 2]);
        assert_eq!(
            rs.reconstruct(&present),
            Err(ErasureError::TooManyErasures { need: 4, have: 3 })
        );
    }

    #[test]
    fn reconstruct_rejects_unequal_survivor_lengths() {
        let rs = ReedSolomon::rs_4_2();
        let mut shards = rs.encode(&[1, 2, 3, 4]).expect("encode");
        shards[2].data.push(0xFF); // corrupt length on a survivor we will keep
        let present = erase(&shards, &[0, 1]); // survivors: 2,3,4,5 — shard 2 is long
        assert!(matches!(
            rs.reconstruct(&present),
            Err(ErasureError::InvalidLayout(_))
        ));
    }

    #[test]
    fn invert_square_identity_is_identity() {
        let rs = ReedSolomon::rs_4_2();
        let eye = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];
        let inv = rs.invert_square(&eye).expect("invert I");
        assert_eq!(inv, eye);
    }

    #[test]
    fn invert_square_rejects_singular() {
        let rs = ReedSolomon::rs_4_2();
        // Two identical rows → singular
        let singular = [[1, 0, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];
        assert!(matches!(
            rs.invert_square(&singular),
            Err(ErasureError::Singular(_))
        ));
    }

    #[test]
    fn encode_rejects_empty() {
        let rs = ReedSolomon::rs_4_2();
        assert!(matches!(
            rs.encode(&[]),
            Err(ErasureError::InvalidLayout(_))
        ));
    }

    #[test]
    fn encode_pads_to_multiple_of_k() {
        let rs = ReedSolomon::rs_4_2();
        let shards = rs.encode(&[0x10, 0x20, 0x30]).expect("encode");
        assert_eq!(shards.len(), RS_N);
        assert!(shards.iter().all(|s| s.data.len() == 1));
        // padded d3 = 0 → P = 0x10 ⊕ 0x20 ⊕ 0x30 ⊕ 0x00 = 0x00
        assert_eq!(shards[3].data, vec![0x00]);
        assert_eq!(shards[4].data, vec![0x00], "P");
    }

    #[test]
    fn encode_assigns_systematic_shard_ids() {
        let rs = ReedSolomon::rs_4_2();
        let shards = rs.encode(&[9, 8, 7, 6]).expect("encode");
        for (i, shard) in shards.iter().enumerate() {
            assert_eq!(shard.id, i);
        }
    }
}
