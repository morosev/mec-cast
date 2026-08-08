//! The timing envelope: the wire-level contract shared by every mec-cast
//! transport profile.
//!
//! The envelope is a fixed 64-byte, little-endian structure. The same bytes
//! ride as a ROS message field today, and as a Zenoh attachment or RTP header
//! extension later. Timestamps are nanoseconds on the PTP-disciplined
//! `CLOCK_REALTIME` domain; a field that has not been stamped yet is `0`.
//!
//! Wire layout (64 bytes, little-endian):
//!
//! ```text
//! offset  size  field
//!      0     1  version         (currently 1)
//!      1     1  modality        (0=PointCloud 1=Video 2=Audio 3=Generic)
//!      2     6  reserved        (must be zero on encode, ignored on decode)
//!      8     8  capture_ns      i64
//!     16     8  send_ns         i64
//!     24     8  recv_ns         i64
//!     32     8  process_done_ns i64
//!     40     8  seq             u64
//!     48    16  trace_id        (UUID bytes, run identifier)
//! ```

use std::fmt;

/// Size in bytes of the serialized envelope.
pub const ENVELOPE_WIRE_LEN: usize = 64;

/// Current wire-format version, stored in byte 0.
pub const ENVELOPE_VERSION: u8 = 1;

/// What kind of payload the envelope describes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Modality {
    PointCloud = 0,
    Video = 1,
    Audio = 2,
    Generic = 3,
}

impl Modality {
    pub fn as_str(&self) -> &'static str {
        match self {
            Modality::PointCloud => "pointcloud",
            Modality::Video => "video",
            Modality::Audio => "audio",
            Modality::Generic => "generic",
        }
    }
}

impl TryFrom<u8> for Modality {
    type Error = EnvelopeError;

    fn try_from(v: u8) -> Result<Self, EnvelopeError> {
        match v {
            0 => Ok(Modality::PointCloud),
            1 => Ok(Modality::Video),
            2 => Ok(Modality::Audio),
            3 => Ok(Modality::Generic),
            other => Err(EnvelopeError::UnknownModality(other)),
        }
    }
}

/// Per-unit timing envelope. One instance accompanies each frame / point
/// cloud / sample through the pipeline; each stage stamps its field.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TimingEnvelope {
    /// Sensor delivered the data (camera grab, lidar sweep complete).
    pub capture_ns: i64,
    /// Last stamp before the payload left the sender's process.
    pub send_ns: i64,
    /// First stamp after the payload arrived at the receiver's process.
    pub recv_ns: i64,
    /// Receiver-side processing finished.
    pub process_done_ns: i64,
    /// Monotonically increasing per-publisher sequence number.
    pub seq: u64,
    /// Payload kind.
    pub modality: Modality,
    /// Experiment run id (UUID bytes); joins samples across processes.
    pub trace_id: [u8; 16],
}

impl TimingEnvelope {
    /// New envelope with all timestamps unset (0).
    pub fn new(modality: Modality, seq: u64, trace_id: [u8; 16]) -> Self {
        Self {
            capture_ns: 0,
            send_ns: 0,
            recv_ns: 0,
            process_done_ns: 0,
            seq,
            modality,
            trace_id,
        }
    }

    /// Serialize to the fixed 64-byte wire format.
    pub fn to_bytes(&self) -> [u8; ENVELOPE_WIRE_LEN] {
        let mut b = [0u8; ENVELOPE_WIRE_LEN];
        b[0] = ENVELOPE_VERSION;
        b[1] = self.modality as u8;
        // bytes 2..8 reserved, already zero
        b[8..16].copy_from_slice(&self.capture_ns.to_le_bytes());
        b[16..24].copy_from_slice(&self.send_ns.to_le_bytes());
        b[24..32].copy_from_slice(&self.recv_ns.to_le_bytes());
        b[32..40].copy_from_slice(&self.process_done_ns.to_le_bytes());
        b[40..48].copy_from_slice(&self.seq.to_le_bytes());
        b[48..64].copy_from_slice(&self.trace_id);
        b
    }

