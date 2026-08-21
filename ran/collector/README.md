# ran-collector

Taps the srsRAN O-DU's MAC/scheduler metrics so RAN state can be correlated with
application-layer latency — answering not just *how late* a point cloud was
but *why*.

## How it works

srsRAN Project's gNB exports metrics as JSON over UDP. Point it here:

```yaml
# gnb.yml
metrics:
  addr: <collector host>
  port: 55555
```

The collector binds that socket, stamps each datagram's arrival with the
same PTP-disciplined clock the UE and edge use, and feeds the shared
telemetry recorder. RAN KPIs and application latency therefore land on one
time base and join on `trace_id = RUN_ID`.

KPIs of interest: DL/UL MCS, PRB utilisation, HARQ retransmissions, buffer
status reports, CQI, RSRP/SINR, per-UE throughput.

## Run

```bash
GNB_METRICS_ADDR=0.0.0.0:55555 \
RUN_ID=$(uuidgen) \
LOGGING_URL=http://infra-host:8000 \
cargo run --release -p ran-collector
```

| Variable | Default | Meaning |
|---|---|---|
| `GNB_METRICS_ADDR` | `0.0.0.0:55555` | UDP bind address |
| `RUN_ID` | `dev-run` | Experiment id — must match the other roles |
| `RUNS_DIR` | `runs` | Output base directory |
| `LOGGING_URL` | — | Logging service; omit to write CSV only |

In the lab it runs as a container beside the gNB:
`deploy/lab/compose.gnb.yml`.

## Testing without the lab

`testdata/srsran_metrics.jsonl` holds captured datagrams;
`tests/replay.rs` feeds them over a real UDP socket and asserts the parse
and recording path. Pin a fresh fixture whenever the lab's gNB version
changes — srsRAN's metrics schema varies between releases, which is why the
parser is lenient and routes unknown fields into `context`.

```bash
cargo test -p ran-collector
```

## Scope

**Observe only.** No E2, no RIC, no control. That is a deliberate phasing
decision — a metrics tap is a day of work and yields the same KPIs for
correlation, whereas a near-RT RIC is weeks of infrastructure before the
first correlated data point. Because this emits into the same recorder and
snapshot schema, an E2SM-KPM xApp can later be added as an additional
producer without touching any consumer. See
[ADR-0005](../../docs/architecture/adr/0005-mac-metrics-tap-before-ric.md).

## Admin control plane

With `ADMIN_URL` set the collector joins the admin service and records only
between `run.start` and `run.stop`. Datagrams arriving while idle are counted
but not recorded — that count is what lets the admin distinguish "srsRAN is
sending nothing" from "we are simply not recording".

The client is synchronous `tungstenite` behind a default-on `admin` feature:
one thread, a read timeout so the stop flag is observed, and bounded channels
that drop rather than block — the same shape the telemetry recorder already
uses. **No async runtime enters the dependency tree**, and CI builds
`--no-default-features` to keep it that way.

Without `ADMIN_URL` the collector records immediately under the environment's
`RUN_ID`, exactly as before.
