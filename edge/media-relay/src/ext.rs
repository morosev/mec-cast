//! The mec-cast RTP header extension: the sender's capture and send times.
//!
//! The patched libwebrtc (third_party/webrtc, `SendTimestampNsExtension`)
//! writes it on every video packet: 16 bytes, RFC 8285 one-byte form,
//! `capture_ns` then `send_ns`, each a big-endian u64 of CLOCK_REALTIME
//! nanoseconds. The relay reads it to measure the uplink, and writes it back
//! unchanged on every packet it forwards, so the receiver sees the sender's
//! stamps as if there were no relay.

use std::sync::Arc;

use str0m::rtp::{Extension, ExtensionSerializer, ExtensionValues};

/// The extension's URI, as the fork registers it.
pub const URI: &str = "http://www.mec-cast.org/experiments/rtp-hdrext/send-timestamp-ns";

/// The ID the relay offers it under. The offer's ID wins in an answer; 12 is
/// what libwebrtc usually picks, and free in str0m's default map.
pub const ID: u8 = 12;

/// The sender's two stamps, carried in `ExtensionValues::user_values`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SendTimes {
    pub capture_ns: u64,
    pub send_ns: u64,
}

impl SendTimes {
    pub const LEN: usize = 16;

    pub fn parse(buf: &[u8]) -> Option<Self> {
        if buf.len() < Self::LEN {
            return None;
        }
        Some(Self {
            capture_ns: u64::from_be_bytes(buf[0..8].try_into().ok()?),
            send_ns: u64::from_be_bytes(buf[8..16].try_into().ok()?),
        })
    }

    pub fn write(&self, buf: &mut [u8]) -> usize {
        if buf.len() < Self::LEN {
            return 0;
        }
        buf[0..8].copy_from_slice(&self.capture_ns.to_be_bytes());
        buf[8..16].copy_from_slice(&self.send_ns.to_be_bytes());
        Self::LEN
    }
}

#[derive(Debug)]
struct Serializer;

impl ExtensionSerializer for Serializer {
    fn write_to(&self, buf: &mut [u8], ev: &ExtensionValues) -> usize {
        ev.user_values
            .get::<SendTimes>()
            .map_or(0, |t| t.write(buf))
    }

    fn parse_value(&self, buf: &[u8], ev: &mut ExtensionValues) -> bool {
        match SendTimes::parse(buf) {
            Some(t) => {
                ev.user_values.set(t);
                true
            }
            None => false,
        }
    }

    fn is_video(&self) -> bool {
        true
    }

    fn is_audio(&self) -> bool {
        true
    }
}

/// The extension, for `RtcConfig::set_extension`.
pub fn extension() -> Extension {
    Extension::UnknownUri(URI.to_string(), Arc::new(Serializer))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_the_fork_wire_format() {
        // capture_ns = 0x0102030405060708, send_ns = 0x1112131415161718,
        // big-endian as the fork writes them.
        let wire: Vec<u8> = (1..=8).chain(0x11..=0x18).collect();
        let t = SendTimes::parse(&wire).unwrap();
        assert_eq!(t.capture_ns, 0x0102_0304_0506_0708);
        assert_eq!(t.send_ns, 0x1112_1314_1516_1718);
        let mut out = [0u8; 16];
        assert_eq!(t.write(&mut out), 16);
        assert_eq!(&out[..], &wire[..]);
    }

    #[test]
    fn a_short_value_is_not_parsed() {
        assert_eq!(SendTimes::parse(&[0u8; 15]), None);
    }

    #[test]
    fn the_serializer_carries_the_value_through_extension_values() {
        let wire: Vec<u8> = (1..=16).collect();
        let mut ev = ExtensionValues::default();
        assert!(Serializer.parse_value(&wire, &mut ev));
        let mut out = [0u8; 16];
        assert_eq!(Serializer.write_to(&mut out, &ev), 16);
        assert_eq!(&out[..], &wire[..]);
    }
}
