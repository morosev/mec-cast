# Services

First-party services that mec-cast depends on at runtime.

| Service | Location | Kind | Status |
|---|---|---|---|
| Logging service | `logging/` | submodule | Wired |
| Admin service | `admin/` | in-repo | Wired |

The two are kept differently on purpose. The logging service releases
independently: its `LogEntryCreate` schema is frozen and reusable by anything
that emits logs, so it earns its own repository. The admin service does not —
every protocol change touches the service and both node clients in the same
commit, and a submodule would make that atomic change impossible. See
[ADR-0007](../docs/architecture/adr/0007-websocket-control-plane.md).

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

## `admin/`

Run orchestration: a WebSocket control plane and an operator page on port 8099.
Nodes subscribe on startup, report status on every change, and take start and
stop commands. Runs are stored as `runs/<run_id>/run.json` plus a journal — no
database.

Two things it does beyond run lifecycle. The fleet can be **declared** in
a topology file (copy `deploy/lab/topology.example.yml` beside itself as
`topology.yml`) — optional, and when present the service validates
the live registry against it, reporting a node that connected but was not
declared, one that was declared and never arrived, and one whose cell
disagrees. And runs are **per cell**: each cell has at most one active run, so
two cells measure independently.

```bash
make up-admin
```

Then open `http://localhost:8099/admin`. Full operator guide:
[docs/operations/admin-service.md](../docs/operations/admin-service.md).

**No authentication.** Anyone who can reach the port can start and stop
experiments. Management LAN only.
