//! V1 — Bencode, the wire's data format. Build the codec from scratch.
//!
//! Everything in BitTorrent is bencoded: the `.torrent` file, the tracker's reply, the
//! DHT. Four types, all length- or delimiter-framed:
//!   - integer:    `i42e`  (also `i-1e`; `i-0e` and leading zeros like `i03e` are illegal)
//!   - byte-string:`4:spam` (a length, a colon, then exactly that many raw bytes)
//!   - list:       `l<values>e`
//!   - dict:       `d<key><value>…e`  (keys are byte-strings, sorted as raw bytes)
//!
//! The subtle constraint that makes this a *challenge*, not a `serde` call: to compute
//! the infohash (V2) you SHA-1 the **exact original bytes** of the `info` dictionary.
//! So your decoder must let you recover a value's precise byte span, and your encoder
//! must be **canonical** (sorted keys, no leading zeros, no whitespace) — otherwise a
//! decode→encode round-trip changes the bytes and the infohash is wrong. This is the
//! whole reason people hit bugs reimplementing BitTorrent.

use std::collections::BTreeMap;

/// A decoded bencode value. `Bytes`/`Dict` keys are raw bytes, *not* `String`: piece
/// hashes and some paths are not valid UTF-8, and treating them as text corrupts them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    Int(i64),
    Bytes(Vec<u8>),
    List(Vec<Value>),
    /// `BTreeMap` keeps keys sorted — which is also the canonical encode order.
    Dict(BTreeMap<Vec<u8>, Value>),
}

#[derive(Debug, thiserror::Error)]
pub enum BencodeError {
    #[error("unexpected end of input")]
    Truncated,
    #[error("trailing bytes after a complete value")]
    TrailingBytes,
    #[error("invalid integer encoding")]
    InvalidInt,
    #[error("invalid byte-string length")]
    InvalidLength,
    #[error("unexpected byte 0x{0:02x}")]
    Unexpected(u8),
    #[error("dict keys out of order or duplicated")]
    UnorderedKeys,
}

impl Value {
    /// Convenience: borrow this value as an integer, if it is one.
    pub fn as_int(&self) -> Option<i64> {
        match self {
            Value::Int(i) => Some(*i),
            _ => None,
        }
    }

    /// Convenience: borrow this value as a byte-string, if it is one.
    pub fn as_bytes(&self) -> Option<&[u8]> {
        match self {
            Value::Bytes(b) => Some(b),
            _ => None,
        }
    }
}

/// Decode one bencode value, requiring it to consume the **entire** input.
///
/// TODO(V1): parse a single top-level value and error with [`BencodeError::TrailingBytes`]
/// if anything is left over. Reject — never panic on — malformed input: truncation,
/// leading zeros (`i03e`), negative zero (`i-0e`), a length that overruns the buffer.
pub fn decode(input: &[u8]) -> Result<Value, BencodeError> {
    let _ = input;
    todo!("V1: decode a bencode value; reject malformed input instead of panicking")
}

/// Encode a value **canonically**: dict keys sorted as raw byte strings, integers with
/// no leading zeros, no extra whitespace. `encode(decode(x)) == x` for every valid `x`.
///
/// TODO(V1): serialize `value`. The canonical rules are the whole point — a
/// non-canonical encoder silently breaks the infohash in V2.
pub fn encode(value: &Value) -> Vec<u8> {
    let _ = value;
    todo!("V1: canonical bencode encoding (sorted keys, no leading zeros)")
}

/// Decode the top-level value **and** return, for each key of a top-level dict, the
/// exact byte span its *value* occupied in `input`. V2 uses this to SHA-1 the original
/// `info` bytes without re-encoding (which could differ if a producer wasn't canonical).
///
/// TODO(V1): return the parsed dict plus a map key → (start, end) byte range. Consider
/// how you'd expose the same for a nested value — the `info` dict is nested one level.
pub fn decode_dict_with_spans(
    input: &[u8],
) -> Result<(BTreeMap<Vec<u8>, Value>, BTreeMap<Vec<u8>, (usize, usize)>), BencodeError> {
    let _ = input;
    todo!("V1: parse a top-level dict and record each value's raw byte span (for the infohash)")
}

#[cfg(test)]
mod tests {
    // TODO(V1): prove the codec.
    //   - each type round-trips: decode then encode returns the original bytes;
    //   - a property test `prop_bencode_roundtrips`: for random Values, encode→decode
    //     is the identity, and decode→encode is byte-stable (canonical);
    //   - malformed inputs (`i03e`, `i-0e`, `3:ab` [short], `d1:a1:b1:a1:ce` [dup key],
    //     trailing junk) each return Err, never panic;
    //   - a real checked-in `.torrent` decodes, and re-encoding the recovered `info`
    //     span reproduces its exact bytes.
}
