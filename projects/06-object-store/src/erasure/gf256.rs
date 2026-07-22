//! GF(2⁸) arithmetic — the number system every shard byte lives in.
//!
//! Ordinary integer math overflows and rounds. Bytes need a field that is
//! **closed over `0..255`**, has inverses for every nonzero element, and where
//! add = subtract = XOR. That field is GF(2⁸) with reduction polynomial
//! **`0x11D`** (AES / RAID-6 convention).
//!
//! ## Layout
//!
//! 1. Precompute `log[256]` / `antilog[256]` once via repeated [`Gf256::xtime`] from 1.
//! 2. [`Gf256::mul`] is `antilog[(log[a] + log[b]) mod 255]` (with `0` short-circuit).
//! 3. [`Gf256::inv`] / [`Gf256::div`] from the tables (`a⁻¹ = antilog[255 − log[a]]`).
//!
//! Bit-exact against the hand-worked products in
//! [`docs/12-how-erasure-coding-works.md`](../../../docs/12-how-erasure-coding-works.md)
//! §3–§4 — a wrong table silently corrupts every shard.
//!
//! `add` is free (XOR).

use super::ErasureError;

/// GF(2⁸) with AES/RAID-6 reduction polynomial `0x11D`.
///
/// Hold one of these (or a `&'static`) and pass it into the RS / LRC encoders.
#[derive(Debug, Clone)]
pub struct Gf256 {
    /// `log[x] = i` such that `2ⁱ ≡ x` (undefined / unused at `log[0]`).
    pub log: [u8; 256],
    /// `antilog[i] = 2ⁱ` for `i` in `0..255` (index 255 mirrors 0: `2²⁵⁵ = 2⁰ = 1`).
    pub antilog: [u8; 256],
}

impl Gf256 {
    /// Irreducible polynomial used by AES and Linux RAID-6 (`x⁸ + x⁴ + x³ + x² + 1`).
    pub const REDUCTION_POLY: u16 = 0x11D;

    /// Build log/antilog tables. Call once; reuse.
    pub fn new() -> Self {
        let mut log = [0; 256];
        let mut antilog = [0; 256];
        let mut x = 1u8;
        for (i, slot) in antilog[..255].iter_mut().enumerate() {
            log[x as usize] = i as u8;
            *slot = x;
            x = Self::xtime(x);
        }
        antilog[255] = antilog[0];
        Self { log, antilog }
    }

    /// Multiply by 2 in GF(2⁸): left-shift, XOR `0x1D` if the high bit was set.
    ///
    /// Useful both for building tables and for the hand-checkable Q parity row.
    #[inline]
    pub fn xtime(a: u8) -> u8 {
        let hi = a & 0x80;
        let shifted = a << 1;
        if hi != 0 {
            shifted ^ 0x1D
        } else {
            shifted
        }
    }

    /// Addition = subtraction = XOR. Implemented — it is the definition of the field.
    #[inline]
    pub fn add(a: u8, b: u8) -> u8 {
        a ^ b
    }

    /// Field multiply via log/antilog tables.
    #[inline]
    pub fn mul(&self, a: u8, b: u8) -> u8 {
        if a == 0 || b == 0 {
            return 0;
        }
        let i = self.log[a as usize] as u16 + self.log[b as usize] as u16;
        self.antilog[(i % 255) as usize]
    }

    /// Multiplicative inverse of a nonzero element.
    pub fn inv(&self, a: u8) -> Result<u8, ErasureError> {
        if a == 0 {
            return Err(ErasureError::Singular(
                "multiplicative inverse of 0 is undefined".into(),
            ));
        }
        Ok(self.antilog[(255 - self.log[a as usize] as u16) as usize])
    }

    /// `a / b = a · b⁻¹`.
    pub fn div(&self, a: u8, b: u8) -> Result<u8, ErasureError> {
        Ok(self.mul(a, self.inv(b)?))
    }
}

impl Default for Gf256 {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Docs §4: `2·0x20 = 0x40`, `4·0x30 = 0xC0`, `8·0x40 = 0x3A`.
    #[test]
    fn hand_worked_products_from_doc() {
        let gf = Gf256::new();
        assert_eq!(gf.mul(2, 0x20), 0x40);
        assert_eq!(gf.mul(4, 0x30), 0xC0);
        assert_eq!(gf.mul(8, 0x40), 0x3A);
        assert_eq!(Gf256::xtime(0x20), 0x40);
        assert_eq!(Gf256::xtime(0x80), 0x1D); // fold: (0x80<<1) ⊕ 0x11D low byte
    }

    #[test]
    fn mul_inv_round_trip_nonzero() {
        let gf = Gf256::new();
        for a in 1u8..=255 {
            let inv = gf.inv(a).expect("nonzero invertible");
            assert_eq!(gf.mul(a, inv), 1, "a={a:#x}");
        }
    }

    #[test]
    fn inv_of_zero_is_singular() {
        let gf = Gf256::new();
        assert!(matches!(gf.inv(0), Err(ErasureError::Singular(_))));
        assert!(matches!(gf.div(1, 0), Err(ErasureError::Singular(_))));
    }

    #[test]
    fn mul_by_zero_is_zero() {
        let gf = Gf256::new();
        assert_eq!(gf.mul(0, 0xAB), 0);
        assert_eq!(gf.mul(0xAB, 0), 0);
        assert_eq!(gf.div(0, 0xAB).unwrap(), 0);
    }

    #[test]
    fn add_is_xor() {
        assert_eq!(Gf256::add(0x10, 0x20), 0x30);
        assert_eq!(Gf256::add(0x30, 0x30), 0x00);
    }

    #[test]
    fn generator_cycles_all_nonzero() {
        let gf = Gf256::new();
        let mut seen = [false; 256];
        for i in 0..255 {
            let x = gf.antilog[i];
            assert_ne!(x, 0);
            assert!(!seen[x as usize], "duplicate antilog[{i}]={x:#x}");
            seen[x as usize] = true;
            assert_eq!(gf.log[x as usize] as usize, i);
        }
        assert_eq!(gf.antilog[255], 1);
    }
}
