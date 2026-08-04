//! Duration parsing for environment-driven config.
//!
//! Timeouts, TTLs and tick intervals are the most common typed config in these
//! projects, and they were all being spelled out by hand:
//!
//! ```ignore
//! let t = Duration::from_millis(common_config::parse_or("REQUEST_TIMEOUT_MS", 10_000));
//! ```
//!
//! [`duration_or`] replaces that: you name the variable, name the unit once,
//! and give the default as a plain number in that unit.
//!
//! ```ignore
//! let t = common_config::duration_or("REQUEST_TIMEOUT_MS", TimeUnit::Millis, 10_000);
//! ```
//!
//! The operator can still override the unit in the value itself — `2s`, `1.5s`,
//! `30m` — which is what makes a `..._MS` variable readable when someone means
//! ten seconds.

use std::time::Duration;

use thiserror::Error;

/// The unit a duration is measured in.
///
/// Used both for the `default` you pass to [`duration_or`] and as the meaning
/// of a suffix an operator writes in a value (the `ms` in `250ms`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum TimeUnit {
    Nanos,
    Micros,
    Millis,
    Secs,
    Mins,
    Hours,
    Days,
}

impl TimeUnit {
    /// How many nanoseconds one of this unit is worth.
    ///
    /// `u128` because a day is ~8.6e13 ns and we multiply by the count before
    /// range-checking the result.
    pub const fn nanos(self) -> u128 {
        const NS: u128 = 1;
        const US: u128 = 1_000 * NS;
        const MS: u128 = 1_000 * US;
        const S: u128 = 1_000 * MS;
        match self {
            TimeUnit::Nanos => NS,
            TimeUnit::Micros => US,
            TimeUnit::Millis => MS,
            TimeUnit::Secs => S,
            TimeUnit::Mins => 60 * S,
            TimeUnit::Hours => 3_600 * S,
            TimeUnit::Days => 86_400 * S,
        }
    }

    /// Scale a count of this unit into a [`Duration`], or `None` if the count is
    /// negative, NaN, or too large to represent.
    ///
    /// Whole counts go through exact integer math; only fractional counts
    /// (`1.5s`, `0.3s`) touch floating point.
    pub fn scale(self, count: f64) -> Option<Duration> {
        if !count.is_finite() || count < 0.0 {
            return None;
        }
        let nanos = (count as u128).checked_mul(self.nanos())?;
        let frac = count.fract();
        let nanos = if frac == 0.0 {
            nanos
        } else {
            nanos.checked_add((frac * self.nanos() as f64).round() as u128)?
        };
        let secs = u64::try_from(nanos / 1_000_000_000).ok()?;
        Some(Duration::new(secs, (nanos % 1_000_000_000) as u32))
    }

    /// Parse the unit suffix of a value — the `ms` in `250ms`.
    ///
    /// Case-insensitive, and accepts the long spellings an operator is likely
    /// to reach for (`250millis`, `30 seconds`).
    pub fn from_suffix(suffix: &str) -> Option<Self> {
        match suffix.trim().to_ascii_lowercase().as_str() {
            "ns" | "nano" | "nanos" | "nanosecond" | "nanoseconds" => Some(TimeUnit::Nanos),
            "us" | "µs" | "μs" | "micro" | "micros" | "microsecond" | "microseconds" => {
                Some(TimeUnit::Micros)
            }
            "ms" | "milli" | "millis" | "millisecond" | "milliseconds" => Some(TimeUnit::Millis),
            "s" | "sec" | "secs" | "second" | "seconds" => Some(TimeUnit::Secs),
            "m" | "min" | "mins" | "minute" | "minutes" => Some(TimeUnit::Mins),
            "h" | "hr" | "hrs" | "hour" | "hours" => Some(TimeUnit::Hours),
            "d" | "day" | "days" => Some(TimeUnit::Days),
            _ => None,
        }
    }
}

