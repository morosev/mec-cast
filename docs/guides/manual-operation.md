# Manual operation and maintenance

Running every component by hand — one container per terminal — plus database
access, log access, and the routine admin tasks. Applies to both the **dev**
box and the **lab** testbed; the differences are called out per section.

`make up-local` does all of this in one command. Read this guide when you
need to know *what it is doing*, when one component misbehaves and you want
it in the foreground, or when you are operating the lab and there is no
Makefile target for "the UE host only".

- [Before you start](#before-you-start)
- [Four concepts first](#four-concepts-first)
- [Dev: one container per terminal](#dev-one-container-per-terminal)
- [Keeping six terminals in one window (tmux)](#keeping-six-terminals-in-one-window-tmux)
- [Lab: one role per host](#lab-one-role-per-host)
- [Docker and compose command vocabulary](#docker-and-compose-command-vocabulary)
- [Accessing the database](#accessing-the-database)
- [Reading logs and component output](#reading-logs-and-component-output)
- [Restarting and recovering](#restarting-and-recovering)
- [Checking system status](#checking-system-status)
- [Routine admin tasks](#routine-admin-tasks)
- [Troubleshooting](#troubleshooting)

## Before you start

One-time, on any machine that will run components:

```bash
bash scripts/bootstrap-dev.sh
```

Verify the pieces this guide assumes:

```bash
docker compose version && docker ps >/dev/null && echo "docker OK"
```

Two tools are genuinely optional but referenced below. Neither is installed
by default on Ubuntu:

```bash
sudo apt install -y jq uuid-runtime
```

On macOS `uuidgen` is already present; only `jq` is missing:

```bash
brew install jq
```

`jq` only pretty-prints JSON — `python3 -m json.tool` is shown as the
alternative everywhere. `uuid-runtime` provides `uuidgen`; where it is
absent the scripts fall back to `/proc/sys/kernel/random/uuid`, which exists
on Linux only — hence the `uuidgen ||` ordering, which is what keeps the
snippets working on macOS.

The ROS image is large (~1.4 GB) and is built once:

```bash
make build-ros2
```

### macOS

The pipeline itself runs in containers and behaves identically, but the host
side differs in two ways worth knowing before you start:

- **Your shell is zsh, not bash.** zsh does not word-split an unquoted
  `$VAR` into separate arguments, so the `$COMPOSE` idiom used throughout
  this guide fails with `no such file or directory: docker compose -f …`
  unless `.run-env` opts into it. [Concept 2](#four-concepts-first) below
  has the one line that fixes it.
- **Docker is a VM.** Docker Desktop (or colima) must be running before any
  `$COMPOSE` command; there is no daemon to add yourself to a group for.

## Four concepts first

Everything below makes sense once these are clear.

**1. `RUN_ID` is the join key.** It becomes the `trace_id` on every log
record and the directory name under `runs/`. Every component in one
experiment **must** share it. Start components with different `RUN_ID`s and
nothing is technically broken — the data simply cannot be correlated, which
you will not discover until analysis. Because each terminal is a separate
shell, put it in a file and source it everywhere:

```bash
echo "export RUN_ID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)" > .run-env
```

Then in **every** terminal, first thing:

```bash
cd ~/mec-cast && source .run-env && echo "RUN_ID=$RUN_ID"
```

`.run-env` is gitignored. Defaults to `dev-run` if you skip this, which is
fine for poking at the system but useless for a measurement campaign.

**2. Two compose files, always passed together.** `logging.yml` has the
backend, `local.yml` has the pipeline. They must be one compose project so
the containers share a network and `http://logging:8000` resolves:

```bash
export COMPOSE="docker compose -f deploy/compose/logging.yml -f deploy/compose/local.yml"
```

Put that in `.run-env` too. Every command below uses `$COMPOSE`.

Because `$COMPOSE` is a *string of arguments*, it only works unquoted in a
shell that word-splits — bash does, zsh does not. On macOS, where zsh is the
default, add one more line to `.run-env` so the rest of this guide works
verbatim:

```bash
cat >> .run-env <<'EOF'
# zsh does not word-split an unquoted $VAR the way bash does, which would
# make every `$COMPOSE ...` below try to exec one long filename.
[ -n "$ZSH_VERSION" ] && setopt SH_WORD_SPLIT
EOF
```

That option applies to the whole shell session, not just `$COMPOSE` — fine
in the dedicated terminals this guide asks you to open, worth knowing if you
reuse them for other work. Without it, prefix each invocation with zsh's
explicit split flag instead: `${=COMPOSE} up --no-deps postgres`.

**3. Service name vs container name.** Compose commands take the *service*
name (`postgres`); raw `docker` commands take the *container* name
(`compose-postgres-1` — project, service, replica). List both:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

**4. Where data lands.** Per-frame CSV is written inside the container to
`/runs`, which is bind-mounted to `runs/` in the repo — so it survives
`docker compose down` and is readable directly from the host. Aggregated
snapshots go over HTTP to the logging service and live in PostgreSQL, which
is **not** persisted in the dev topology (no volume) but **is** in the lab
(`pgdata`). Dev database contents are therefore lost on `down -v`.

## Dev: one container per terminal

Six components. Start them in this order — each command runs in the
foreground, printing that component's logs, and stops with `Ctrl-C`.

`--no-deps` is what makes this "one container per terminal": without it,
compose helpfully starts the dependencies too, and you lose the isolation
you opened six terminals to get.

Every terminal starts with:

```bash
cd ~/mec-cast && source .run-env
```

### Terminal 1 — PostgreSQL

```bash
$COMPOSE up --no-deps postgres
```

Wait for `database system is ready to accept connections`.

### Terminal 2 — logging service

```bash
$COMPOSE up --no-deps --build logging
```

Applies migrations at startup (`MECLOG_AUTO_MIGRATE=true`) — you never run
`mec-cast-logs migrate` by hand here. Wait for the uvicorn startup line, then
confirm from any terminal:

```bash
curl -s http://localhost:8000/health/ready
```

### Terminal 3 — Zenoh router

```bash
$COMPOSE up --no-deps --build zenoh-router
```

The rendezvous point. Both the client and the edge dial into it; nothing
discovers anything by multicast (that is the whole reason Zenoh was chosen —
see [ADR-0001](../architecture/adr/0001-zenoh-over-dds.md)).

### Terminal 4 — edge ingest node

```bash
$COMPOSE up --no-deps edge
```

Start the **consumer before the producer** so the first clouds are not
published into the void. It stamps arrival, computes latency, writes
`runs/$RUN_ID/edge/samples.csv`, and posts snapshots every 2 s.

### Terminal 5 — LiDAR client (the producer)

```bash
$COMPOSE up --no-deps lidar-client
```

Override the workload without editing anything:

```bash
NUM_POINTS=60000 RATE_HZ=5.0 $COMPOSE up --no-deps lidar-client
```

### Terminal 6 — netem impairment (optional)

```bash
$COMPOSE up --no-deps netem
```

Shares the client's network namespace and impairs its egress, modelling the
5G uplink. Skip this terminal to measure the unimpaired floor. Change the
impairment:

```bash
NETEM_DELAY=50ms NETEM_JITTER=10ms NETEM_LOSS=1% $COMPOSE up --no-deps netem
```

It applies `tc` once and then sleeps, so restarting *it* alone is how you
change impairment mid-run — the qdisc uses `replace`, so it is idempotent.

### Stopping

`Ctrl-C` in each terminal, in reverse order (producer first, so recorders
drain and flush). Then, from any terminal:

```bash
$COMPOSE down
```

Add `-v` to also delete the database volume and start clean next time.

## Keeping six terminals in one window (tmux)

`tmux`, `screen`, and `byobu` are all installed. tmux is the one worth
learning.

```bash
tmux new -s meccast
```

| Keys | Action |
|---|---|
| `Ctrl-b` `"` | split horizontally |
| `Ctrl-b` `%` | split vertically |
| `Ctrl-b` `o` | cycle panes |
| `Ctrl-b` arrow | move by direction |
| `Ctrl-b` `z` | zoom pane fullscreen (toggle) |
| `Ctrl-b` `c` | new window |
| `Ctrl-b` `0`…`9` | switch window |
| `Ctrl-b` `[` | scroll mode (`q` exits) |
| `Ctrl-b` `d` | detach, leaving everything running |

Reattach later — including after closing the terminal entirely:

```bash
tmux attach -t meccast
```

This matters in the lab: an SSH drop kills plain foreground containers, but
a detached tmux session keeps running. Start long experiments inside tmux.

To lay out all six panes at once:

```bash
tmux new -s meccast \; split-window -h \; split-window -v \; select-pane -t 0 \; split-window -v \; select-layout tiled
```

## Lab: one role per host

Four hosts, four roles. Same containers, different composition, and real
5G instead of netem.

| Role | Host | Components | Required env |
|---|---|---|---|
| `infra` | services host | postgres + logging | — |
| `edge` | MEC app server | zenoh-router + edge | `RUN_ID`, `LOGGING_HOST` |
| `ue` | robot compute | lidar-client | `RUN_ID`, `EDGE_HOST`, `LOGGING_HOST` |
| `gnb` | srsRAN host | ran-collector | `RUN_ID`, `LOGGING_HOST` |

**Order matters: `infra` → `edge` → `gnb` → `ue`.** Everything posts to the
logging service, and the UE dials the edge's router. Start the UE last or it
will retry against a router that is not there yet.

The scripted path, run from your workstation, per host:

```bash
bash deploy/lab/deploy.sh infra ops@infra-host
```

That rsyncs the repo (excluding `third_party/`, `runs/`, `target/`), builds,
runs `docker compose up -d`, and verifies PTP.

### Doing it manually on each host

Same thing without the script — this is what you want when debugging. SSH to
the host, then:

```bash
cd ~/mec-cast && export RUN_ID=<the-one-run-id-for-all-hosts>
```

**infra host:**

```bash
docker compose -f deploy/lab/compose.infra.yml up -d --build
```

**edge host** (`LOGGING_HOST` is the infra host's management-LAN address):

```bash
LOGGING_HOST=10.0.0.10 docker compose -f deploy/lab/compose.edge.yml up -d --build
```

**gNB host** — also point srsRAN at it in `gnb.yml` under `metrics:` with
`addr: <gnb-host>` and `port: 55555`:

```bash
LOGGING_HOST=10.0.0.10 docker compose -f deploy/lab/compose.gnb.yml up -d --build
```

**UE host** (`EDGE_HOST` must be reachable across the UPF):

```bash
LOGGING_HOST=10.0.0.10 EDGE_HOST=10.0.0.20 docker compose -f deploy/lab/compose.ue.yml up -d --build
```

To run one of these in the foreground for debugging, drop `-d` and add the
service name, exactly as in the dev section.

### PTP — check before trusting any cross-host number

Lab roles mount `/dev/ptp0`. Cross-host latency is only meaningful if the
clocks are disciplined:

```bash
bash deploy/lab/ptp/verify-ptp.sh
```

Run it on **every** measuring host. See
[ADR-0003](../architecture/adr/0003-ptp-on-management-lan.md) for why PTP
runs on the management LAN and never the 5G user plane. Every snapshot
records `context.ptp.reliable`, so you can filter after the fact — but
noticing during the run is cheaper.

## Docker and compose command vocabulary

The commands you will actually use, with what they mean. `$COMPOSE` is the
two-file invocation from above; in the lab substitute
`docker compose -f deploy/lab/compose.<role>.yml`.

### Seeing what exists

```bash
$COMPOSE ps
```

Compose's view: service name, state, ports. Add `-a` to include stopped.

```bash
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Docker's view: every container on the host, including things compose did not
start.

```bash
docker stats --no-stream
```

Live CPU/memory/network per container. Drop `--no-stream` for a continuously
updating view — useful when the edge node starts dropping samples and you
want to know whether it is CPU-bound.

### Starting and stopping

| Command | Effect |
|---|---|
| `$COMPOSE up -d` | all services, background |
| `$COMPOSE up --no-deps <svc>` | one service, foreground, its logs |
| `$COMPOSE stop <svc>` | SIGTERM, container kept |
| `$COMPOSE start <svc>` | start a stopped container |
| `$COMPOSE restart <svc>` | stop + start, same container |
| `$COMPOSE down` | stop and remove containers + network |
| `$COMPOSE down -v` | …and delete volumes (**database contents**) |

`stop` is graceful — 10 s by default. The recorders flush on shutdown, so
give them room when a run has data worth keeping:

```bash
$COMPOSE stop -t 15 lidar-client edge
```

### Getting inside a container

```bash
docker exec -it compose-edge-1 bash
```

An interactive shell. Inside the ROS image, source the workspace before ROS
commands work — the entrypoint does this for the main process, not for your
shell:

```bash
source /opt/ros/jazzy/setup.bash && source /ws/install/setup.bash
```

Then the usual introspection:

```bash
ros2 topic list && ros2 topic hz /point_cloud
```

One-shot commands without a shell:

```bash
docker exec compose-edge-1 ls -la /runs
```

### Rebuilding after a code change

Compose does not rebuild automatically:

```bash
$COMPOSE up -d --build edge
```

For changes to the telemetry crate or the ROS packages, rebuild the shared
image first — both the client and edge use it:

```bash
make build-ros2 && $COMPOSE up -d edge lidar-client
```

## Accessing the database

**PostgreSQL is deliberately not published to the host.** The logging service
has no authentication, and neither has the database beyond its password, so
the default topology keeps it reachable only from inside the compose network.
That is why pgAdmin cannot see it out of the box.

### Option A — psql inside the container (no setup)

```bash
docker exec -it compose-postgres-1 psql -U postgres -d mec_cast_logs
```

Works immediately, needs nothing installed, and cannot be reached from
outside. This is the right default.

A single query without an interactive session:

```bash
docker exec compose-postgres-1 psql -U postgres -d mec_cast_logs -c "select count(*) from log_entries;"
```

### Option B — publish the port for pgAdmin / DBeaver

Add the overlay, which publishes **5433** so it cannot collide with a
PostgreSQL already on the host:

```bash
docker compose -f deploy/compose/logging.yml -f deploy/compose/expose-db.yml up --no-deps postgres
```

pgAdmin connection settings:

| Field | Value |
|---|---|
| Host | `localhost` |
| Port | `5433` |
| Maintenance database | `mec_cast_logs` |
| Username | `postgres` |
| Password | `postgres` |

In pgAdmin: *Object* → *Register* → *Server*, name it anything, then the
*Connection* tab for the values above. Tables are under
`mec_cast_logs → Schemas → public → Tables` — `log_entries` and
`schema_migrations`.

Connecting from a **Windows** pgAdmin to a WSL container works via
`localhost` on modern WSL2. If it does not, use the WSL IP:

```bash
ip -4 addr show eth0 | awk '/inet /{print $2}' | cut -d/ -f1
```

### Option C — SSH tunnel (preferred for the lab)

Do **not** publish the database on a lab network. Tunnel instead, from your
workstation:

```bash
ssh -L 5433:localhost:5433 ops@infra-host
```

With the lab overlay active on the infra host (it binds loopback by default):

```bash
docker compose -f deploy/lab/compose.infra.yml -f deploy/lab/expose-db.yml up -d
```

Then point pgAdmin at `localhost:5433` as above. The tunnel means the port is
never exposed to the lab LAN, which matters because these records include
everything the platform measured.

### Queries worth knowing

Snapshots for one run, newest first:

```sql
select timestamp, service, context->'metrics'->'network'->>'p50_ns' as net_p50
from log_entries where trace_id = '<RUN_ID>' order by timestamp desc limit 20;
```

Which services reported, and how much:

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

Same thing over HTTP, no SQL client needed:

```bash
curl -sG http://localhost:8000/api/v1/logs --data-urlencode "trace_id=$RUN_ID" | python3 -m json.tool
```

Interactive API docs are at `http://localhost:8000/docs`.

## Reading logs and component output

There are three distinct output streams and they answer different questions.

### 1. Container stdout — "is this component alive and what is it doing?"

```bash
$COMPOSE logs -f edge
```

`-f` follows. Useful variants:

```bash
$COMPOSE logs --tail 50 edge
```

```bash
$COMPOSE logs -f --timestamps edge lidar-client
```

All services at once, colour-coded by service:

```bash
$COMPOSE logs -f
```

Raw docker equivalent, by container name:

```bash
docker logs -f --tail 100 compose-edge-1
```

Since `--tail`/`-f` read the whole JSON log file by default, prefer `--tail`
on a container that has been up for hours.

### 2. Per-frame CSV — "what did it actually measure?"

Written to the host, so no container needed:

```bash
ls -la runs/$RUN_ID/*/
```

```bash
head -3 runs/$RUN_ID/edge/samples.csv
```

Watch it grow live — the fastest confirmation that data is flowing:

```bash
watch -n 2 "wc -l runs/$RUN_ID/*/samples.csv"
```

Median network delay in ms, without loading pandas:

```bash
awk -F, 'NR>1 && $11 != "" {print $11}' runs/$RUN_ID/edge/samples.csv | sort -n | awk '{a[NR]=$1} END {print a[int(NR/2)]/1e6 " ms"}'
```

Columns are `seq,modality,kind,site,capture_ns,send_ns,recv_ns,process_done_ns,payload_bytes,aux_ns,network_ns,e2e_ns,processing_ns,sender_ns`.

### 3. Logging service — "what happened across all components?"

The only view that spans hosts. Query by `trace_id`, service, level, or
free text — see [Accessing the database](#accessing-the-database).

### Legacy WebRTC client (Profile B)

Not containerised; it writes files directly:

```bash
tail -f clients/webrtc_native/log/client.log
```

```bash
tail -f clients/webrtc_native/log/alice_webrtc.log
```

## Restarting and recovering

**Restart one component, keep the rest running:**

```bash
$COMPOSE restart edge
```

The recorder starts a fresh CSV section but keeps the same `RUN_ID`, so the
run stays joinable. Sequence numbers will show a gap — that is honest, and
visible in analysis.

**Restart after a code change** (restart alone will not pick it up):

```bash
make build-ros2 && $COMPOSE up -d --build edge
```

**The logging service is down and components are running.** Nothing is lost
immediately: the recorder buffers snapshots and drops the oldest when the
buffer fills, counting every drop. Per-frame CSV is unaffected — it never
goes through HTTP. Bring the service back and snapshots resume:

```bash
$COMPOSE up -d postgres logging
```

**PostgreSQL is unhealthy.** Check its own logs first — a corrupt or
version-mismatched volume is the usual cause:

```bash
$COMPOSE logs --tail 50 postgres
```

Last resort in dev, destroying all stored logs:

```bash
$COMPOSE down -v && $COMPOSE up -d postgres logging
```

Never do that in the lab without a dump — see below.

**Everything is wedged and you want a clean slate (dev):**

```bash
$COMPOSE down -v --remove-orphans && make build-ros2 && make up-local
```

## Checking system status

A quick pass, top to bottom:

```bash
$COMPOSE ps
```

```bash
curl -s http://localhost:8000/health/ready
```

`{"status":"ok",...,"database":"up"}` means the service is up **and** can
reach PostgreSQL. `/health` alone does not touch the database — use
`/health/ready` when you care whether the whole backend works.

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
$COMPOSE logs edge | grep -i drop
```

## Routine admin tasks

### Retention — the database grows without bound

Nothing is deleted automatically. Bulk deletion is deliberately a CLI
command rather than an HTTP endpoint, because the API has no authentication.

Preview first:

```bash
docker exec compose-logging-1 mec-cast-logs purge --days 30 --dry-run
```

Then for real:

```bash
docker exec compose-logging-1 mec-cast-logs purge --days 30
```

In the lab, run it from cron or a systemd timer on the infra host at
whatever cadence your volume needs.

### Backup and restore

Dump the whole database:

```bash
docker exec compose-postgres-1 pg_dump -U postgres -Fc mec_cast_logs > backup-$(date +%F).dump
```

Restore into a running instance:

```bash
docker exec -i compose-postgres-1 pg_restore -U postgres -d mec_cast_logs --clean < backup-2026-08-14.dump
```

Per-frame CSV is already on the host under `runs/` — back that up with
ordinary file tooling. It is the source of truth for whole-run statistics;
the snapshots in PostgreSQL are windowed summaries.

### Disk

Runs are large: 10 Hz for 10 minutes is ~6000 rows per site, and images plus
volumes add up faster than the data does.

```bash
du -sh runs/* | sort -h | tail
```

```bash
docker system df
```

Reclaim space from stopped containers, unused networks, and dangling images:

```bash
docker system prune
```

Add `-a` to also remove images not used by any container — this deletes
`mec-cast-ros`, so the next start rebuilds ~1.4 GB. Never use `--volumes` on
the lab infra host: that is the measurement database.

### Rotating a run

Each experiment gets a fresh `RUN_ID`. The scripted path handles it and also
writes provenance:

```bash
bash scripts/run-experiment.sh -d 60 -n 30000 -t "baseline"
```

That writes `runs/<run_id>/run.json` with workload, impairment, and the git
SHAs of the repo and every submodule. Manual runs do not get that file —
if the numbers matter, use the script or write the metadata yourself.

### Keeping the lab hosts in sync

```bash
bash deploy/lab/deploy.sh edge ops@edge-host
```

Re-running it rsyncs, rebuilds, and restarts that role. Deploy `infra`
first after a change that touches the logging schema.

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `password authentication failed for user "postgres"` | Talking to a *host* PostgreSQL, not the container | `ss -ltn \| grep 5432`; use `docker exec … psql` |
| No `samples.csv` appears | Producer and consumer disagree on `RUN_ID` | `$COMPOSE exec edge printenv RUN_ID` in each |
| Edge sees no clouds | Router not up, or client started first | `$COMPOSE logs zenoh-router`; restart `edge` then `lidar-client` |
| Snapshots missing, CSV fine | Logging service unreachable | `curl -s localhost:8000/health/ready` |
| `422` from the logging service | Extra top-level field; schema is `extra="forbid"` | [logging-submodule.md](../operations/logging-submodule.md) |
| Nonzero drop counters | Consumer slower than producer | `docker stats`; lower `RATE_HZ` or `NUM_POINTS` |
| `ptp.reliable: false` on a lab run | phc2sys not disciplining | `bash deploy/lab/ptp/verify-ptp.sh` |
| Port 8000 already allocated | Previous stack still up | `$COMPOSE down`; `docker ps -a` |
| Code change has no effect | Image not rebuilt | `make build-ros2 && $COMPOSE up -d --build` |

## See also

- [running-an-experiment.md](running-an-experiment.md) — the measurement workflow
- [lab-topology.md](../operations/lab-topology.md) — hosts, addressing, PTP
- [timing-model.md](../architecture/timing-model.md) — what each metric means and when it is valid
- [logging-submodule.md](../operations/logging-submodule.md) — the schema contract and the service's security posture
