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
//!
//! Consumed by [`crate::session`] when handling `AMF0_COMMAND` message bodies.

use std::collections::BTreeMap;
use std::io::Read;

use crate::error::AppError;

/// AMF0 type markers for the value kinds a publish flow needs.
///
/// Each encoded value begins with one of these bytes; `OBJECT_END` is not a
/// standalone value — it terminates an [`Amf0::Object`] after an empty key.
pub mod marker {
    /// IEEE-754 `f64`, big-endian (8 bytes after the marker).
    pub const NUMBER: u8 = 0x00;
    /// One byte: zero is false, any non-zero is true.
    pub const BOOLEAN: u8 = 0x01;
    /// `u16` big-endian length, then that many UTF-8 bytes.
    pub const STRING: u8 = 0x02;
    /// Key/value pairs until an empty key followed by [`OBJECT_END`].
    pub const OBJECT: u8 = 0x03;
    /// Marker only — no payload bytes.
    pub const NULL: u8 = 0x05;
    /// Object terminator (`0x09`), written after a zero-length key — not a value type.
    pub const OBJECT_END: u8 = 0x09;
}

/// NetConnection `objectEncoding` values negotiated in `connect` / `_result`.
///
/// `0` = AMF0 only (message types 20/18). `3` would mean AMF3 is also allowed;
/// this ingest only speaks AMF0.
pub mod object_encoding {
    /// AMF0 — Flash 6+; the encoding we implement and advertise on connect.
    pub const AMF0: f64 = 0.0;
}

/// A decoded AMF0 value.
///
/// [`Object`](Amf0::Object) preserves keys but not insertion order (`BTreeMap` is
/// enough for the fields a publish flow reads: `app`, `code`, `level`, …).
#[derive(Debug, Clone, PartialEq)]
pub enum Amf0 {
    /// IEEE-754 double (transaction ids and numbers on the wire).
    Number(f64),
    /// Boolean flag.
    Boolean(bool),
    /// UTF-8 string (command names, stream keys, status codes, …).
    String(String),
    /// Named property bag; keys are ordered by [`BTreeMap`] for stable equality.
    Object(BTreeMap<String, Amf0>),
    /// Explicit null (common for unused command-object slots).
    Null,
}

impl Amf0 {
    /// Return the inner string when `self` is [`Amf0::String`].
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Amf0::String(s) => Some(s),
            _ => None,
        }
    }

    /// Return the inner number when `self` is [`Amf0::Number`].
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Amf0::Number(n) => Some(*n),
            _ => None,
        }
    }

    fn _length(amf: &Amf0) -> usize {
        match amf {
            Amf0::Number(_) => 8,
            Amf0::Boolean(_) => 1,
            Amf0::String(s) => 2 + s.len(),
            Amf0::Object(map) => map.iter().fold(0, |acc, (key, value)| {
                acc + 2 + key.len() + Self::length(value)
            }),
            Amf0::Null => 0,
        }
    }

    /// Byte length of this value's payload after its type marker.
    ///
    /// Used to pre-size the buffer in [`encode`]. Does not include the leading
    /// marker byte itself.
    pub fn length(&self) -> usize {
        Self::_length(self)
    }

    /// AMF0 type marker for this value ([`marker`] constants).
    pub(crate) const fn marker(&self) -> u8 {
        match self {
            Amf0::Number(_) => marker::NUMBER,
            Amf0::Boolean(_) => marker::BOOLEAN,
            Amf0::String(_) => marker::STRING,
            Amf0::Object(_) => marker::OBJECT,
            Amf0::Null => marker::NULL,
        }
    }
}

/// Decode the sequence of AMF0 values that make up one command message body (V2).
///
/// A command body is several concatenated values: the command name (`"connect"`),
/// a transaction id (number), a command object (or null), then any arguments.
/// Walks `buf` value by value until it is exhausted.
///
/// # Errors
///
/// Returns [`AppError::BadRequest`] for an unsupported marker, invalid UTF-8 in a
/// string/key, or a malformed object terminator; truncated payloads surface as
/// [`AppError::Other`] via I/O errors from the cursor.
///
/// # Examples
///
/// ```ignore
/// let mut buf = vec![marker::STRING];
/// buf.extend_from_slice(&(7u16).to_be_bytes());
/// buf.extend_from_slice(b"connect");
/// assert_eq!(decode(&buf).unwrap(), vec![Amf0::String("connect".into())]);
/// ```
pub fn decode(buf: &[u8]) -> Result<Vec<Amf0>, AppError> {
    let mut cursor = std::io::Cursor::new(buf);
    let mut amfs = Vec::new();
    while cursor.position() < buf.len() as u64 {
        amfs.push(decode_one(&mut cursor)?);
    }
    Ok(amfs)
}

