//! Wire helpers — **fully wired**, not a vertical.
//!
//! Two small things every plane needs before the interesting work starts:
//!
//! 1. [`classify`] — the RFC 7983 first-byte demultiplex. One UDP port carries STUN *and*
//!    RTP *and* RTCP (WebRTC muxes them all), so the very first thing the pump does with a
//!    datagram is decide which it is, by its leading byte(s). This is mechanical, so it's
//!    given to you.
//! 2. Zero-copy RTP header **field accessors** ([`RtpView`]). An SFU forwards RTP *without
//!    decoding it* — it reads a handful of header fields (sequence, timestamp, SSRC, marker,
//!    payload type) and, per subscriber, rewrites some of them **in place**. Parsing the RTP
//!    header from scratch was project 14's V1; here it's plumbing, so these read/patch the
//!    fixed 12-byte header by offset. The learning in *this* project is what the rewriter
//!    (V2) *does* with these, not re-deriving the byte layout.

/// What kind of datagram arrived on the muxed UDP port (RFC 7983 demux).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PacketKind {
    /// A STUN message (ICE connectivity check / response) — first byte in `0x00..=0x03`.
    Stun,
    /// An RTP media packet — first byte in `0x80..=0xBF`, payload-type byte *not* 64..=95.
    Rtp,
    /// An RTCP control packet — first byte in `0x80..=0xBF`, second byte's PT in `192..=223`.
    Rtcp,
    /// Anything else (DTLS, ZRTP, TURN channel, or garbage) — dropped by this SFU.
    Unknown,
}

/// The fixed RTP header size, before any CSRC entries or extensions.
pub const RTP_MIN_HEADER: usize = 12;
/// The RTP version this SFU speaks.
pub const RTP_VERSION: u8 = 2;

/// Classify a datagram by its leading bytes (RFC 7983). Cheap, allocation-free, total.
pub fn classify(buf: &[u8]) -> PacketKind {
    match buf.first() {
        Some(&b) if b <= 3 => PacketKind::Stun,
        Some(&b) if (128..=191).contains(&b) => {
            // RTP vs RTCP share the 0x80 band; the second byte disambiguates: an RTCP
            // packet-type sits in 192..=223, everything else in that band is RTP.
            match buf.get(1) {
                Some(&pt) if (192..=223).contains(&pt) => PacketKind::Rtcp,
                Some(_) => PacketKind::Rtp,
                None => PacketKind::Unknown,
            }
        }
        _ => PacketKind::Unknown,
    }
}

/// A read/patch view over one RTP datagram's fixed header. Borrows the buffer; the
/// accessors are pure offset reads and the mutators patch big-endian fields in place.
///
/// This is the SFU's forwarding lens: it never copies the payload, it just reads the fields
/// the routing/rewriting logic (V2) reasons about and stamps new ones on the way out.
pub struct RtpView<'a> {
    buf: &'a mut [u8],
}

impl<'a> RtpView<'a> {
    /// Wrap a datagram, verifying only that it's long enough to hold a fixed header and is
    /// version 2 — the SFU forwards bytes it doesn't fully parse, but it won't index past
    /// the end of a runt datagram.
    pub fn new(buf: &'a mut [u8]) -> Option<Self> {
        if buf.len() < RTP_MIN_HEADER || (buf[0] >> 6) != RTP_VERSION {
            return None;
        }
        Some(Self { buf })
    }

    /// Marker bit (set on the last packet of a frame — a safe simulcast switch boundary).
    pub fn marker(&self) -> bool {
        self.buf[1] & 0x80 != 0
    }

    /// 7-bit payload type.
    pub fn payload_type(&self) -> u8 {
        self.buf[1] & 0x7f
    }

    /// Per-packet sequence number.
    pub fn sequence(&self) -> u16 {
        u16::from_be_bytes([self.buf[2], self.buf[3]])
    }

    /// Media timestamp (clock-rate ticks).
    pub fn timestamp(&self) -> u32 {
        u32::from_be_bytes([self.buf[4], self.buf[5], self.buf[6], self.buf[7]])
    }

    /// Synchronization source id (identifies the origin stream / simulcast layer).
    pub fn ssrc(&self) -> u32 {
        u32::from_be_bytes([self.buf[8], self.buf[9], self.buf[10], self.buf[11]])
    }

    /// Overwrite the sequence number (the per-subscriber rewriter keeps this contiguous).
    pub fn set_sequence(&mut self, seq: u16) {
        self.buf[2..4].copy_from_slice(&seq.to_be_bytes());
    }

    /// Overwrite the media timestamp (rebased per subscriber on a layer switch).
    pub fn set_timestamp(&mut self, ts: u32) {
        self.buf[4..8].copy_from_slice(&ts.to_be_bytes());
    }

    /// Overwrite the SSRC (the SFU presents one stable SSRC per subscriber, hiding layer
    /// switches behind it).
    pub fn set_ssrc(&mut self, ssrc: u32) {
        self.buf[8..12].copy_from_slice(&ssrc.to_be_bytes());
    }
}
