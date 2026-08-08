# ADR-0003: Synchronise clocks over the management LAN, not the 5G user plane

- **Status:** Accepted
- **Date:** 2026-08-06

## Context

Every cross-host one-way metric — network delay, glass-to-glass, the
sender/receiver pipeline split — is a subtraction of timestamps taken on
two different machines. Without a shared clock those numbers measure
clock offset, not latency, and nothing downstream can tell the difference.
They can even come out negative.

5G *can* carry time synchronisation: 3GPP defines 5G-TSN with DS-TT and
NW-TT translators that distribute a PTP domain across the radio path. It
would be architecturally elegant for the UE to get its clock through the
same link it measures.

**srsRAN and Open5GS do not implement 5G-TSN.** The lab's 5G user plane
therefore cannot distribute a clock.

## Decision

Discipline `CLOCK_REALTIME` on the UE-compute host, the edge server, and
the gNB host from a common PTP grandmaster reachable on the
**management/backhaul LAN**, using `ptp4l` + `phc2sys` with NIC hardware
timestamping. The 5G path carries measured traffic only, never sync.

## Rationale

- It is the only option that actually works on this testbed.
- It is also *methodologically cleaner*: the clock distribution path is
  independent of the path under measurement, so impairing the 5G link
  cannot perturb the measurement reference.
- Hardware timestamping on a switched LAN gives 10–100 ns, comfortably
  below the millisecond-scale effects being studied.

## Consequences

- Every measuring host needs a NIC with IEEE 1588 hardware timestamping
  and a second interface on the management LAN.
- A grandmaster (dedicated appliance or GPS-disciplined source) becomes
  lab infrastructure.
- **Sync quality must be verified per campaign.** `deploy/lab/ptp/verify-ptp.sh`
  gates this, and `PtpMonitor` records `ptp.reliable` plus the measured
  offset in every snapshot, so a run made without sync is identifiable
  after the fact.
- Same-host runs (the local docker topology) share the kernel clock and
  are valid without any PTP; they honestly report `ptp.reliable = false`.
- Without PTP the system falls back to a signalling-based NTP-style
  estimate (~1–5 ms). That is fine for functional testing and **not**
  fine for published measurements.
- If the lab ever adopts a 5G-TSN-capable core, distributing the clock
  in-band becomes a genuine research topic in its own right.
