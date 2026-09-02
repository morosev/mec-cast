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

- **`PhcClock`** — reads a `/dev/ptpN` device directly. The NIC hardware
  clock, disciplined by `ptp4l` to a grandmaster. **Which N is not
  knowable in advance**: index 0 is whichever NIC the driver registered
  first, so the device is configured (`PTP_DEVICE`, or derived from
  `PTP_IFACE`) rather than assumed.
- **`RealtimeClock`** — `CLOCK_REALTIME`, disciplined from the PHC by
  `phc2sys` or by chrony with a PHC refclock. This is the default on lab
  hosts.
- **`MonotonicClock`** — never jumps, but not comparable across machines.
  Use for intervals within one process.
- **`MockClock`** — deterministic, for tests.

### What makes cross-host subtraction valid

Not that each end is disciplined — that both ends trace to the **same
grandmaster**. Being disciplined is necessary and nowhere near sufficient,
and the difference is not academic: it is the failure the lab actually hit.

There are three ways to get this wrong, in increasing order of how well they
hide.

**Mixed sources.** A PHC stamp minus a `CLOCK_REALTIME` stamp silently
includes the offset between them, which is the same magnitude as the effect
being measured. Keep both ends of every metric on the same source.

**Two roots.** Each host disciplined perfectly to a *different* grandmaster,
or to the same one in a different PTP domain. Every local indicator is green
on both, `ptp.reliable` is true on both, and every cross-host delay is wrong
by whatever the roots disagree by. Compare `grandmasterIdentity` — it is the
only value that settles it:

```bash
sudo pmc -u -b 0 'GET PARENT_DATA_SET' | grep grandmasterIdentity
```

**The wrong device.** The worst of the three, because it survives every check
above. A host may run `ptp4l` on one NIC while chrony takes `CLOCK_REALTIME`
from a *different*, undisciplined NIC's PHC — and chrony will report
nanosecond accuracy the whole time, because it is measuring the system clock
against the very clock it is slaving it to. A free-running crystal reaches
seconds of error in weeks. Observed in this lab: `ptp4l` on `ens3f0`
(`/dev/ptp2`, locked to −11 ns), chrony following `/dev/ptp3` (`ens3f1`,
disciplined by nothing), containers handed `/dev/ptp0` (a third NIC) —
**11.15 s** of skew with every indicator healthy. Chrony's `refid` is a free
text label and read `PHC0` throughout.

The check that catches it is comparing the *disciplined* PHC against
`CLOCK_REALTIME`, which `deploy/lab/ptp/verify-ptp.sh` now does.

### `ptp.reliable` is a local statement

`PtpQuality` is `|PHC − CLOCK_REALTIME|` **on one host**. It cannot see any
of the three failures above, because all of them concern the relationship
between two hosts, or between the system clock and a device it was never
pointed at. It is a necessary condition, not a verdict.

The diagnostic that does catch skew is arithmetic on the data itself: a
negative one-way delay is impossible, so the recorder counts them and the
admin raises `WF_CLOCK_SKEW`. That fires after a run is already
contaminated. `verify-ptp.sh --peer <host>` is the pre-flight equivalent.

## Precision budget

| Setup | Expected accuracy |
|---|---|
| PTP + hardware timestamping, same switch | 10–100 ns |
| PTP + hardware timestamping, multiple hops | 100–500 ns |
| PTP with software timestamping | 1–10 µs |
| No PTP (NTP-style signalling fallback) | 1–5 ms |
| Two roots, or CLOCK_REALTIME on an undisciplined device | **unbounded** |

The effects under study are milliseconds. The first three rows are
comfortably sufficient; the fourth is the same order as the signal and is
therefore only acceptable for functional testing.

The last row is not a precision figure at all — it is a correctness failure
wearing one. Each host reports tens of nanoseconds while the pair is seconds
apart, so it appears nowhere in this table's terms and is bounded only by how
long the wrong clock has been drifting.
