# mec-cast-admin

Run orchestration for mec-cast: a WebSocket control plane and an operator page.

A measurement run is otherwise a shell variable — `RUN_ID` exported into every
container by `scripts/run-experiment.sh`. This service owns run lifecycle
instead: nodes dial in on startup, report status on every change, and take
start and stop commands, while an operator page shows the run table and says
what to do when the workflow is not established.

See [ADR-0007](../../docs/architecture/adr/0007-websocket-control-plane.md) for
why this exists and [ADR-0008](../../docs/architecture/adr/0008-run-identity-and-store.md)
for how runs are identified and stored.

**The env-`RUN_ID` path is unchanged.** A node with no `admin_url` behaves
exactly as it always has. This service is additive.

## Requirements

- Python 3.11+
- A writable `runs/` directory, shared with the nodes on the same host

No database. Run state lives in `runs/<run_id>/run.json` plus an append-only
journal, so the page still works when PostgreSQL is down.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

Every setting is an environment variable prefixed with `MECADM_`. The only one
you are likely to set is `MECADM_RUNS_DIR`.

## Running

```bash
mec-cast-admin serve --port 8099
```

Then open `http://localhost:8099/admin`.

Always a single worker: the registry of connected nodes and the run state
machine are in-process, and a second worker would hold a second, divergent view
of the fleet.

## Security posture

**No authentication, no rate limiting** — the same posture as the logging
service, but a wider blast radius: anyone who can reach the port can start and
stop experiments. Bind it to the management LAN only, never to a UE-reachable
or public interface.

## Testing

```bash
pytest
```

No database and no containers. The state machine, protocol and diagnostics are
pure functions over plain dicts; the service tests drive `TestClient`.

`tests/vectors.json` is shared with the Rust node client
(`ran/collector/tests/admin_ws.rs`) so the two protocol implementations cannot
drift apart silently.
