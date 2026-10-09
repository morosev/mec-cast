# ran-collector

Taps the srsRAN O-DU's MAC/scheduler metrics so RAN state can be correlated with
application-layer latency — answering not just *how late* a point cloud was
but *why*.

## How it works

srsRAN Project's gNB exports metrics as JSON. Which transport depends on the
release, and the collector reads either:

| srsRAN | Transport | gnb.yml |
|---|---|---|
| ≤ 24.x | UDP push, one datagram per report | `metrics: {enable_json_metrics: true, addr: <collector host>, port: 55555}` |
| 25.04+ | `remote_control` WebSocket; the collector connects and sends `{"cmd":"metrics_subscribe"}` | `metrics: {enable_json: true}` and `remote_control: {enabled: true, bind_addr: 127.0.0.1, port: 8001}` |

```yaml
# srsRAN 25.04+
metrics:
  enable_json: true
remote_control:
  enabled: true
  bind_addr: 127.0.0.1     # the collector runs on the gNB host (network_mode: host)
  port: 8001
```

`GNB_METRICS_SOURCE` picks the transport:
- **`udp`** reads only the UDP push.
- **`ws`** reads only the WebSocket.
- **`auto`** is the default. It listens on both and locks onto whichever
  delivers first. A gNB that somehow sends on both is still recorded once per
  report, not twice.

The chosen transport, and any WebSocket connection error, appear in the
admin's gNB node status: `source`, `transport`, `ws_connected`,
`ws_last_error`.

**What each report produces**
- **One CSV row**, `kind=event`, `site=2`:
  - `recv_ns` is the arrival time on the shared telemetry clock.
  - `send_ns` is **the gNB's own timestamp** for the report. srsRAN has written
    it as epoch seconds and as a zoneless ISO string, which is read as UTC.
  - `network_ns` is therefore the metrics pipeline's lag. Collector and gNB
    share a host, so it needs no PTP.
- **One line in `reports.jsonl`**: the report verbatim. This is the local
  record of the deep source, and the way a lab capture becomes a fixture
  (`scripts/ran-fixture.sh`). Turn it off with `RAN_RAW_REPORTS=0`.
- **One logging-service entry**: `service: "mec-cast-ran"`,
  `trace_id: run_id`. Its `context` holds:
  - the report verbatim, as `kpi`;
  - `recv_ns` and `gnb_ts_ns`;
  - `ptp: {offset_ns, reliable}`.

KPIs of interest: DL/UL MCS, CQI, HARQ ok/nok, BSR, SNR/RSRP, timing advance,
per-UE throughput, and the scheduler's own delays (`sr_to_pusch_delay`,
`pusch_harq_delay`, `crc_delay`).

## Run

```bash
GNB_METRICS_SOURCE=auto \
RUN_ID=$(uuidgen | tr A-F a-f) \
LOGGING_URL=http://infra-host:8000 \
cargo run --release -p ran-collector
```

| Variable | Default | Meaning |
|---|---|---|
| `GNB_METRICS_SOURCE` | `auto` | `udp`, `ws`, or `auto` (both, lock onto the first to deliver) |
| `GNB_METRICS_ADDR` | `0.0.0.0:55555` | UDP bind address |
| `GNB_METRICS_WS` | `127.0.0.1:8001` | srsRAN `remote_control`, `host:port` or `ws://` URL |
| `PTP_DEVICE` | — | PHC for `ptp.reliable`, e.g. `/dev/ptp0`; needs the `linux-ptp` build |
| `RUN_ID` | `dev-run` | Experiment id — must match the other roles; ignored under the admin |
| `RUNS_DIR` | `runs` | Base directory; each run writes `<RUNS_DIR>/<run_id>/ran/` |
| `RAN_RAW_REPORTS` | `1` | Keep every report verbatim in `<run>/ran/reports.jsonl` |
| `LOGGING_URL` | — | Logging service; omit to write CSV only |
| `ADMIN_URL` | — | Admin control plane; with it, the admin names and scopes the runs |

