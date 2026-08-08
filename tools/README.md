# Analysis tools

Offline analysis of measurement runs. Nothing here is on a hot path or in
the deployment; it reads `runs/<run_id>/` and the logging service.

Suggested contents as the campaign grows:

- Notebooks plotting latency vs. payload size, rate, and impairment.
- Scripts joining per-frame CSV with RAN KPIs on `trace_id` + timestamp.
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