/// A `u16`-length-prefixed UTF-8 run — the on-wire shape of both an AMF0 `string`
/// body (after its marker) and an object key (which carries no marker).
fn read_amf_string(cursor: &mut std::io::Cursor<&[u8]>) -> Result<String, AppError> {
    let mut length = [0u8; 2];
    cursor.read_exact(&mut length)?;
    let length = u16::from_be_bytes(length) as usize;

    let mut bytes = vec![0u8; length];
    cursor.read_exact(&mut bytes)?;
    String::from_utf8(bytes).map_err(|e| AppError::BadRequest(format!("invalid AMF0 string: {e}")))
}

/// Decode **exactly one** AMF0 value from `cursor`, leaving it positioned just past
/// that value.
///
/// This is the primitive both [`decode`] (a sequence of values) and object fields
/// (each field's value) are built on. Because it reads a single marker plus that
/// type's bytes and then *stops* — rather than running to the end of the buffer — a
/// value nested inside an object cannot swallow the sibling fields or the `00 00 09`
/// object terminator. All lengths are bounded by `read_exact`, so a truncated or
/// oversized value errors instead of over-reading.
fn decode_one(cursor: &mut std::io::Cursor<&[u8]>) -> Result<Amf0, AppError> {
    let mut marker = [0u8; 1];
    cursor.read_exact(&mut marker)?;
    match marker[0] {
        marker::NUMBER => {
            let mut number = [0u8; 8];
            cursor.read_exact(&mut number)?;
            Ok(Amf0::Number(f64::from_be_bytes(number)))
        }
        marker::BOOLEAN => {
            let mut boolean = [0u8; 1];
            cursor.read_exact(&mut boolean)?;
            Ok(Amf0::Boolean(boolean[0] != 0))
        }
        marker::STRING => Ok(Amf0::String(read_amf_string(cursor)?)),
        marker::NULL => Ok(Amf0::Null),
        marker::OBJECT => {
            let mut object = BTreeMap::new();
            loop {
                // Each field is `<u16 key-len><key><value>`. An empty key marks the
                // end of the fields, and the very next byte must be the object-end
                // marker (together they are the `00 00 09` terminator).
                let key = read_amf_string(cursor)?;
                if key.is_empty() {
                    let mut end = [0u8; 1];
                    cursor.read_exact(&mut end)?;
                    if end[0] != marker::OBJECT_END {
                        return Err(AppError::BadRequest(format!(
                            "invalid AMF0 object-end marker: {:#04x}",
                            end[0]
                        )));
                    }
                    break;
                }
                object.insert(key, decode_one(cursor)?);
            }
            Ok(Amf0::Object(object))
        }
        other => Err(AppError::BadRequest(format!(
            "unsupported or incomplete AMF0 marker: {other:#04x}"
        ))),
    }
}

