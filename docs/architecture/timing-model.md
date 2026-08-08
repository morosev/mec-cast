# Timing model — what each metric means and when it is valid

Every number this platform produces is a subtraction of two timestamps.
Whether it means anything depends entirely on **which clocks** those two
timestamps came from. This page is the reference for that.

## The four stamps

Carried in the 64-byte `TimingEnvelope` alongside every unit of data:

| Stamp | Taken | By |
|---|---|---|
| `capture_ns` | Sensor delivers the frame | UE |
| `send_ns` | Immediately before handing to the transport | UE |
| `recv_ns` | First line of the receive callback | Edge |
| `process_done_ns` | After application processing completes | Edge |

## The derived metrics

| Metric | Definition | Cross-host? | Valid without PTP? |
|---|---|---|---|
| `network_ns` | `recv_ns − send_ns` | **Yes** | ❌ measures clock offset |
| `e2e_ns` | `process_done_ns − capture_ns` | **Yes** | ❌ |
| `sender_ns` | `send_ns − capture_ns` | No | ✅ |
| `processing_ns` | `process_done_ns − recv_ns` | No | ✅ |

The rule: **a metric spanning two hosts is only as good as the clock
synchronisation between them.** Local-only metrics are always valid because
both stamps come from the same clock.

## Same-host runs are a special case

In the local docker topology, all containers share the host kernel clock.
Cross-host metrics are therefore valid *even though PTP is absent* — the
offset is genuinely zero, not merely assumed to be. The telemetry layer
reports `ptp.reliable = false` in that situation, which is honest rather
than pessimistic: it says "no PTP was verified", not "these numbers are
wrong". Interpretation is the analyst's job, and `run.json` records which
topology produced the run.

## Negative values are a signal, not a bug

If `network_ns` comes out negative, the receiver's clock is behind the
sender's. That is clock skew being reported faithfully. The old C++ layer
hid this by initialising `max` to `0` — an all-negative series reported a
maximum that was never observed
([ADR-0004](adr/0004-exact-percentiles.md)). The Rust core initialises to
`i64::MIN` and returns `None` for an empty window, so skew shows up as skew.

## Clock sources

`Clock` implementations, selected per deployment:

- **`PhcClock`** — reads `/dev/ptp0` directly. The NIC hardware clock,
  disciplined by `ptp4l` to a grandmaster.
- **`RealtimeClock`** — `CLOCK_REALTIME`, disciplined by `phc2sys` from the
  PHC. This is the default on lab hosts; it is what makes cross-host
  subtraction valid.
- **`MonotonicClock`** — never jumps, but not comparable across machines.
  Use for intervals within one process.
- **`MockClock`** — deterministic, for tests.

Mixing sources across the two ends of a subtraction is the subtle failure
mode: a PHC stamp minus a `CLOCK_REALTIME` stamp silently includes the
phc2sys offset, which is the same magnitude as the effect being measured.
Keep both ends of every metric on the same source.

## Precision budget

| Setup | Expected accuracy |
|---|---|
| PTP + hardware timestamping, same switch | 10–100 ns |
| PTP + hardware timestamping, multiple hops | 100–500 ns |
| PTP with software timestamping | 1–10 µs |
| No PTP (NTP-style signalling fallback) | 1–5 ms |

The effects under study are milliseconds. The first three rows are
comfortably sufficient; the fourth is the same order as the signal and is
therefore only acceptable for functional testing.
