# Services

First-party services that mec-cast depends on at runtime but that release
independently.

| Service | Location | Status |
|---|---|---|
| Logging service | `logging/` (submodule) | Wired |

## `logging/`

Submodule of
[mec-cast-logging-service](https://github.com/morosev/mec-cast-logging-service):
FastAPI + PostgreSQL, ingesting aggregated latency snapshots over HTTP and
serving them back filtered by `trace_id`, service, level, and JSONB
`context` containment.

```bash
git submodule update --init services/logging
make up-logging
curl -sf http://localhost:8000/health/ready
```

Its `LogEntryCreate` schema is `extra="forbid"`, which makes it a **versioned
contract** with the telemetry crate's HTTP sink — see
[docs/operations/logging-submodule.md](../docs/operations/logging-submodule.md).

Distinct from `third_party/`: those are forks of external libraries; this is
first-party code that simply ships on its own cadence.
