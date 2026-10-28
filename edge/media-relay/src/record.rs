//! Per-frame measurement at the relay: the middle site of the media profile.
//!
//! A video frame arrives as several RTP packets sharing one RTP timestamp, the
//! last one marked. The relay records one sample per frame, at site
//! [`SITE_RELAY`], in the same CSV schema as every other mec-cast node:
//!
//! - `capture_ns`, `send_ns`: the sender's stamps, from the mec-cast header
//!   extension (`ext::SendTimes`), as the receiver also sees them;
//! - `recv_ns`: when the frame's last packet reached the relay's socket;
//! - `process_done_ns`: when its last packet was handed to the receivers'
//!   egress, so `processing_ns` is the relay's own forwarding time;
//! - `aux_ns`: from the first to the last packet's arrival, how long the frame
//!   took to come in;
//! - `seq`: the frame's RTP timestamp, which the receiver's samples carry too:
//!   the key that joins the two sites per frame.
//!
//! `network_ns` (`recv_ns - send_ns`) is then the sender-to-relay leg alone:
//! over 5G, the uplink.

use std::collections::HashMap;
use std::time::Instant;

use mec_cast_telemetry::{
    Clock, Modality, RealtimeClock, Sample, SampleKind, SampleSender, TimingEnvelope,
};

use crate::ext::SendTimes;

/// The relay's site code. 0-2 are the ROS profile's (lidar, edge, renderer).
pub const SITE_RELAY: u8 = 3;

/// The 16-byte trace id of a run: the UUID's bytes when `run_id` is a UUID,
/// else its UTF-8 bytes, zero-padded or truncated, as the ROS nodes do.
pub fn trace_id(run_id: &str) -> [u8; 16] {
    let hex: String = run_id.chars().filter(|c| *c != '-').collect();
    let mut out = [0u8; 16];
    if hex.len() == 32 && hex.chars().all(|c| c.is_ascii_hexdigit()) {
        for (i, byte) in out.iter_mut().enumerate() {
            *byte = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap_or(0);
        }
        return out;
    }
    for (o, b) in out.iter_mut().zip(run_id.as_bytes()) {
        *o = *b;
    }
    out
}

struct Frame {
    rtp_ts: u32,
    first_recv_ns: i64,
    last_recv_ns: i64,
    forwarded_ns: i64,
    bytes: u64,
    times: Option<SendTimes>,
}

/// Turns forwarded packets into per-frame samples.
pub struct FrameRecorder {
    sender: Option<SampleSender>,
    trace_id: [u8; 16],
    // CLOCK_REALTIME at a known Instant: packet arrival Instants convert to
    // realtime through it, so every stamp is on the clock the other sites use.
    epoch: (Instant, i64),
    frames: HashMap<u32, Frame>,
    pub frames_recorded: u64,
}

impl FrameRecorder {
    pub fn new(sender: Option<SampleSender>, trace_id: [u8; 16]) -> Self {
        Self {
            sender,
            trace_id,
            epoch: (Instant::now(), RealtimeClock.now_ns()),
            frames: HashMap::new(),
            frames_recorded: 0,
        }
    }

    /// CLOCK_REALTIME nanoseconds at `at`.
    pub fn realtime_ns(&self, at: Instant) -> i64 {
        let (base, base_ns) = self.epoch;
        match at.checked_duration_since(base) {
            Some(d) => base_ns + d.as_nanos() as i64,
            None => base_ns - base.duration_since(at).as_nanos() as i64,
        }
    }

    /// One forwarded video packet.
    #[allow(clippy::too_many_arguments)]
    pub fn packet(
        &mut self,
        ssrc: u32,
        rtp_ts: u32,
        marker: bool,
        received: Instant,
        forwarded_ns: i64,
        bytes: usize,
        times: Option<SendTimes>,
    ) {
        let recv_ns = self.realtime_ns(received);
        // A new timestamp with the last frame unfinished: its marked packet
        // was lost. Record what arrived of it.
        if self.frames.get(&ssrc).is_some_and(|f| f.rtp_ts != rtp_ts) {
            if let Some(f) = self.frames.remove(&ssrc) {
                self.record(f);
            }
        }
        let f = self.frames.entry(ssrc).or_insert(Frame {
            rtp_ts,
            first_recv_ns: recv_ns,
            last_recv_ns: recv_ns,
            forwarded_ns,
            bytes: 0,
            times: None,
        });
        f.last_recv_ns = f.last_recv_ns.max(recv_ns);
        f.first_recv_ns = f.first_recv_ns.min(recv_ns);
        f.forwarded_ns = f.forwarded_ns.max(forwarded_ns);
        f.bytes += bytes as u64;
        if times.is_some() {
            f.times = times;
        }
        if marker {
            if let Some(f) = self.frames.remove(&ssrc) {
                self.record(f);
            }
        }
    }

    fn record(&mut self, f: Frame) {
        self.frames_recorded += 1;
        let Some(sender) = self.sender.as_mut() else {
            return;
        };
        let mut envelope = TimingEnvelope::new(Modality::Video, u64::from(f.rtp_ts), self.trace_id);
        if let Some(t) = f.times {
            envelope.capture_ns = t.capture_ns as i64;
            envelope.send_ns = t.send_ns as i64;
        }
        envelope.recv_ns = f.last_recv_ns;
        envelope.process_done_ns = f.forwarded_ns;
        sender.try_record(Sample {
            envelope,
            kind: SampleKind::Frame,
            site: SITE_RELAY,
            payload_bytes: u32::try_from(f.bytes).unwrap_or(u32::MAX),
            aux_ns: f.last_recv_ns - f.first_recv_ns,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn a_uuid_run_id_is_its_bytes() {
        let t = trace_id("01a16014-5386-7000-a6d6-93c35f5fd215");
        assert_eq!(t[0], 0x01);
        assert_eq!(t[15], 0x15);
    }

    #[test]
    fn another_run_id_is_padded_utf8() {
        let t = trace_id("dev-run");
        assert_eq!(&t[..7], b"dev-run");
        assert_eq!(t[7..], [0u8; 9]);
    }

    #[test]
    fn a_frame_is_recorded_at_its_marked_packet_or_when_the_next_begins() {
        let mut r = FrameRecorder::new(None, [0; 16]);
        let t0 = Instant::now();
        r.packet(1, 100, false, t0, 0, 1000, None);
        r.packet(1, 100, true, t0 + Duration::from_millis(1), 0, 500, None);
        assert_eq!(r.frames_recorded, 1);
        // Frame 200 loses its marked packet; frame 300 begins.
        r.packet(1, 200, false, t0, 0, 1000, None);
        r.packet(1, 300, false, t0, 0, 1000, None);
        assert_eq!(r.frames_recorded, 2);
        // Another SSRC is its own frame stream.
        r.packet(2, 300, true, t0, 0, 10, None);
        assert_eq!(r.frames_recorded, 3);
    }

    #[test]
    fn realtime_follows_the_instant() {
        let r = FrameRecorder::new(None, [0; 16]);
        let (base, base_ns) = r.epoch;
        assert_eq!(
            r.realtime_ns(base + Duration::from_millis(5)),
            base_ns + 5_000_000
        );
    }
}
