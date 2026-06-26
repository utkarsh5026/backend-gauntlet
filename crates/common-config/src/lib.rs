//! Tiny configuration helpers shared across projects.
//!
//! Philosophy: config comes from the environment (12-factor). In local dev we
//! load a `.env` file if present; in production the real environment wins.
//! Secrets never live in code or in version control — see each project's
//! `.env.example` for the shape, and copy it to `.env` (which is gitignored).

use thiserror::Error;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("required environment variable `{0}` is not set")]
    Missing(&'static str),
    #[error("environment variable `{0}` is set but could not be parsed: {1}")]
    Parse(&'static str, String),
}

/// Load a `.env` file into the process environment if one exists.
///
/// Safe to call when there is no `.env` file (returns quietly). Call once at the
/// very start of `main`, before reading any config.
pub fn load_dotenv() {
    match dotenvy::dotenv() {
        Ok(path) => tracing::debug!(?path, "loaded .env file"),
        Err(e) if e.not_found() => tracing::debug!("no .env file found, using process env"),
        Err(e) => tracing::warn!(error = %e, "failed to load .env file"),
    }
}

/// Fetch a required environment variable, erroring if it's missing.
pub fn require(key: &'static str) -> Result<String, ConfigError> {
    std::env::var(key).map_err(|_| ConfigError::Missing(key))
}

/// Fetch an environment variable or fall back to a default.
pub fn or_default(key: &str, default: impl Into<String>) -> String {
    std::env::var(key).unwrap_or_else(|_| default.into())
}

/// Fetch and parse a typed environment variable, falling back to a default.
///
/// ```no_run
/// let port: u16 = common_config::parse_or("PORT", 8080);
/// ```
pub fn parse_or<T>(key: &'static str, default: T) -> T
where
    T: std::str::FromStr,
{
    match std::env::var(key) {
        Ok(v) => v.parse().unwrap_or(default),
        Err(_) => default,
    }
}

/// Fetch and parse a required typed environment variable.
pub fn require_parsed<T>(key: &'static str) -> Result<T, ConfigError>
where
    T: std::str::FromStr,
    T::Err: std::fmt::Display,
{
    let raw = require(key)?;
    raw.parse()
        .map_err(|e: T::Err| ConfigError::Parse(key, e.to_string()))
}
