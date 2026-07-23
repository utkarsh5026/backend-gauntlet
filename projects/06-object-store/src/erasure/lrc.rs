//! Local Reconstruction Codes — cheap repair for the common case (one lost shard).
//!
//! Plain RS repairs one failure by reading **all `k`** other shards. LRC splits
//! data into `l` local groups, adds one local parity per group, plus `r` global
//! (RS-style) parities. A single lost shard repairs from **≈ `k/l`** reads.
//!
//! Lab target from docs/12 §6.1: **`(k=4, l=2, r=2)`** — 4 data + 2 local + 2
//! global = 8 shards. Repairing one data shard reads 2 (local group), not 4.
//!
//! ## Observable (SPEC)
//!
//! Measure repair-read fan-in both ways: LRC single-shard repair ≈ `k/l`, plain
//! RS = `k`. Bodies are `todo!()`; reuse [`super::reed_solomon`] / [`super::gf256`]
//! for the global parities.

use super::gf256::Gf256;
use super::reed_solomon::Shard;
use super::ErasureError;

/// Lab LRC shape constants (docs/12 §6.1): `(k=4, l=2, r=2)`.
const LAB_K: usize = 4;
const LAB_L: usize = 2;
const LAB_R: usize = 2;
/// Data shards per local group when the `k` data shards split evenly across `l`.
const GROUP_SIZE: usize = LAB_K / LAB_L; // 2

/// Global (RS-style) parity rows over the `k` data shards, docs/12 §6.1.
/// Same GF(2⁸) tables and Vandermonde flavour as [`super::reed_solomon`]'s Q row —
/// LRC reorganizes which shards a parity covers, it does not invent new math.
const GLOBAL_COEFFS: [[u8; LAB_K]; LAB_R] = [
    [1, 2, 4, 8],  // G0 = 1·d0 ⊕ 2·d1 ⊕ 4·d2 ⊕ 8·d3
    [1, 3, 9, 27], // G1 = 1·d0 ⊕ 3·d1 ⊕ 9·d2 ⊕ 27·d3
];

/// LRC parameters `(k, l, r)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LrcParams {
    /// Data shards.
    pub k: usize,
    /// Number of local groups (one local parity each).
    pub l: usize,
    /// Global (RS-style) parity shards.
    pub r: usize,
}

impl LrcParams {
    /// Docs §6.1 lab shape: 4 data, 2 local groups, 2 global parities.
    pub const LAB_4_2_2: Self = Self { k: 4, l: 2, r: 2 };

    /// Total shards = data + local parities + global parities.
    pub fn n(self) -> usize {
        self.k + self.l + self.r
    }

    /// Ideal single-shard repair-read fan-in ≈ `k / l` (when groups are equal).
    pub fn local_repair_fan_in(self) -> usize {
        self.k / self.l
    }
}

/// How many shards a repair actually touched — the metric the SPEC wants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RepairStats {
    /// Shards read to rebuild the missing one.
    pub shards_read: usize,
    /// Whether repair used a local group (`true`) or fell back to globals.
    pub used_local: bool,
}

/// Local Reconstruction Code coder.
#[derive(Debug, Clone)]
pub struct Lrc {
    pub params: LrcParams,
    pub gf: Gf256,
}

impl Lrc {
    /// Lab LRC `(4, 2, 2)`.
    pub fn lab() -> Self {
        Self {
            params: LrcParams::LAB_4_2_2,
            gf: Gf256::new(),
        }
    }

    /// Encode plaintext → `n = k + l + r` shards (docs/12 §6.1).
    ///
    /// Systematic layout: ids `0..k` data, `k..k+l` local XOR parities (one per
    /// group), `k+l..k+l+r` global RS-style parities across all `k` data shards.
    ///
    /// # Errors
    ///
    /// [`ErasureError::InvalidLayout`] on empty input; [`ErasureError::Unsupported`]
    /// if this is not the lab `(4, 2, 2)` shape (the coefficient tables are fixed).
    pub fn encode(&self, plaintext: &[u8]) -> Result<Vec<Shard>, ErasureError> {
        self.assert_lab_shape()?;
        if plaintext.is_empty() {
            return Err(ErasureError::InvalidLayout(
                "plaintext must be non-empty".into(),
            ));
        }

        // Zero-pad to a multiple of k, then split into k contiguous data shards.
        let k = self.params.k;
        let mut padded = plaintext.to_vec();
        let rem = padded.len() % k;
        if rem != 0 {
            padded.resize(padded.len() + (k - rem), 0);
        }
        let shard_len = padded.len() / k;
        let data: Vec<Vec<u8>> = padded.chunks(shard_len).map(<[u8]>::to_vec).collect();

        let mut shards: Vec<Shard> = data
            .iter()
            .enumerate()
            .map(|(id, bytes)| Shard::new(id, bytes.clone()))
            .collect();

        // Local parities: pure XOR over each group's data shards (docs/12 §6.1).
        for g in 0..self.params.l {
            let members = self.group_data_ids(g);
            let mut parity = vec![0u8; shard_len];
            for &d in &members {
                for (col, byte) in parity.iter_mut().enumerate() {
                    *byte = Gf256::add(*byte, data[d][col]);
                }
            }
            shards.push(Shard::new(k + g, parity));
        }

        // Global parities: RS-style Vandermonde rows over ALL k data shards.
        for (t, coeffs) in GLOBAL_COEFFS.iter().enumerate() {
            let mut parity = vec![0u8; shard_len];
            for (byte_idx, byte) in parity.iter_mut().enumerate() {
                let mut acc = 0u8;
                for (c, chunk) in data.iter().enumerate() {
                    acc = Gf256::add(acc, self.gf.mul(coeffs[c], chunk[byte_idx]));
                }
                *byte = acc;
            }
            shards.push(Shard::new(k + self.params.l + t, parity));
        }

        Ok(shards)
    }

