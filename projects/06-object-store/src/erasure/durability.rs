//! Durability ("nines") calculator — Backblaze-style.
//!
//! Erasure coding does **not** magically equal eleven nines. Durability is a
//! function of `(k, m)`, per-shard annual failure rate, and repair window.
//! This module turns those inputs into an annual survival probability and a
//! nines count you can defend in a bench doc.
//!
//! Formula sketch (docs/12 §7 — sum the full binomial, not only the dominant term):
//!
//! ```text
//! p     = AFR × (repair_hours / 8760)
//! P(loss in one window) = Σ_{j=m+1..n} C(n,j) · p^j · (1−p)^{n−j}
//! annual_durability     = (1 − P_loss_window) ^ (8760 / repair_hours)
//! nines                 = −log10(1 − annual_durability)
//! ```
//!
//! Reference values from the doc (order-of-magnitude checks, not bit-exact):
//! - Backblaze `(17,3)`, AFR≈0.00405, 156h → ~11 nines
//! - Lab RS(4,2), same AFR/window → ~9 nines

use super::ErasureError;

/// Inputs to the durability model.
#[derive(Debug, Clone, Copy)]
pub struct DurabilityInput {
    /// Data shards.
    pub k: u32,
    /// Parity shards (survive any `m` failures).
    pub m: u32,
    /// Annual failure rate per shard/disk (e.g. `0.00405` ≈ 0.4%).
    pub annual_failure_rate: f64,
    /// Hours to detect + rebuild one lost shard (e.g. `156.0`).
    pub repair_window_hours: f64,
}

impl DurabilityInput {
    /// Backblaze's published shape (docs/12 §7.2).
    pub fn backblaze_17_3() -> Self {
        Self {
            k: 17,
            m: 3,
            annual_failure_rate: 0.00405,
            repair_window_hours: 156.0,
        }
    }

    /// Lab RS(4,2) with the same AFR / window (docs/12 §7.3).
    pub fn lab_rs_4_2() -> Self {
        Self {
            k: 4,
            m: 2,
            annual_failure_rate: 0.00405,
            repair_window_hours: 156.0,
        }
    }

    pub fn n(self) -> u32 {
        self.k + self.m
    }
}

/// Calculator output — put the assumptions next to these numbers in the bench doc.
#[derive(Debug, Clone, Copy)]
pub struct DurabilityReport {
    pub n: u32,
    pub p_fail_in_window: f64,
    pub p_loss_in_window: f64,
    pub windows_per_year: f64,
    pub annual_durability: f64,
    /// Approximate count of leading nines (`−log10(1 − durability)`).
    pub nines: f64,
}

/// Binomial coefficient `C(n, j)` as `f64` (n ≤ 20 here, so no overflow / no factorials).
///
/// Multiplicative form `∏ (n−i)/(i+1)` keeps every partial product an integer-valued
/// float and dodges the huge intermediate `n!` a naive `n!/(j!(n−j)!)` would build.
fn binomial(n: u32, j: u32) -> f64 {
    let mut acc = 1.0f64;
    for i in 0..j {
        acc = acc * f64::from(n - i) / f64::from(i + 1);
    }
    acc
}

/// Compute annual durability and nines from [`DurabilityInput`] (docs/12 §7).
///
/// # Errors
///
/// [`ErasureError::Unsupported`] if the inputs are out of range (non-positive
/// repair window, AFR outside `0..=1`, or a per-window failure probability that
/// escapes `0..=1`).
pub fn compute_durability(input: DurabilityInput) -> Result<DurabilityReport, ErasureError> {
    if input.k == 0 {
        return Err(ErasureError::Unsupported("k must be > 0".into()));
    }
    if input.repair_window_hours <= 0.0 {
        return Err(ErasureError::Unsupported(
            "repair_window_hours must be > 0".into(),
        ));
    }
    if !(0.0..=1.0).contains(&input.annual_failure_rate) {
        return Err(ErasureError::Unsupported(
            "annual_failure_rate must be in 0..=1".into(),
        ));
    }

    const HOURS_PER_YEAR: f64 = 8760.0;
    let n = input.n();

    // Step 1: probability ONE shard fails inside a single repair window.
    let p = input.annual_failure_rate * (input.repair_window_hours / HOURS_PER_YEAR);
    if !(0.0..=1.0).contains(&p) {
        return Err(ErasureError::Unsupported(format!(
            "per-window failure probability {p} escaped 0..=1 (repair window too long?)"
        )));
    }

    // Step 2: P(loss in one window) = P(≥ m+1 of n shards fail) — the full binomial
    // sum, not just the dominant j = m+1 term (docs/12 §7.1).
    let mut p_loss_in_window = 0.0f64;
    for j in (input.m + 1)..=n {
        let term = binomial(n, j) * p.powi(j as i32) * (1.0 - p).powi((n - j) as i32);
        p_loss_in_window += term;
    }

    // Steps 4–6. Compose the whole-year survival from per-window survival, then
    // read off the nines. Work in the "loss" tail via ln_1p / exp_m1 so the tiny
    // (1 − durability) never vanishes into f64's rounding at ~1e-16.
    let windows_per_year = HOURS_PER_YEAR / input.repair_window_hours;
    let ln_survive_window = (-p_loss_in_window).ln_1p(); // ln(1 − p_loss)
    let annual_loss = -(windows_per_year * ln_survive_window).exp_m1();
    let annual_durability = 1.0 - annual_loss;
    let nines = -annual_loss.log10();

    Ok(DurabilityReport {
        n,
        p_fail_in_window: p,
        p_loss_in_window,
        windows_per_year,
        annual_durability,
        nines,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backblaze_lands_near_eleven_nines() {
        let report = compute_durability(DurabilityInput::backblaze_17_3()).expect("calc");
        assert!(
            report.nines > 10.0 && report.nines < 12.0,
            "got {} nines (doc ~11)",
            report.nines
        );
    }

    #[test]
    fn lab_rs_4_2_lands_near_nine_nines() {
        let report = compute_durability(DurabilityInput::lab_rs_4_2()).expect("calc");
        assert!(
            report.nines > 8.0 && report.nines < 10.5,
            "got {} nines (doc ~9)",
            report.nines
        );
    }
}
