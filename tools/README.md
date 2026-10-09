# Analysis tools

Offline analysis of measurement runs. Nothing here is on a hot path or in
the deployment; it reads `runs/<run_id>/` and the logging service.

## ran_join.py — per-frame latency against RAN state

```bash
python3 tools/ran_join.py runs/<run_id>                    # summary, JSON tap
python3 tools/ran_join.py runs/<run_id> --source kpm       # the E2 xApp's rows
python3 tools/ran_join.py runs/<run_id> -o joined.csv      # one row per frame
```

For each frame in `edge-0/samples.csv`, it takes the latest RAN sample per
metric at or before the frame's `capture_ns` (an as-of join). Samples older
than twice the report period are left blank. It works for the frame's UE and
its cell.

- **Input:** it reads `kpi.csv`, or re-normalises `reports.jsonl` for runs
  recorded before `kpi.csv` existed.
- **Which UE:** the only one, or the uplink-heavy one (the LiDAR), or
  `--ue`.
- **The summary's r** is descriptive. On `gnb-sim`, whose RAN model has no
  coupling to the network at all, r(cqi, network) still came out -0.69 over
  45 s. Compare runs; do not read one. Tested in `ran/py/tests`.

## Still to come

- Notebooks plotting latency vs. payload size, rate, and impairment.
- Full-run percentile computation (the live snapshots use a sliding window;
  the CSV is the source of truth for whole-run statistics — see
  [ADR-0004](../docs/architecture/adr/0004-exact-percentiles.md)).

Reading a run:

```python
import pandas as pd, json, pathlib
run = pathlib.Path("runs/<run_id>")
meta = json.loads((run / "run.json").read_text())
edge = pd.read_csv(run / "edge" / "samples.csv")
edge["network_ms"] = edge["network_ns"] / 1e6
```

`run.json` carries the workload, impairment, transport, and the git SHAs
that produced the data — always report it alongside any figure.