/// Any number that can stand in as a count of [`TimeUnit`]s.
///
/// Exists so the `default` argument to [`duration_or`] can be written as a bare
/// literal or an existing `u64`/`f64` constant without an `as` cast at the call
/// site — `10_000` and `0.3` both just work.
pub trait Count: Copy {
    fn as_f64(self) -> f64;
}

macro_rules! impl_count {
    ($($t:ty),* $(,)?) => {
        $(impl Count for $t {
            fn as_f64(self) -> f64 {
                self as f64
            }
        })*
    };
}

impl_count!(u8, u16, u32, u64, usize, i8, i16, i32, i64, isize, f32, f64);

/// Why a duration string could not be turned into a [`Duration`].
#[derive(Debug, Error, PartialEq, Eq)]
pub enum DurationParseError {
    #[error("duration is empty")]
    Empty,
    #[error("`{0}` is not a number")]
    NotANumber(String),
    #[error("`{0}` is not a known time unit (try ns, us, ms, s, m, h, d)")]
    UnknownUnit(String),
    #[error("duration `{0}` is negative or too large to represent")]
    OutOfRange(String),
}

/// Read a duration from the environment.
///
/// `unit` is how a bare number is read — both the operator's (`SWEEP_MS=750`)
/// and your `default`. The operator may override it per-value with a suffix, so
/// `REQUEST_TIMEOUT_MS=2s` really is two seconds.
///
/// If the variable is unset, unparseable, or names a unit that doesn't exist,
/// the default is used and a warning is logged — a mistyped timeout quietly
/// reverting to a default is exactly what you want to see in the boot logs.
///
/// ```no_run
/// use common_config::{duration_or, TimeUnit};
///
/// let timeout = duration_or("REQUEST_TIMEOUT_MS", TimeUnit::Millis, 10_000);
/// let ttl = duration_or("PRESENCE_TTL_SECS", TimeUnit::Secs, 30);
/// let part = duration_or("TARGET_PART_SECS", TimeUnit::Secs, 0.3);
/// ```
///
/// # Panics
///
/// If `default` is negative or too large to be a [`Duration`] — that's a bug in
/// the call site, not operator input, so it fails loudly at startup.
pub fn duration_or(key: &'static str, unit: TimeUnit, default: impl Count) -> Duration {
    let fallback = unit
        .scale(default.as_f64())
        .unwrap_or_else(|| panic!("invalid default duration for `{key}`: {}", default.as_f64()));

    let Ok(raw) = std::env::var(key) else {
        return fallback;
    };
    match parse(&raw, unit) {
        Ok(d) => d,
        Err(e) => {
            tracing::warn!(
                key,
                value = %raw,
                error = %e,
                default = ?fallback,
                "invalid duration in environment, using default"
            );
            fallback
        }
    }
}

