# ADR-0005: Start RAN visibility with a MAC metrics tap, not an E2/RIC xApp

- **Status:** Accepted
- **Date:** 2026-08-06

## Context

Application-layer latency alone cannot explain *why* a point cloud arrived
late. The interesting correlations are with RAN state: uplink grant
timing, MCS selection, PRB utilisation, HARQ retransmissions, buffer
status reports, CQI, SINR.

The O-RAN-native way to obtain these is a near-real-time RIC (FlexRIC or
the OSC RIC) with an xApp subscribing to E2SM-KPM over the E2 interface.
srsRAN Project ships an E2 agent, so this is achievable.

## Decision

**Phase RAN-1:** consume srsRAN's existing **JSON-over-UDP metrics export**
with a small collector (`ran/collector`) that stamps arrival using the same
PTP-disciplined clock as the endpoints and feeds the shared telemetry
recorder.

**Phase RAN-2 (deferred):** FlexRIC + an E2SM-KPM xApp.
**Phase RAN-3 (speculative):** E2SM-RC for scheduling/slicing control.

## Rationale

- The metrics tap is roughly a day of work and yields the *same KPIs* for
  correlation purposes. A RIC deployment is weeks of infrastructure before
  the first correlated data point exists.
- The research question in phase 1 is **observational** — "how does RAN
  state correlate with application latency" — and observation does not
  require the control plane that E2 exists to provide.
- Standing up a RIC first would front-load the largest integration risk in
  the project onto the phase with the least payoff.
- Nothing is foreclosed: because the collector emits into the same
  recorder, snapshot schema, and `trace_id` join key, an xApp source can
  be added later as an additional producer without touching consumers.

## Consequences

- The tap is **observe-only**. Experiments that require influencing the
  scheduler (QoS-flow prioritisation for the LiDAR UE, slicing) are out of
  scope until RAN-3.
- srsRAN's metrics JSON schema varies between versions, so the collector
  parses leniently and routes unknown fields into `context`. A fixture
  captured from the lab's actual gNB build is pinned in
  `ran/collector/testdata/`.
- The tap is srsRAN-specific. A different gNB means a different collector
  front end — whereas E2SM-KPM would have been vendor-neutral. This is the
  main cost being accepted.
- Correlation depends on the gNB host being PTP-synced like the endpoints
  (see ADR-0003).
