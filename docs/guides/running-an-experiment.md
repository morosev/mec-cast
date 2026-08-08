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

## Reading results

Per-frame CSV columns: `seq, modality, kind, site, capture_ns, send_ns,
recv_ns, process_done_ns, payload_bytes, aux_ns` plus the derived
`network_ns, e2e_ns, processing_ns, sender_ns`.

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