    /// Deserialize from the wire format. Rejects wrong lengths, unknown
    /// versions, and unknown modality values.
    pub fn from_bytes(b: &[u8]) -> Result<Self, EnvelopeError> {
        if b.len() != ENVELOPE_WIRE_LEN {
            return Err(EnvelopeError::WrongLength {
                expected: ENVELOPE_WIRE_LEN,
                got: b.len(),
            });
        }
        if b[0] != ENVELOPE_VERSION {
            return Err(EnvelopeError::UnsupportedVersion(b[0]));
        }
        let modality = Modality::try_from(b[1])?;
        let i64_at = |off: usize| i64::from_le_bytes(b[off..off + 8].try_into().unwrap());
        let mut trace_id = [0u8; 16];
        trace_id.copy_from_slice(&b[48..64]);
        Ok(Self {
            capture_ns: i64_at(8),
            send_ns: i64_at(16),
            recv_ns: i64_at(24),
            process_done_ns: i64_at(32),
            seq: u64::from_le_bytes(b[40..48].try_into().unwrap()),
            modality,
            trace_id,
        })
    }
}

/// Decoding errors.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EnvelopeError {
    WrongLength { expected: usize, got: usize },
    UnsupportedVersion(u8),
    UnknownModality(u8),
}

impl fmt::Display for EnvelopeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EnvelopeError::WrongLength { expected, got } => {
                write!(f, "envelope must be {expected} bytes, got {got}")
            }
            EnvelopeError::UnsupportedVersion(v) => write!(f, "unsupported envelope version {v}"),
            EnvelopeError::UnknownModality(m) => write!(f, "unknown modality value {m}"),
        }
    }
}

impl std::error::Error for EnvelopeError {}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> TimingEnvelope {
        TimingEnvelope {
            capture_ns: 1_700_000_000_000_000_001,
            send_ns: 1_700_000_000_000_000_002,
            recv_ns: 1_700_000_000_000_000_003,
            process_done_ns: 1_700_000_000_000_000_004,
            seq: 42,
            modality: Modality::PointCloud,
            trace_id: *b"0123456789abcdef",
        }
    }

    #[test]
    fn round_trip() {
        let e = sample();
        let bytes = e.to_bytes();
        assert_eq!(bytes.len(), ENVELOPE_WIRE_LEN);
        assert_eq!(TimingEnvelope::from_bytes(&bytes).unwrap(), e);
    }

    #[test]
    fn negative_timestamps_survive() {
        // Unsynchronized clocks can legitimately produce negative deltas;
        // the wire format must not mangle negative values.
        let mut e = sample();
        e.capture_ns = -5;
        let decoded = TimingEnvelope::from_bytes(&e.to_bytes()).unwrap();
        assert_eq!(decoded.capture_ns, -5);
    }

    #[test]
    fn rejects_wrong_length() {
        assert_eq!(
            TimingEnvelope::from_bytes(&[0u8; 63]),
            Err(EnvelopeError::WrongLength {
                expected: 64,
                got: 63
            })
        );
        assert_eq!(
            TimingEnvelope::from_bytes(&[0u8; 65]),
            Err(EnvelopeError::WrongLength {
                expected: 64,
                got: 65
            })
        );
    }

    #[test]
    fn rejects_unknown_version() {
        let mut b = sample().to_bytes();
        b[0] = 99;
        assert_eq!(
            TimingEnvelope::from_bytes(&b),
            Err(EnvelopeError::UnsupportedVersion(99))
        );
    }

    #[test]
    fn rejects_unknown_modality() {
        let mut b = sample().to_bytes();
        b[1] = 200;
        assert_eq!(
            TimingEnvelope::from_bytes(&b),
            Err(EnvelopeError::UnknownModality(200))
        );
    }

    #[test]
    fn garbage_is_an_error_not_a_panic() {
        let garbage: Vec<u8> = (0..64).map(|i| (i * 37 % 251) as u8).collect();
        // byte 0 = 0 -> unsupported version; must not panic either way
        let _ = TimingEnvelope::from_bytes(&garbage);
    }
}