/// Encode a sequence of AMF0 values into a command reply body (V2).
///
/// Inverse of [`decode`]: writes each value's marker then its payload (BE `f64`,
/// u16-prefixed strings, object key/value pairs + the `00 00 09` terminator). Used
/// to build `_result` / `onStatus` replies the session sends back.
///
/// # Examples
///
/// ```ignore
/// let values = vec![Amf0::String("status".into()), Amf0::Null];
/// assert_eq!(decode(&encode(&values)).unwrap(), values);
/// ```
pub fn encode(values: &[Amf0]) -> Vec<u8> {
    let expected_length = values.iter().fold(0, |acc, value| acc + value.length());
    let mut buf = Vec::with_capacity(expected_length);
    for value in values {
        buf.push(value.marker());
        match value {
            Amf0::Number(n) => {
                buf.extend_from_slice(&n.to_be_bytes());
            }
            Amf0::Boolean(b) => {
                buf.push(*b as u8);
            }
            Amf0::String(s) => {
                buf.extend_from_slice(&(s.len() as u16).to_be_bytes());
                buf.extend_from_slice(s.as_bytes());
            }
            Amf0::Object(map) => {
                for (key, value) in map {
                    buf.extend_from_slice(&(key.len() as u16).to_be_bytes());
                    buf.extend_from_slice(key.as_bytes());
                    buf.extend_from_slice(&encode(std::slice::from_ref(value)));
                }
                buf.extend_from_slice(&[0x00, 0x00, marker::OBJECT_END]);
            }
            Amf0::Null => {
                // We don't need to write anything for Null.
            }
        }
    }
    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    fn num_bytes(v: f64) -> Vec<u8> {
        let mut b = vec![marker::NUMBER];
        b.extend_from_slice(&v.to_be_bytes()); // IEEE-754 f64, big-endian
        b
    }

    fn bool_bytes(v: bool) -> Vec<u8> {
        vec![marker::BOOLEAN, v as u8]
    }

    fn str_bytes(s: &str) -> Vec<u8> {
        let mut b = vec![marker::STRING];
        b.extend_from_slice(&(s.len() as u16).to_be_bytes()); // u16 length prefix
        b.extend_from_slice(s.as_bytes());
        b
    }

    /// An object key on the wire: `<u16 len><utf8>` (no type marker — keys are raw).
    fn key_bytes(k: &str) -> Vec<u8> {
        let mut b = (k.len() as u16).to_be_bytes().to_vec();
        b.extend_from_slice(k.as_bytes());
        b
    }

    /// A single-field object `{ key: value }`: marker, the key/value pair, then the
    /// `00 00 09` terminator (empty key + object-end).
    fn obj_bytes(k: &str, value_bytes: &[u8]) -> Vec<u8> {
        let mut b = vec![marker::OBJECT];
        b.extend_from_slice(&key_bytes(k));
        b.extend_from_slice(value_bytes);
        b.extend_from_slice(&[0x00, 0x00, marker::OBJECT_END]);
        b
    }

    fn obj(pairs: &[(&str, Amf0)]) -> Amf0 {
        Amf0::Object(
            pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.clone()))
                .collect(),
        )
    }

    // --- decode ----------------------------------------------------------------------

    #[test]
    fn decode_number() {
        assert_eq!(decode(&num_bytes(3.5)).unwrap(), vec![Amf0::Number(3.5)]);
        // transaction ids are whole numbers carried as f64
        assert_eq!(decode(&num_bytes(7.0)).unwrap(), vec![Amf0::Number(7.0)]);
    }

    #[test]
    fn decode_boolean() {
        assert_eq!(
            decode(&bool_bytes(true)).unwrap(),
            vec![Amf0::Boolean(true)]
        );
        assert_eq!(
            decode(&bool_bytes(false)).unwrap(),
            vec![Amf0::Boolean(false)]
        );
        // any non-zero byte is true
        assert_eq!(
            decode(&[marker::BOOLEAN, 0x7f]).unwrap(),
            vec![Amf0::Boolean(true)]
        );
    }

    #[test]
    fn decode_string() {
        assert_eq!(
            decode(&str_bytes("connect")).unwrap(),
            vec![Amf0::String("connect".into())]
        );
        assert_eq!(
            decode(&str_bytes("")).unwrap(),
            vec![Amf0::String("".into())]
        );
    }

    #[test]
    fn decode_null() {
        assert_eq!(decode(&[marker::NULL]).unwrap(), vec![Amf0::Null]);
    }

    #[test]
    fn decode_empty_buffer_is_no_values() {
        assert_eq!(decode(&[]).unwrap(), vec![]);
    }

    #[test]
    fn decode_object_single_field() {
        let bytes = obj_bytes("app", &str_bytes("live"));
        assert_eq!(
            decode(&bytes).unwrap(),
            vec![obj(&[("app", Amf0::String("live".into()))])]
        );
    }

    #[test]
    fn decode_multiple_concatenated_values() {
        // A command body is several values back to back.
        let mut bytes = str_bytes("createStream");
        bytes.extend_from_slice(&num_bytes(4.0));
        bytes.extend_from_slice(&[marker::NULL]);
        assert_eq!(
            decode(&bytes).unwrap(),
            vec![
                Amf0::String("createStream".into()),
                Amf0::Number(4.0),
                Amf0::Null,
            ]
        );
    }

    #[test]
    fn decode_connect_command() {
        // The real opening command: connect(app) — name, transaction id, command object.
        let mut bytes = str_bytes("connect");
        bytes.extend_from_slice(&num_bytes(1.0));
        bytes.extend_from_slice(&obj_bytes("app", &str_bytes("live")));

        let values = decode(&bytes).unwrap();
        assert_eq!(values.len(), 3);
        assert_eq!(values[0].as_str(), Some("connect"));
        assert_eq!(values[1].as_f64(), Some(1.0));
        match &values[2] {
            Amf0::Object(map) => {
                assert_eq!(map.get("app").and_then(Amf0::as_str), Some("live"))
            }
            other => panic!("expected command object, got {other:?}"),
        }
    }

    // --- encode ----------------------------------------------------------------------

    #[test]
    fn encode_number() {
        assert_eq!(encode(&[Amf0::Number(3.5)]), num_bytes(3.5));
    }

    #[test]
    fn encode_boolean() {
        assert_eq!(encode(&[Amf0::Boolean(true)]), bool_bytes(true));
        assert_eq!(encode(&[Amf0::Boolean(false)]), bool_bytes(false));
    }

    #[test]
    fn encode_string() {
        assert_eq!(
            encode(&[Amf0::String("status".into())]),
            str_bytes("status")
        );
    }

    #[test]
    fn encode_null() {
        // Null is a bare marker with no payload — exactly one byte.
        assert_eq!(encode(&[Amf0::Null]), vec![marker::NULL]);
    }

    #[test]
    fn encode_object_single_field() {
        let value = obj(&[("app", Amf0::String("live".into()))]);
        assert_eq!(encode(&[value]), obj_bytes("app", &str_bytes("live")));
    }

    #[test]
    fn roundtrip_scalars() {
        let values = vec![
            Amf0::String("connect".into()),
            Amf0::Number(1.0),
            Amf0::Boolean(true),
            Amf0::Null,
        ];
        assert_eq!(decode(&encode(&values)).unwrap(), values);
    }

    #[test]
    fn roundtrip_publish_command() {
        let values = vec![
            Amf0::String("onStatus".into()),
            Amf0::Number(0.0),
            Amf0::Null,
            obj(&[
                ("level", Amf0::String("status".into())),
                ("code", Amf0::String("NetStream.Publish.Start".into())),
            ]),
        ];
        assert_eq!(decode(&encode(&values)).unwrap(), values);
    }

    #[test]
    fn decode_truncated_number_errors() {
        // marker says number (needs 8 bytes) but only 3 follow.
        let err = decode(&[marker::NUMBER, 0x40, 0x00, 0x00]).unwrap_err();
        assert!(
            matches!(err, AppError::Other(_) | AppError::BadRequest(_)),
            "got {err:?}"
        );
    }

    #[test]
    fn decode_string_length_beyond_buffer_errors() {
        // Length says 10 bytes but only 3 are present.
        let bytes = [marker::STRING, 0x00, 0x0A, b'a', b'b', b'c'];
        let err = decode(&bytes).unwrap_err();
        assert!(
            matches!(err, AppError::Other(_) | AppError::BadRequest(_)),
            "got {err:?}"
        );
    }

    #[test]
    fn decode_unknown_marker_errors() {
        let err = decode(&[0xEE]).unwrap_err();
        assert!(matches!(err, AppError::BadRequest(_)), "got {err:?}");
    }

    /// A generator for arbitrary AMF0 values, including nested objects, so the
    /// round-trip property is exercised across the whole type set — not just the few
    /// shapes the example tests hand-pick.
    fn arb_amf0() -> impl proptest::strategy::Strategy<Value = Amf0> {
        use proptest::prelude::*;
        let leaf = prop_oneof![
            // NaN is excluded: NaN != NaN, so it would fail equality for a reason that
            // has nothing to do with the codec.
            any::<f64>()
                .prop_filter("NaN never compares equal", |f| !f.is_nan())
                .prop_map(Amf0::Number),
            any::<bool>().prop_map(Amf0::Boolean),
            ".{0,32}".prop_map(Amf0::String),
            Just(Amf0::Null),
        ];
        // Objects can nest: depth ≤ 3, ≤ 4 fields each, lowercase keys.
        leaf.prop_recursive(3, 32, 4, |inner| {
            proptest::collection::btree_map("[a-z]{1,8}", inner, 0..4).prop_map(Amf0::Object)
        })
    }

    proptest::proptest! {
        /// No random byte string may panic the decoder — it returns Ok or Err only.
        #[test]
        fn decode_never_panics(bytes in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..256)) {
            let _ = decode(&bytes);
        }

        /// The codec's defining property: encoding a value then decoding it yields the
        /// same value back, for *any* AMF0 value. This is what actually pins the wire
        /// format down — a subtly wrong marker or terminator fails here even if the
        /// hand-written example tests happen to miss it.
        #[test]
        fn encode_then_decode_is_identity(value in arb_amf0()) {
            let bytes = encode(std::slice::from_ref(&value));
            let decoded = decode(&bytes).expect("our own encoding must re-decode");
            proptest::prop_assert_eq!(decoded, vec![value]);
        }

        /// A whole command body (several concatenated values) also round-trips.
        #[test]
        fn encode_then_decode_sequence_is_identity(values in proptest::collection::vec(arb_amf0(), 0..6)) {
            let bytes = encode(&values);
            let decoded = decode(&bytes).expect("our own encoding must re-decode");
            proptest::prop_assert_eq!(decoded, values);
        }
    }
}
