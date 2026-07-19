//! Shared record types — the vocabulary every layer speaks.
//!
//! Plumbing, not a vertical: these are the plain data the log stores and the API
//! returns. `Bytes` is used for key/value so a record can move from the produce
//! path into the append path and back out to a fetch response without copying
//! the payload each hop.

use bytes::Bytes;

/// A logical position within a single partition's log. Offsets are per-partition
/// (V3), assigned monotonically by the log (V1), and start at 0.
pub type Offset = u64;

/// A record as handed to the broker to append. The offset is *not* here — the
/// log assigns it on append and returns it.
#[derive(Debug, Clone)]
pub struct Record {
    /// Optional partition key. Same key → same partition (V3), and it's stored in
    /// the frame so a fetch can return it.
    pub key: Option<Bytes>,
    /// The payload.
    pub value: Bytes,
    /// Producer timestamp (epoch millis). Stamped at produce time if the client
    /// didn't supply one.
    pub timestamp: i64,
}

/// A record read back from the log: the stored `Record` plus the offset the log
/// assigned it.
#[derive(Debug, Clone)]
pub struct StoredRecord {
    pub offset: Offset,
    pub timestamp: i64,
    pub key: Option<Bytes>,
    pub value: Bytes,
}
