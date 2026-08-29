# Admin manual

Operating a deployment that already exists: what `up` and `down` actually do,
reading logs, starting and stopping one service, reaching the database, backup,
and the routine tasks that keep a testbed healthy.

Getting the software onto machines is [deploy-manual.md](deploy-manual.md).
The admin **service** — its protocol, run states, findings and the meaning of
each `WF_*` code — is [admin-service.md](admin-service.md); this page is the
procedures, not the reference.

- [Cheat sheet](#cheat-sheet)
- [What up and down actually do](#what-up-and-down-actually-do)
- [Starting and stopping one service](#starting-and-stopping-one-service)
- [Reading logs](#reading-logs)
- [Getting inside a container](#getting-inside-a-container)
- [Running with the admin service](#running-with-the-admin-service)
- [Accessing the database](#accessing-the-database)
- [Backup and restore](#backup-and-restore)
- [Retention and disk](#retention-and-disk)
- [Checking system status](#checking-system-status)
- [Restarting and recovering](#restarting-and-recovering)
- [Troubleshooting](#troubleshooting)

## Cheat sheet

`compose` below is the project's file set. Locally that is two files; define it
once as a shell **function**, never a variable — a variable holding a string of
arguments only word-splits in bash, and zsh (the macOS default) does not:

```bash
compose() { docker compose -f deploy/compose/logging.yml -f deploy/compose/local.yml "$@"; }
```

In the lab substitute `docker compose -f deploy/lab/compose.<role>.yml`.

| Want | Command |
|---|---|
| What is this host running? | `make version` |
| Everything up, background | `compose up -d` |
| Everything down, keep data | `make down` |
| Everything down, **delete the database** | `make down-hard` |
| One service, foreground, its logs | `compose up --no-deps <svc>` |
| Follow all logs | `make logs` |
| Follow one service | `compose logs -f <svc>` |
| Last 50 lines | `compose logs --tail 50 <svc>` |
| Restart one service | `compose restart <svc>` |
| Rebuild one after a code change | `compose up -d --build <svc>` |
| Shell inside a container | `docker exec -it compose-edge-1 bash` |
| One-shot command in a container | `docker exec compose-edge-1 ls -la /runs` |
| psql | `docker exec -it compose-postgres-1 psql -U postgres -d mec_cast_logs` |
| Is the backend healthy? | `curl -s localhost:8000/health/ready` |
| Is data arriving? | `wc -l runs/$RUN_ID/*/samples.csv` |
| Resource use per container | `docker stats --no-stream` |
| Service name vs container name | `docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'` |

**Service name vs container name** trips everyone once: compose commands take
the *service* (`postgres`), raw `docker` commands take the *container*
(`compose-postgres-1` — project, service, replica).

## What up and down actually do

| Command | Effect |
|---|---|
| `compose up -d` | create and start every service, background |
| `compose up --no-deps <svc>` | one service only, foreground, streaming its logs |
| `compose stop <svc>` | SIGTERM, container kept |
| `compose start <svc>` | start a stopped container |
| `compose restart <svc>` | stop + start, **same** container and same environment |
| `compose down` | stop and remove containers and the network |
| `compose down -v` | …**and delete volumes, which is the database** |

Three consequences worth knowing before you need them.

**`down` keeps your data; `down-hard` does not.** `make down` removes
containers and leaves the `pgdata` volume alone, so every aggregated snapshot
survives. `make down-hard` deletes it — every logged snapshot of every run.
Per-frame CSVs live in a bind mount under `runs/` and survive either way, so a
`down -v` loses exactly half the picture, silently. Accumulation is harmless by
design: every query is scoped by `trace_id = RUN_ID`.

**`make down` is enough whichever `up-*` you ran.** It passes
`--remove-orphans`, which sweeps containers belonging to the project but not
named in its files — so it tears down the admin and the renderer too. There is
deliberately no `down-admin` or `down-render`.

**`restart` does not pick up environment or code changes.** A running container
keeps the environment it was *created* with. To change either:

```bash
compose up -d --force-recreate --no-deps edge
```

For a code change, rebuild the shared image first — the client and edge both
use it:

```bash
make build-ros2 && compose up -d edge lidar-client
```

## Starting and stopping one service

```bash
compose up --no-deps postgres
```

`--no-deps` is what makes this "one service": without it compose helpfully
starts the dependencies too.

**`Ctrl-C` in an attached `up` stops the container**, including one that was
already running before you attached. There is no detach-without-stopping for
`up`. When you want the container left alone, read its logs instead:

```bash
compose logs -f postgres
```

Graceful stop, with room for the recorders to flush:

```bash
compose stop -t 15 lidar-client edge
```

## Reading logs

Three distinct streams, answering different questions.

### 1. Container stdout — "is it alive, and what is it doing?"

```bash
compose logs -f edge
```

```bash
compose logs --tail 50 edge
```

```bash
compose logs -f --timestamps edge lidar-client
```

All services at once, colour-coded — this is what `make logs` runs, and it
includes the admin and renderer when they are up:

```bash
make logs
```

Raw docker equivalent, by container name:

```bash
docker logs -f --tail 100 compose-edge-1
```

`--tail`/`-f` read the whole JSON log file by default, so prefer `--tail` on a
container that has been up for hours.

### 2. Per-frame CSV — "what did it actually measure?"

Written to the host, so no container needed:

```bash
ls -la runs/$RUN_ID/*/
```

```bash
head -3 runs/$RUN_ID/edge-0/samples.csv
```

Directory leaves are instance-suffixed (`pub-0`, `pub-1`, `edge-0`,
`render-0`), one per node instance. Watch them grow — the fastest confirmation
that data is flowing:

```bash
while :; do clear; wc -l runs/$RUN_ID/*/samples.csv; sleep 2; done
```

On Linux, `watch -n 2 "wc -l runs/$RUN_ID/*/samples.csv"` is the shorthand.

Median network delay in ms, without loading pandas:

```bash
awk -F, 'NR>1 && $11 != "" {print $11}' runs/$RUN_ID/edge-0/samples.csv | sort -n | awk '{a[NR]=$1} END {print a[int(NR/2)]/1e6 " ms"}'
```

### 3. Logging service — "what happened across all components?"

The only view that spans hosts. Query by `trace_id`, service, level or free
text — see [Accessing the database](#accessing-the-database).

### Legacy WebRTC client (Profile B)

Not containerised; it writes files directly:

```bash
tail -f clients/webrtc_native/log/client.log
```

## Getting inside a container

```bash
docker exec -it compose-edge-1 bash
```

Inside the ROS image, source the workspace before ROS commands work — the
entrypoint does that for the main process, not for your shell:

```bash
source /opt/ros/jazzy/setup.bash && source /ws/install/setup.bash
```

Then the usual introspection:

```bash
ros2 topic list && ros2 topic hz /mec_cast/cloud
```

One-shot, without a shell:

```bash
docker exec compose-edge-1 ls -la /runs
```

## Running with the admin service

Nodes subscribe to the admin on startup and take run commands from a web page,
instead of reading `RUN_ID` from the environment. Neither replaces the other:
with no `ADMIN_URL` a node behaves exactly as it always has.

Locally:

```bash
make up-admin
```

Then open `http://localhost:8099/admin`. Press **Add run**, then **Start**.
`RUN_ID` is ignored — the admin mints one.

In the lab the admin runs in the `edge` role and the other roles dial it at
`ws://${EDGE_HOST}:8099/ws/node`.

**When the page shows no nodes**, the usual cause is nodes created without
`ADMIN_URL`, which took the standalone path and never dialled in. Check what a
node actually got:

```bash
docker exec compose-edge-1 printenv | grep ADMIN
```

No output means standalone. Recreate it — `restart` will not do, since the
environment is fixed at creation:

```bash
compose up -d --force-recreate --no-deps edge lidar-client
```

The run table, the state machine, every `WF_*` finding and its remedy, the
declared topology and the protocol are documented in
[admin-service.md](admin-service.md).

## Accessing the database

**PostgreSQL is deliberately not published to the host.** The logging service
has no authentication and the database none beyond its password, so the default
topology keeps it reachable only from inside the compose network. That is why
pgAdmin cannot see it out of the box.

### Option A — psql inside the container (no setup)

```bash
docker exec -it compose-postgres-1 psql -U postgres -d mec_cast_logs
```

Works immediately, needs nothing installed, cannot be reached from outside.
This is the right default.

A single query without an interactive session:

```bash
docker exec compose-postgres-1 psql -U postgres -d mec_cast_logs -c "select count(*) from log_entries;"
```

### Option B — publish the port for pgAdmin / DBeaver

The overlay publishes **5433**, so it cannot collide with a PostgreSQL already
on the host:

```bash
docker compose -f deploy/compose/logging.yml -f deploy/compose/expose-db.yml up --no-deps postgres
```

| Field | Value |
|---|---|
| Host | `localhost` |
| Port | `5433` |
| Maintenance database | `mec_cast_logs` |
| Username | `postgres` |
| Password | `postgres` |

In pgAdmin: *Object* → *Register* → *Server*, then the *Connection* tab.
Tables are under `mec_cast_logs → Schemas → public → Tables` — `log_entries`
and `schema_migrations`.

Connecting from a **Windows** pgAdmin to a WSL container works via `localhost`
on modern WSL2. If it does not, use the WSL IP:

```bash
ip -4 addr show eth0 | awk '/inet /{print $2}' | cut -d/ -f1
```

### Option C — SSH tunnel (the lab)

Do **not** publish the database on a lab network. Tunnel instead, from your
workstation:

```bash
ssh -L 5433:localhost:5433 ops@infra-host
```

With the lab overlay active on the infra host (it binds loopback by default):

```bash
docker compose -f deploy/lab/compose.infra.yml -f deploy/lab/expose-db.yml up -d
```

Then point pgAdmin at `localhost:5433`. The tunnel means the port is never
exposed to the lab LAN, which matters because these records include everything
the platform measured.

### Queries worth knowing

Snapshots for one run, newest first:

```sql
select timestamp, service, context->'metrics'->'network'->>'p50_ns' as net_p50
from log_entries where trace_id = '<RUN_ID>' order by timestamp desc limit 20;
```

Which services reported, and how much — instance-suffixed since one host can
run several (`mec-cast-pub-0`, `mec-cast-pub-1`):

```sql
select service, count(*), min(timestamp), max(timestamp)
from log_entries where trace_id = '<RUN_ID>' group by service;
```

Anything that went wrong:

```sql
select timestamp, service, message from log_entries
where severity >= 40 order by timestamp desc limit 50;
```

Dropped samples — a run with drops under-represents its own tail:

```sql
select service, max((context->'drops'->>'samples_total')::bigint) as dropped
from log_entries where trace_id = '<RUN_ID>' group by service;
```

The same over HTTP, no SQL client needed:

```bash
curl -sG http://localhost:8000/api/v1/logs --data-urlencode "trace_id=$RUN_ID" | python3 -m json.tool
```

Interactive API docs are at `http://localhost:8000/docs`.

## Backup and restore

Dump the whole database:

```bash
docker exec compose-postgres-1 pg_dump -U postgres -Fc mec_cast_logs > backup-$(date +%F).dump
```

Restore into a running instance:

```bash
docker exec -i compose-postgres-1 pg_restore -U postgres -d mec_cast_logs --clean < backup-2026-08-14.dump
```

Per-frame CSV is already on the host under `runs/` — back that up with ordinary
file tooling. It is the source of truth for whole-run statistics; the snapshots
in PostgreSQL are windowed summaries.

Take a dump before anything that could destroy the volume: `make down-hard`,
`docker compose down -v`, or `docker system prune --volumes`.

## Retention and disk

Nothing is deleted automatically. Bulk deletion is deliberately a CLI command
rather than an HTTP endpoint, because the API has no authentication.

Preview first:

```bash
docker exec compose-logging-1 mec-cast-logs purge --days 30 --dry-run
```

Then for real:

```bash
docker exec compose-logging-1 mec-cast-logs purge --days 30
```

In the lab, run it from cron or a systemd timer on the infra host at whatever
cadence your volume needs.

Runs are large — 10 Hz for 10 minutes is ~6000 rows per site, and images plus
volumes add up faster than the data does:

```bash
du -sh runs/* | sort -h | tail
```

```bash
docker system df
```

```bash
docker system prune
```

Add `-a` to also remove images not used by any container — that deletes
`mec-cast-ros`, so the next start rebuilds ~1.4 GB. **Never use `--volumes` on
the lab infra host**: that is the measurement database.

## Checking system status

Start with what this host is running:

```bash
make version
```

Then a quick pass, top to bottom:

```bash
compose ps
```

```bash
curl -s http://localhost:8000/health/ready
```

`{"status":"ok",...,"database":"up"}` means the service is up **and** can reach
PostgreSQL. `/health` alone does not touch the database — use `/health/ready`
when you care whether the whole backend works.

```bash
docker exec compose-postgres-1 pg_isready -U postgres -d mec_cast_logs
```

Is data actually arriving?

```bash
wc -l runs/$RUN_ID/*/samples.csv
```

```bash
curl -sG http://localhost:8000/api/v1/stats | python3 -m json.tool
```

Is anything being dropped?

```bash
compose logs edge | grep -i drop
```

## Restarting and recovering

**Restart one component, keep the rest running:**

```bash
compose restart edge
```

The recorder starts a fresh CSV section but keeps the same `RUN_ID`, so the run
stays joinable. Sequence numbers show a gap — that is honest, and visible in
analysis.

**The logging service is down and components are running.** Nothing is lost
immediately: the recorder buffers snapshots and drops the oldest when the buffer
fills, counting every drop. Per-frame CSV is unaffected — it never goes through
HTTP. Bring the service back and snapshots resume:

```bash
compose up -d postgres logging
```

**PostgreSQL is unhealthy.** Check its own logs first — a corrupt or
version-mismatched volume is the usual cause:

```bash
compose logs --tail 50 postgres
```

Last resort in dev, destroying all stored logs:

```bash
make down-hard && compose up -d postgres logging
```

Never do that in the lab without a dump.

**Everything is wedged and you want a clean slate (dev):**

```bash
make down-hard && make build-ros2 && make up-local
```

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `password authentication failed for user "postgres"` | Talking to a *host* PostgreSQL, not the container | `ss -ltn \| grep 5432` (Linux) or `lsof -iTCP:5432 -sTCP:LISTEN` (macOS); use `docker exec … psql` |
| No `samples.csv` appears | Producer and consumer disagree on `RUN_ID` | `compose exec edge printenv RUN_ID` in each |
| Edge sees no clouds | Router not up, or client started first | `compose logs zenoh-router`; restart `edge` then `lidar-client` |
| Snapshots missing, CSV fine | Logging service unreachable | `curl -s localhost:8000/health/ready` |
| `422` from the logging service | Extra top-level field; schema is `extra="forbid"` | [logging-submodule.md](logging-submodule.md) |
| Nonzero drop counters | Consumer slower than producer | `docker stats`; lower `RATE_HZ` or `NUM_POINTS` |
| `ptp.reliable: false` on a lab run | phc2sys not disciplining | `bash deploy/lab/ptp/verify-ptp.sh` |
| Port 8000 already allocated | Previous stack still up | `make down`; `docker ps -a` |
| Code change has no effect | Image not rebuilt | `make build-ros2 && compose up -d --build` |
| Admin page shows no nodes | Nodes created without `ADMIN_URL` | `docker exec compose-edge-1 printenv \| grep ADMIN`, then `--force-recreate` |
| Renderer logs in bursts with big `seq` gaps | Uplink in congestion collapse — not a renderer fault | Compare `pub-0` to `edge-0` row counts. See [local-development.md](../guides/local-development.md#when-the-renderer-looks-broken-but-is-not) |
| Router floods `Route data with unknown scope N!` | rmw_zenoh discovery declarations lost on the impaired link | Check delivery, not the log — data is unaffected. Lower `NETEM_LOSS` |

## See also

- [admin-service.md](admin-service.md) — the control plane's protocol, states and findings
- [deploy-manual.md](deploy-manual.md) — getting software onto machines and updating it
- [local-development.md](../guides/local-development.md) — running components by hand
- [running-an-experiment.md](../guides/running-an-experiment.md) — the measurement workflow
- [logging-submodule.md](logging-submodule.md) — the schema contract and security posture