/// Parse a duration string, reading a bare number as `unit`.
fn parse(raw: &str, unit: TimeUnit) -> Result<Duration, DurationParseError> {
    let s = raw.trim();
    if s.is_empty() {
        return Err(DurationParseError::Empty);
    }

    // Split at the first character that can't be part of the number. `_` is
    // allowed so `10_000ms` mirrors how the same literal is written in Rust.
    let split = s
        .find(|c: char| !(c.is_ascii_digit() || matches!(c, '.' | '_' | '+' | '-')))
        .unwrap_or(s.len());
    let (number, suffix) = s.split_at(split);
    let suffix = suffix.trim();
    if number.is_empty() {
        // Nothing numeric at all ("soon"): report it as a bad number rather
        // than blaming the unit, which is what the operator actually got wrong.
        return Err(DurationParseError::NotANumber(s.to_string()));
    }

    let unit = if suffix.is_empty() {
        unit
    } else {
        TimeUnit::from_suffix(suffix)
            .ok_or_else(|| DurationParseError::UnknownUnit(suffix.to_string()))?
    };

    let count: f64 = number
        .replace('_', "")
        .parse()
        .map_err(|_| DurationParseError::NotANumber(number.trim().to_string()))?;

    unit.scale(count)
        .ok_or_else(|| DurationParseError::OutOfRange(s.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bare_numbers_take_the_given_unit() {
        assert_eq!(
            parse("250", TimeUnit::Millis),
            Ok(Duration::from_millis(250))
        );
        assert_eq!(parse("30", TimeUnit::Secs), Ok(Duration::from_secs(30)));
        assert_eq!(parse("2", TimeUnit::Days), Ok(Duration::from_secs(172_800)));
    }

    #[test]
    fn a_suffix_in_the_value_overrides_the_unit() {
        assert_eq!(parse("2s", TimeUnit::Millis), Ok(Duration::from_secs(2)));
        assert_eq!(
            parse("500ms", TimeUnit::Secs),
            Ok(Duration::from_millis(500))
        );
        assert_eq!(
            parse("5 minutes", TimeUnit::Secs),
            Ok(Duration::from_secs(300))
        );
        assert_eq!(parse("1H", TimeUnit::Secs), Ok(Duration::from_secs(3_600)));
    }

    #[test]
    fn fractions_and_separators_parse() {
        assert_eq!(
            parse("1.5s", TimeUnit::Secs),
            Ok(Duration::from_millis(1_500))
        );
        // TARGET_PART_SECS=0.3 in 13-live-ingest.
        assert_eq!(parse("0.3", TimeUnit::Secs), Ok(Duration::from_millis(300)));
        assert_eq!(
            parse("10_000", TimeUnit::Millis),
            Ok(Duration::from_secs(10))
        );
        assert_eq!(
            parse("  100ms  ", TimeUnit::Secs),
            Ok(Duration::from_millis(100))
        );
    }

    #[test]
    fn bad_input_is_rejected() {
        assert_eq!(parse("", TimeUnit::Secs), Err(DurationParseError::Empty));
        assert!(matches!(
            parse("soon", TimeUnit::Secs),
            Err(DurationParseError::NotANumber(_))
        ));
        assert!(matches!(
            parse("10 fortnights", TimeUnit::Secs),
            Err(DurationParseError::UnknownUnit(_))
        ));
        assert!(matches!(
            parse("-5s", TimeUnit::Secs),
            Err(DurationParseError::OutOfRange(_))
        ));
        assert!(matches!(
            parse("999999999999999999999999d", TimeUnit::Secs),
            Err(DurationParseError::OutOfRange(_))
        ));
    }

    #[test]
    fn defaults_accept_any_numeric_type() {
        // Unset keys, so each of these exercises only the default path.
        assert_eq!(
            duration_or("TEST_DUR_TYPE_U64", TimeUnit::Millis, 10_000_u64),
            Duration::from_secs(10)
        );
        assert_eq!(
            duration_or("TEST_DUR_TYPE_LITERAL", TimeUnit::Secs, 30),
            Duration::from_secs(30)
        );
        assert_eq!(
            duration_or("TEST_DUR_TYPE_FLOAT", TimeUnit::Secs, 0.3),
            Duration::from_millis(300)
        );
    }

    #[test]
    fn env_values_are_read_and_defaulted() {
        // Unique keys per test so parallel test threads don't collide.
        std::env::set_var("TEST_DUR_SWEEP_MS", "750");
        assert_eq!(
            duration_or("TEST_DUR_SWEEP_MS", TimeUnit::Millis, 1_000),
            Duration::from_millis(750)
        );

        // The operator can override the unit in the value.
        std::env::set_var("TEST_DUR_TIMEOUT_MS", "2s");
        assert_eq!(
            duration_or("TEST_DUR_TIMEOUT_MS", TimeUnit::Millis, 500),
            Duration::from_secs(2)
        );

        // Set but garbage -> default (and a warning).
        std::env::set_var("TEST_DUR_BAD_MS", "later");
        assert_eq!(
            duration_or("TEST_DUR_BAD_MS", TimeUnit::Millis, 3_000),
            Duration::from_secs(3)
        );
    }

    #[test]
    #[should_panic(expected = "invalid default duration")]
    fn a_negative_default_is_a_call_site_bug() {
        duration_or("TEST_DUR_NEGATIVE_DEFAULT", TimeUnit::Secs, -1);
    }
}
