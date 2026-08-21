# Running an experiment

## Locally (no radio hardware)

```bash
make up-logging          # logging service + postgres, once
bash scripts/run-experiment.sh -d 60 -n 30000 -r 10 -l 20ms -t "baseline"
```

Options: `-d` duration s, `-n` points/cloud, `-r` rate Hz, `-l` netem delay,
`-j` jitter, `-L` loss, `-s` seed, `-t` free-text tag.

Each run produces `runs/<run_id>/`:

| Path | Contents |
|---|---|
| `run.json` | Full configuration + git SHAs of repo and submodules |
| `pub/samples.csv` | Per-frame samples, sender side |
| `edge/samples.csv` | Per-frame samples, receiver side |
| `ran/samples.csv` | RAN KPIs (lab runs only) |

`run.json` is the reproducibility artifact. It records the workload, the
impairment, the transport, and the exact code state — including whether the
tree was dirty. A number without a `run.json` beside it cannot be defended.

## Choosing an impairment the link can carry

Packet loss does not cost you a few frames in proportion to the loss rate. It
collapses throughput, and the frames go missing at the *publisher*, before the
network is even involved.

Zenoh runs over Reliable UDP — its own UDP link with Zenoh's transport-level
reliability above it, **not** QUIC and not encrypted
([router-config.json5](../../deploy/docker/zenoh/router-config.json5), and
[ADR-0006](../architecture/adr/0006-reliable-udp-transport.md) for why). A
dropped fragment is retransmitted rather than losing the whole frame, and
there is **no congestion control**, so loss does not throttle the sender the
way it does on TCP. What still sheds frames is volume: retransmissions add
bytes to a path that is already the constraint, the tx queue fills, and the
publisher's `KEEP_LAST(10)` drops the backlog before it ever reaches the wire.

The figures below were measured over TCP, whose self-limit follows the Mathis
bound:

```
bandwidth ≈ MSS / (RTT × √loss)     ≈ 1448 / (2·delay × √p)
```

At 25 ms delay and 0.4% loss that is about **0.44 MB/s**, against 3.4 MB/s
offered by a 30,000-point cloud at 10 Hz — roughly 8× over. **That formula
models TCP specifically and is not a property of the link**: nothing caps
bandwidth in the compose topology, and the same workload runs loss-free once
the impairment is removed. Treat it as an order-of-magnitude warning about
over-subscription, not as a capacity figure for the transport now in use.

Measured on the dev box, same code and same run each time:

| Points | netem | Edge frames | Loss | network p50 |
|---|---|---|---|---|
| 30,000 | none | 481 / 481 | 0% | — |
| 30,000 | 25 ms delay only | 479 / 479 | 0% | 27.6 ms |
| 30,000 | 25 ms + 0.4% loss | 47 / 2,329 | **98%** | 363 ms |
| 3,000 | 25 ms + 0.4% loss | 481 / 481 | 0% | 32.2 ms |

Delay alone is harmless — the third row is the only one that loses frames, and
the fourth shows the same impairment is fine once the workload fits. So when a
run comes back with a huge `missing` count and inflated `network_ns`, suspect
the offered load against the impaired capacity before suspecting the pipeline.

There is a second ceiling that has nothing to do with the network. Unimpaired,
delivered throughput tracks offered exactly up to **~17 MB/s** (150,000 points
at 10 Hz) and decouples above it — 200,000 and 1,000,000-point workloads both
land in the same ~17–19 MB/s band. That is the Python pipeline —
serialisation, `PointCloud2` construction, the edge's per-frame numpy pass —
not the link, and no transport or impairment setting moves it. Measure it on
each host before attributing a large-payload result to the network.

`run-experiment.sh` prints a warning when the workload exceeds this estimate.
It does not refuse: measuring an over-capacity link is a legitimate experiment,
as long as that is what you meant to do. Note the script's own defaults
(30,000 points, 10 Hz, 0.5% loss) are over capacity — good for observing
congestion collapse, useless for a glass-to-glass latency number.

## Reading results

Per-frame CSV columns: `seq, modality, kind, site, capture_ns, send_ns,
recv_ns, process_done_ns, payload_bytes, aux_ns` plus the derived
`network_ns, e2e_ns, processing_ns, sender_ns`.

The recorder **appends**, so restarting a component without changing `RUN_ID`
adds to the same file rather than replacing it — the earlier frames exist
nowhere else, since the logging service only ever receives aggregates. The
cost of keeping them is that one file may hold more than one incarnation, and
`seq` restarts at 0 each time a recorder starts. Sort by `capture_ns` rather
than `seq` when a file spans a restart, and treat a `seq` that goes backwards
as the boundary.

Aggregated snapshots (1–2 s cadence) go to the logging service, joined by
`trace_id = run_id`:

```bash
curl -sG http://localhost:8000/api/v1/logs \
  --data-urlencode "trace_id=$RUN_ID" | jq '.items[0].context.metrics'
```

Postgres keeps `context` as JSONB, so metrics are directly queryable:

```sql
SELECT timestamp,
       (context->'metrics'->'network'->>'p99_ns')::bigint / 1e6 AS p99_ms
FROM log_entries
WHERE trace_id = '<run_id>' AND service = 'mec-cast-edge'
ORDER BY timestamp;
```

## Before trusting cross-host numbers

```bash
bash deploy/lab/ptp/verify-ptp.sh     # on BOTH endpoints
```

Every snapshot also carries `context.ptp.reliable`. If it is `false`, the
one-way metrics (network, glass-to-glass, sender/receiver split) measure
clock offset as much as latency. Local-only metrics (encode, decode, jitter
buffer, processing) stay valid regardless. See
[ADR-0003](../architecture/adr/0003-ptp-on-management-lan.md).

## Parameter sweeps

The payload/rate knobs are the primary experimental variables:

```bash
for n in 5000 10000 30000 60000 120000; do
  bash scripts/run-experiment.sh -d 60 -n "$n" -t "sweep-points-$n"
done
```

Keep the seed fixed across a sweep so cloud contents are identical and only
the size varies.