    /// Repair a single missing shard; return the rebuilt shard + read fan-in.
    ///
    /// Prefers the local group (`used_local = true`, `shards_read ≈ k/l`): a lost
    /// data or local-parity shard is the XOR of the rest of its group. Falls back
    /// to the global parities (`used_local = false`, `shards_read = k`) when the
    /// local group is itself incomplete, or when the missing shard *is* a global.
    ///
    /// # Errors
    ///
    /// [`ErasureError::InvalidLayout`] for a wrong slot count or an out-of-range
    /// `missing`; [`ErasureError::TooManyErasures`] if neither the local group nor
    /// the globals leave enough survivors to rebuild it.
    pub fn repair_one(
        &self,
        shards: &[Option<Shard>],
        missing: usize,
    ) -> Result<(Shard, RepairStats), ErasureError> {
        self.assert_lab_shape()?;
        let n = self.params.n();
        if shards.len() != n {
            return Err(ErasureError::InvalidLayout(format!(
                "expected {n} shard slots, got {}",
                shards.len()
            )));
        }
        if missing >= n {
            return Err(ErasureError::InvalidLayout(format!(
                "missing id {missing} out of range 0..{n}"
            )));
        }

        // Fast path: rebuild from the missing shard's local group with one XOR pass.
        if let Some(group) = self.local_group_of(missing) {
            let peers: Vec<usize> = group.into_iter().filter(|&id| id != missing).collect();
            if peers.iter().all(|&id| shards[id].is_some()) {
                let shard_len = shards[peers[0]].as_ref().unwrap().data.len();
                let mut data = vec![0u8; shard_len];
                for &id in &peers {
                    let s = shards[id].as_ref().unwrap();
                    for (col, byte) in data.iter_mut().enumerate() {
                        *byte = Gf256::add(*byte, s.data[col]);
                    }
                }
                return Ok((
                    Shard::new(missing, data),
                    RepairStats {
                        shards_read: peers.len(),
                        used_local: true,
                    },
                ));
            }
        }

        // Fallback: use a global parity row. Needs every OTHER data shard present.
        self.repair_via_global(shards, missing)
    }

    /// Global-parity fallback for a single missing data or local-parity shard.
    ///
    /// Recomputes the shard from all `k` data shards (recovering the one missing
    /// data shard first via a global row whose coefficient on it is nonzero).
    /// Reads `k` shards — the same fan-in plain RS pays, which is exactly the cost
    /// LRC's local groups avoid in the common case.
    fn repair_via_global(
        &self,
        shards: &[Option<Shard>],
        missing: usize,
    ) -> Result<(Shard, RepairStats), ErasureError> {
        let k = self.params.k;

        // Collect the k data shards, tolerating at most the one we're rebuilding.
        let mut data: Vec<Option<&Vec<u8>>> = (0..k)
            .map(|id| shards[id].as_ref().map(|s| &s.data))
            .collect();
        let missing_data: Vec<usize> = (0..k).filter(|&id| data[id].is_none()).collect();

        // More than one hole among the data shards is beyond a single-shard repair.
        let recovered_missing_data: Vec<u8>;
        if missing_data.len() > 1 || (missing_data.len() == 1 && missing_data[0] != missing) {
            return Err(ErasureError::TooManyErasures {
                need: k,
                have: k - missing_data.len(),
            });
        }
        if let Some(&hole) = missing_data.first() {
            // Recover the missing data shard d[hole] from a present global parity:
            //   G = Σ coeff[c]·d[c]  ⇒  d[hole] = (G ⊕ Σ_{c≠hole} coeff[c]·d[c]) / coeff[hole]
            let (coeffs, parity) =
                self.available_global(shards)
                    .ok_or(ErasureError::TooManyErasures {
                        need: k,
                        have: k - 1,
                    })?;
            let shard_len = parity.len();
            let coeff_hole = coeffs[hole];
            let inv = self.gf.inv(coeff_hole)?;
            let mut rebuilt = vec![0u8; shard_len];
            for (col, byte) in rebuilt.iter_mut().enumerate() {
                let mut acc = parity[col];
                for c in 0..k {
                    if c == hole {
                        continue;
                    }
                    acc = Gf256::add(acc, self.gf.mul(coeffs[c], data[c].unwrap()[col]));
                }
                *byte = self.gf.mul(acc, inv);
            }
            recovered_missing_data = rebuilt;
            data[hole] = Some(&recovered_missing_data);
        }

        // Every data shard is now known; recompute whichever shard was asked for.
        let shard_bytes = self.recompute_shard(missing, &data);
        Ok((
            Shard::new(missing, shard_bytes),
            RepairStats {
                shards_read: k,
                used_local: false,
            },
        ))
    }

