# Architecture Decision Records

Short, numbered records of decisions that were expensive to make and would
otherwise be re-litigated. Each states the context, the decision, why the
alternatives lost, and what the decision costs.

| # | Decision | Status |
|---|----------|--------|
| [0001](0001-zenoh-over-dds.md) | rmw_zenoh rather than raw DDS for point clouds | Accepted |
| [0002](0002-rust-telemetry-core.md) | One shared telemetry core, in Rust | Accepted |
| [0003](0003-ptp-on-management-lan.md) | PTP over the management LAN, not the 5G user plane | Accepted |
| [0004](0004-exact-percentiles.md) | Exact windowed percentiles, not a streaming estimator | Accepted |
| [0005](0005-mac-metrics-tap-before-ric.md) | MAC metrics tap before an E2/RIC xApp | Accepted |
| [0006](0006-reliable-udp-transport.md) | Reliable UDP for the Zenoh transport | Accepted (amended 2026-08-20, 2026-08-30) |
| [0007](0007-websocket-control-plane.md) | A WebSocket control plane for run orchestration | Accepted |
| [0008](0008-run-identity-and-store.md) | Run identity is a UUIDv7, stored in `run.json` | Accepted |
| [0009](0009-render-return-path.md) | A return path and a UE-side renderer | Accepted |

## Writing a new one

Copy the shape of an existing record. Keep it to one page.

```markdown
# ADR-000N: <decision in the imperative>

- **Status:** Proposed | Accepted | Superseded by ADR-000M
- **Date:** YYYY-MM-DD

## Context
What forces are at play? What makes this hard?

## Decision
What we are doing.

## Rationale
Why this over the alternatives — name them and say why they lost.

## Consequences
What this costs, what it forecloses, and what must now be true.
```

A record is never edited after acceptance except to mark it superseded.
The history of what was believed and when is the point.
