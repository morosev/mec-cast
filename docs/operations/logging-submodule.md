# The `services/logging` submodule

`services/logging` is a submodule of
[mec-cast-logging-service](https://github.com/morosev/mec-cast-logging-service):
a FastAPI + PostgreSQL service that ingests aggregated latency snapshots and
makes them queryable by `trace_id`, service, level, and JSONB `context`
containment.

## Getting it

```bash
git submodule update --init services/logging
test -f services/logging/pyproject.toml && echo OK
```

A fresh clone should use `--recurse-submodules`; `scripts/bootstrap-dev.sh`
initialises it otherwise.

## Why a submodule rather than a merge

- It is an independent deployable with its own database, migrations, and
  release cadence — merging would dissolve a real boundary and put two
  unrelated Python projects under one CI configuration.
- Telemetry's HTTP sink is coupled to its `LogEntryCreate` schema, which is
  `extra="forbid"`: **any** unexpected top-level field is a 422 at runtime.
  Pinning a submodule SHA makes "which schema version does this build
  target" an explicit, reviewable fact rather than whatever happens to be
  checked out next door.
- It removes the out-of-repo `../` build context the compose files used to
  need, so a fresh clone gives a working e2e suite and CI can run the job.

## Operational posture — read before exposing it

The service was built with a **deliberately unauthenticated core**. That
shapes three things you must respect when deploying to the lab:

- **No authentication, no rate limiting.** Anyone who can reach the port
  can both ingest and read every log entry. Bind it to the management LAN
  only — never to a UE-reachable or public interface. `deploy/lab/` keeps
  it on the infra role for exactly this reason.
- **There is now a browser UI, and it is unauthenticated too.** `/dashboard`
  serves the telemetry dashboard and `/` redirects to it, so the root of the
  service is a readable page rather than a 404. This does not widen what is
  exposed — querying was always open — but it changes who reaches it: an
  unauthenticated API invites a deliberate `curl`, an unauthenticated page
  invites anyone who pastes the host into a browser. Treat the port as the
  boundary, not the path.
- **Bulk deletion is not an HTTP endpoint.** Retention is a CLI command
  (`mec-cast-logs purge --days 30 --dry-run`) precisely so that destructive
  bulk operations are not reachable over an unauthenticated API. Run it
  from cron or a systemd timer; nothing is deleted automatically. Still true
  with the dashboard in place: it is strictly read-only.
- **Explicitly out of scope upstream:** auth, rate limiting, and non-HTTP
  ingestion (syslog, message queues). If a mec-cast component needs one of
  these, it is a change to the service, not a workaround here.

## What is actually written to the database

One row per **snapshot window** per recorder, in `log_entries`. A node with a
recorder posts one every `interval_s` (2 s by default) while a run is
streaming, so a five-minute run with three components writes roughly 450 rows,
not one per frame. Per-frame truth lives in `samples.csv`; the database holds
the aggregate.

The top level is the service's own `LogEntryCreate` shape and carries nothing
mec-cast-specific — everything of ours goes inside `context`:

| Column | Value |
|---|---|
| `service` | `mec-cast-<role>-<instance>` — `mec-cast-edge-0`, `mec-cast-pub-1` |
| `host` | the reporting host |
| `trace_id` | **the `RUN_ID`** — the join key across every component |
| `message` | a human line; the numbers are all in `context` |
| `context` | the snapshot, below |

`context` is written by the Rust recorder and looks like this:

```json
{
  "run_id": "01a04fae-50a5-7000-ad8e-5f539cdfc60f",
  "interval_s": 2.0,
  "metrics": {
    "e2e":        { "count": 19, "min_ns": 103808154, "max_ns": 371037189,
                    "mean_ns": 240082532.4, "stddev_ns": 78518589.3,
                    "p50_ns": 240732239, "p90_ns": 362828722,
                    "p99_ns": 371037189, "last_ns": 103808154 },
    "network":    { "…same shape…" },
    "processing": { "…same shape…" },
    "sender":     { "…same shape…" }
  },
  "drops":       { "samples_total": 0, "samples_delta": 0, "snapshots": 0 },
  "ptp":         { "offset_ns": 0, "reliable": false },
  "seq":         { "first": 0, "last": 19 },
  "rows_written": 20
}
```

Every metric block has the same nine keys. A metric is **absent** rather than
zero when its two stamps were not both set, which is how a publisher's window
carries no `e2e` at all.

### The four metrics, and why `e2e` is not one thing

Each is a difference between two stamps in the 64-byte timing envelope, and
the recorder derives all of them the same way wherever it runs:

| Metric | Stamps | Notes |
|---|---|---|
| `sender` | `capture_ns → send_ns` | time inside the publisher. Local, PTP-free |
| `network` | `send_ns → recv_ns` | one wire hop. **PTP-dependent** |
| `processing` | `recv_ns → process_done_ns` | work at the receiver. Local |
| `e2e` | `capture_ns → process_done_ns` | see below |

`e2e` is derived identically everywhere, so **it measures whatever leg the
recording node closes**:

- **`mec-cast-edge-*`** stamps `process_done_ns` when voxelisation finishes,
  so its `e2e` is the **sending leg — lidar to edge**. This is the platform's
  headline measurement. It is PTP-dependent, because the two stamps come off
  two hosts.
- **`mec-cast-render-*`** stamps it when the draw returns, so its `e2e` is the
  **full round trip**. Per ADR-0009 this one is PTP-free, since `capture_ns`
  and `process_done_ns` both come off the paired UE's clock — but only while
  the paired lidar is on that same host, which `WF_RENDER_CROSS_HOST` exists
  to catch.
- **`mec-cast-pub-*`** never sets `process_done_ns`, so it reports **no `e2e`
  at all**. Its windows carry `sender` only.

The three are not comparable and must not be pooled. Querying them together
gives a number that is neither leg — which is exactly what the dashboard used
to do, and why its headline is now scoped to the sending leg.

### Reading it back

Sessions are separated by `context ? 'metrics'`, which is what distinguishes a
telemetry snapshot from an ordinary log line and rides the existing GIN index.
To pull one run's sending leg straight from SQL:

```sql
SELECT "timestamp",
       (context->'metrics'->'e2e'->>'p50_ns')::bigint / 1e6 AS p50_ms,
       (context->'metrics'->'e2e'->>'p99_ns')::bigint / 1e6 AS p99_ms
FROM log_entries
WHERE trace_id = '<RUN_ID>'
  AND service LIKE 'mec-cast-edge-%'
  AND context ? 'metrics'
ORDER BY "timestamp";
```

Drop the `service` filter and the rows from three components interleave, one
per window each.

## The schema contract

Snapshots posted by `mec-cast-telemetry` must match `LogEntryCreate`
exactly — top level is only `timestamp, level, service, host, logger,
message, context, trace_id`. Everything mec-cast-specific goes inside
`context`. When bumping the submodule, re-run:

```bash
make test-e2e
```

`tests/e2e/test_e2e_latency.py` asserts snapshots actually land and are
queryable by `trace_id`, so a schema drift fails there rather than silently
in the lab.

## Pointing the build elsewhere

The compose files take the build context from `MECLOG_BUILD_CONTEXT`,
defaulting to `../../services/logging`. To build from a working tree
instead:

```bash
MECLOG_BUILD_CONTEXT=../../../mec-cast-logging-service make up-logging
```

`tests/e2e/` and `scripts/run-experiment.sh` set this automatically if the
submodule is ever empty, so the suite degrades gracefully rather than
failing with a confusing missing-`pyproject.toml` error.