    /// The first present global parity as `(coefficients, bytes)`, if any.
    fn available_global<'a>(
        &self,
        shards: &'a [Option<Shard>],
    ) -> Option<(&'static [u8; LAB_K], &'a Vec<u8>)> {
        let base = self.params.k + self.params.l;
        (0..self.params.r).find_map(|t| {
            shards[base + t]
                .as_ref()
                .map(|s| (&GLOBAL_COEFFS[t], &s.data))
        })
    }

    /// Recompute shard `id` from the full set of `k` data shards.
    fn recompute_shard(&self, id: usize, data: &[Option<&Vec<u8>>]) -> Vec<u8> {
        let k = self.params.k;
        let shard_len = data.iter().flatten().next().map_or(0, |d| d.len());
        if id < k {
            return data[id].unwrap().clone();
        }
        if id < k + self.params.l {
            let g = id - k;
            let mut parity = vec![0u8; shard_len];
            for &d in &self.group_data_ids(g) {
                for (col, byte) in parity.iter_mut().enumerate() {
                    *byte = Gf256::add(*byte, data[d].unwrap()[col]);
                }
            }
            return parity;
        }
        let coeffs = &GLOBAL_COEFFS[id - k - self.params.l];
        let mut parity = vec![0u8; shard_len];
        for (col, byte) in parity.iter_mut().enumerate() {
            let mut acc = 0u8;
            for (c, d) in data.iter().enumerate() {
                acc = Gf256::add(acc, self.gf.mul(coeffs[c], d.unwrap()[col]));
            }
            *byte = acc;
        }
        parity
    }

    /// Data shard ids belonging to local group `g` (equal-sized groups).
    fn group_data_ids(&self, g: usize) -> Vec<usize> {
        let start = g * GROUP_SIZE;
        (start..start + GROUP_SIZE).collect()
    }

    /// Ids in the local group that covers shard `id`: its data shards + local
    /// parity. `None` for a global parity (globals have no local group).
    fn local_group_of(&self, id: usize) -> Option<Vec<usize>> {
        let k = self.params.k;
        let g = if id < k {
            id / GROUP_SIZE
        } else if id < k + self.params.l {
            id - k
        } else {
            return None;
        };
        let mut members = self.group_data_ids(g);
        members.push(k + g); // the group's local parity
        Some(members)
    }

    /// The coefficient tables are hard-wired to `(4, 2, 2)`, mirroring how
    /// [`super::reed_solomon`] asserts its RS(4,2) shape.
    fn assert_lab_shape(&self) -> Result<(), ErasureError> {
        let p = self.params;
        if (p.k, p.l, p.r) != (LAB_K, LAB_L, LAB_R) {
            return Err(ErasureError::Unsupported(format!(
                "only lab LRC(4,2,2) is implemented, got ({}, {}, {})",
                p.k, p.l, p.r
            )));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lab_params_fan_in() {
        let p = LrcParams::LAB_4_2_2;
        assert_eq!(p.n(), 8);
        assert_eq!(p.local_repair_fan_in(), 2); // k/l = 4/2
    }

    #[test]
    fn single_data_shard_repairs_with_local_fan_in() {
        let lrc = Lrc::lab();
        let plaintext: Vec<u8> = (0u8..32).collect();
        let shards = lrc.encode(&plaintext).expect("encode");
        let missing = 0usize; // d0 in group A
        let mut present: Vec<Option<Shard>> = shards.into_iter().map(Some).collect();
        present[missing] = None;
        let (rebuilt, stats) = lrc.repair_one(&present, missing).expect("repair");
        assert_eq!(rebuilt.id, missing);
        assert!(stats.used_local);
        assert_eq!(
            stats.shards_read,
            lrc.params.local_repair_fan_in(),
            "SPEC: single-shard repair reads ≈ k/l, not k"
        );
    }
}
