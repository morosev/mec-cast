# ADR-0010: Keep the JSON tap, and add an E2 xApp beside it

- **Status:** Accepted
- **Date:** 2026-10-09
- **Supersedes:** [ADR-0005](0005-mac-metrics-tap-before-ric.md)

## Context

ADR-0005 started RAN visibility with a tap on srsRAN's JSON metrics and
deferred a near-RT RIC with an E2SM-KPM xApp. Two of its premises have since
turned out to be wrong.

**"The tap yields the same KPIs."** It yields far more. srsRAN's E2 agent
exposes six DRB metrics (`DRB.UEThpDl/Ul`, `DRB.RlcSduTransmittedVolumeDL/UL`,
`DRB.RlcPacketDropRateDl`, `DRB.PacketSuccessRateUlgNBUu`). The CQI, RSRP and
RSRQ it also exposes are documented as dummy values to be removed. The
monitoring period cannot go below 1 s. The JSON export carries, per UE: MCS,
CQI, HARQ ok/nok counts, BSR, SNR, RSRP, timing advance, and the scheduler's
own delays (`sr_to_pusch_delay`, `pusch_harq_delay`, `crc_delay`). Per cell it
carries latency histograms, late HARQs and PRB usage.

**"srsRAN exports JSON over UDP."** Release 25.04 dropped `metrics.addr/port`.
Current releases serve the same JSON over the `remote_control` WebSocket to a
client that sends `{"cmd":"metrics_subscribe"}`. A tap that only binds UDP
receives nothing from a current gNB.

Meanwhile, what E2 offers that the tap cannot is still real:
- it is vendor-neutral;
- its report formats are standardised;
- through E2SM-RC, it is the only route to *control* — slice-level PRB quota
  is the control srsRAN supports.

## Decision

Two RAN sources, both permanent, feeding one RAN data model.

1. **The JSON tap** (`ran/collector`) stays the deep, srsRAN-specific record
   and is saved locally per run.
   - It reads either transport, chosen by configuration:
     `GNB_METRICS_SOURCE=udp|ws|auto`. `auto` listens on both and locks onto
     whichever delivers first, so a report is never recorded twice.
2. **An E2 xApp** (`ran/xapp`, Python) on the O-RAN SC near-RT RIC is the
   standards-aligned path.
   - It starts with E2SM-KPM and grows into E2SM-RC control.
   - It is built around capabilities (monitor, control, …) and an adapter
     per RIC, so new behaviour is a new capability rather than a fork.
3. **Both sources normalise into one schema.** A Rust normaliser and a Python
   one are held together by a shared vector file, the same discipline the
   admin protocol uses. Analysis does not care which source produced a row.
4. **The RIC runs beside this repository, never inside it.**
   - It is srsRAN's `oran-sc-ric` (AGPL-3.0), at a pinned commit, deployed
     on the infra role.
   - The xApp is written against `ricxappframe` (Apache-2.0), not by copying
     `oran-sc-ric`'s helper modules.
5. **Local testing does not need a radio.** `gnb-sim` emits srsRAN-shaped
   reports over both JSON transports. From the xApp phase on, it also emits
   KPM-shaped indications from the same internal state, so the parity check
   and the control loop run on a laptop.

## Rationale

- **Replacing the tap with KPM** would trade the richest observability in
  the platform for a standard, and gain nothing for the research questions
  that need per-UE scheduler state.
- **Keeping only the tap** would close off control experiments (PRB
  reservation for the LiDAR UE, closed loops on application latency) and
  leave the platform tied to one vendor's JSON forever.
- **Two sources** are the cost of having both. The shared schema and a live
  parity check (the same throughput seen through both) turn that cost into a
  cross-validation that neither source could provide alone.
- **O-SC RIC over FlexRIC.** srsRAN tests against it, it deploys with Docker
  Compose and no Kubernetes, and its Python framework fits a repository whose
  control-plane client is already Python. FlexRIC needs an older branch for
  srsRAN compatibility and a gcc-10 source build. An adapter can still add
  it later.

## Consequences

- srsRAN's JSON schema drift is still ours to absorb. The tap keeps parsing
  leniently and keeps the raw report beside anything it extracts.
- The RIC is roughly seven more containers, plus SCTP between the gNB and the
  RIC, all on the management LAN (ADR-0003).
- KPM figures are 1 s granularity at best. Per-frame explanation still comes
  from the tap, or from MAC traces offline — never from E2.
- Control changes what is being measured. ADR-0011 will set the rule: every
  policy is run-scoped, recorded as data, and reverted when the run ends.