**Build features**
- `admin` and `ws` are on by default.
- `--no-default-features` builds a UDP-only collector with no websocket
  dependency. CI builds it to keep it that way.
- `linux-ptp` opens the PHC. The image (`deploy/docker/ran.Dockerfile`)
  builds with it.

In the lab it runs as a container beside the gNB:
`deploy/lab/compose.gnb.yml`.

## Testing without the lab

**Fixtures.** One line per report:

| File | Shape | Provenance |
|---|---|---|
| `testdata/srsran_metrics.jsonl` | ≤ 24.x (`ue_list[].ue_container`, numeric timestamp) | Hand-written. To be replaced by a lab capture. |
| `testdata/srsran_ws_metrics.synthetic.jsonl` | 25.x (`cells[].{cell_metrics,event_list,ue_list}`, ISO timestamp) | **Synthetic**, shaped after the srsRAN docs. To be replaced by a lab capture. |

Pin a real capture whenever the lab's gNB version changes. The schema varies
between releases, which is why the parser is lenient and forwards the whole
report.

```bash
bash scripts/ran-fixture.sh <run_id> <srsran-version> <udp|ws>
```

This copies the run's `reports.jsonl` into `testdata/srsran_<version>.lab.jsonl`
with a provenance sidecar. `tests/fixtures.rs` checks **every** `testdata/*.jsonl`
without naming it:
- each line parses;
- each report's timestamp is readable;
- some report carries a UE;
- a `.lab.jsonl` has a sidecar naming its srsRAN version.

The lab procedure is in the deploy manual,
[The gNB — metrics tap, E2 and the RIC](../../docs/operations/deploy-manual.md#the-gnb--metrics-tap-e2-and-the-ric).

**Tests.** They replay those fixtures over real sockets:

```bash
cargo test -p ran-collector
```

- `tests/replay.rs`: UDP.
- `tests/ws_replay.rs`: a stub `remote_control` server, plus `auto` against a
  gNB sending on both transports.
- `tests/admin_ws.rs`: the control plane, including two admin-driven runs
  landing in two directories.
- `tests/fixtures.rs`: every fixture, as above.

**The whole path in containers, with no radio and no srsRAN build.**
`ran/sim/gnb_sim.py` (gnb-sim) emits the fixture's reports over UDP and the
WebSocket at the same time, re-stamped with the current time:

```bash
make up-ran                       # RAN_SOURCE=auto by default
RAN_SOURCE=ws make up-ran         # pin the transport
make up-ran-admin                 # the same, driven by the admin
pytest tests/e2e/test_ran_local.py -v   # udp, ws and auto, end to end
```

`SIM_MODE=model` makes the per-UE figures take a seeded random walk instead of
cycling the fixture.

## Scope

**Observe only.** No E2, no RIC, no control. This collector is the deep,
srsRAN-specific source. The standards-aligned E2 xApp is planned *beside* it,
not instead of it, and both normalise into one RAN data model — see
[ADR-0010](../../docs/architecture/adr/0010-two-ran-sources.md), which
supersedes ADR-0005.

## Admin control plane

With `ADMIN_URL` set, the collector joins the admin service and records only
between `run.start` and `run.stop`. Each run gets its own
`<RUNS_DIR>/<run_id>/ran/`.

Reports arriving while idle are counted but not recorded. That count is what
lets the admin tell "srsRAN is sending nothing" (`WF_GNB_SILENT`) apart from
"we are simply not recording".

The client is synchronous `tungstenite`, the same crate the WebSocket metrics
source uses:
- one thread;
- a read timeout so the stop flag is observed;
- bounded channels that drop rather than block, the same shape the telemetry
  recorder already uses.

**No async runtime enters the dependency tree.**

Without `ADMIN_URL`, the collector records immediately under the
environment's `RUN_ID`, exactly as before.
