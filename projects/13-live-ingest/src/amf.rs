//! V2 (part 1) — AMF0: RTMP's command serialization.
//!
//! RTMP carries its control RPC (`connect`, `createStream`, `publish`, and the
//! `_result`/`onStatus` replies) as **AMF0**-encoded values inside command messages.
//! AMF0 is a compact typed format: a 1-byte **type marker** then the value —
//! `number` (IEEE-754 f64, big-endian), `boolean` (1 byte), `string` (u16 length +
//! UTF-8), `object` (a run of `<u16-len key><value>` pairs ended by the empty key +
//! the `object-end` marker `0x09`), and `null`. Those are the types a publish flow
//! uses; this module decodes and encodes them.
//!
//! Pure functions over `&[u8]` / `Vec<u8>` — no I/O — so the parser is exhaustively
//! property-testable, which is what V2's Proof asks for.

use std::collections::BTreeMap;

use crate::error::AppError;

/// AMF0 type markers (the ones a publish flow needs).
pub mod marker {
    pub const NUMBER: u8 = 0x00;
    pub const BOOLEAN: u8 = 0x01;
    pub const STRING: u8 = 0x02;
    pub const OBJECT: u8 = 0x03;
    pub const NULL: u8 = 0x05;
    pub const OBJECT_END: u8 = 0x09;
}

/// A decoded AMF0 value. `Object` preserves keys but not order (a `BTreeMap` is enough
/// for the fields a publish flow reads: `app`, `code`, `level`, …).
#[derive(Debug, Clone, PartialEq)]
pub enum Amf0 {
    Number(f64),
    Boolean(bool),
    String(String),
    Object(BTreeMap<String, Amf0>),
    Null,
}

impl Amf0 {
    /// Convenience: read this value as a string, if it is one.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Amf0::String(s) => Some(s),
            _ => None,
        }
    }

    /// Convenience: read this value as a number, if it is one.
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Amf0::Number(n) => Some(*n),
            _ => None,
        }
    }
}

/// Decode the sequence of AMF0 values that make up one command message body (V2).
///
/// A command body is several concatenated values: the command name (`"connect"`),
/// a transaction id (number), a command object (or null), then any arguments.
///
/// TODO(V2): walk `buf` value by value. For each: read the 1-byte marker, then decode
/// per type (BE f64 for number; 1 byte for boolean; u16-length + bytes for string; for
/// object, loop reading `<u16 key><value>` until the empty key + `OBJECT_END`). Stop at
/// the end of `buf`. **Bound every length against the remaining bytes before slicing**
/// — a malicious length must error, never panic or over-allocate.
pub fn decode(buf: &[u8]) -> Result<Vec<Amf0>, AppError> {
    let _ = buf;
    todo!("V2: decode a run of AMF0 values (number/boolean/string/object/null)")
}

/// Encode a sequence of AMF0 values into a command reply body (V2).
///
/// TODO(V2): the inverse of `decode` — write each value's marker then its bytes
/// (BE f64, u16-prefixed strings, object key/value pairs + the `00 00 09` terminator).
/// This builds the `_result` / `onStatus` replies the session sends back.
pub fn encode(values: &[Amf0]) -> Vec<u8> {
    let _ = values;
    todo!("V2: encode AMF0 values for a command reply (_result / onStatus)")
}

#[cfg(test)]
mod tests {
    // TODO(V2): prove the codec:
    //   - `decode`∘`encode` round-trips number/boolean/string/object/null
    //     (`amf0_roundtrips_publish_command`);
    //   - decoding a captured real `connect` body yields command name "connect",
    //     a transaction id, and an object carrying `app`;
    //   - a truncated value or an oversized length errors, never panics
    //     (property/fuzz test over random bytes).
}
