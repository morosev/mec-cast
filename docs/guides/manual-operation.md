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
- [Running with the admin service](#running-with-the-admin-service)
- [Keeping six terminals in one window (tmux)](#keeping-six-terminals-in-one-window-tmux)
- [Lab: one role per host](#lab-one-role-per-host)
- [Docker and compose command vocabulary](#docker-and-compose-command-vocabulary)
- [Accessing the database](#accessing-the-database)
- [Reading logs and component output](#reading-logs-and-component-output)
- [Restarting and recovering](#restarting-and-recovering)
- [Checking system status](#checking-system-status)
- [Routine admin tasks](#routine-admin-tasks)
- [Troubleshooting](#troubleshooting)
- [Opening the renderer](#opening-the-renderer)
- [When the renderer looks broken but is not](#when-the-renderer-looks-broken-but-is-not)

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

The ROS image is large (~2.4 GB, of which ~1 GB is the rerun viewer) and
is built once. Build with `--build-arg WITH_RERUN=0` on hosts that never
render — the edge, gNB and infra roles do not need it:

```bash
make build-ros2
```

### Linux and macOS

The pipeline runs in containers and behaves identically on both. Only the host
side differs, and every difference is called out inline where it matters. The
whole list:

| | Linux | macOS |
|---|---|---|
| Default shell | bash | **zsh** — which is why this guide defines `compose` as a function, not a `$COMPOSE` variable ([Concept 2](#four-concepts-first)) |
| Docker | daemon on the host | Docker Desktop or colima — **a VM that must be running first**; there is no group to add yourself to |
| `uuidgen` | `sudo apt install uuid-runtime` | built in |
| `jq` | `sudo apt install jq` | `brew install jq` |
| `watch` | built in | not present — use the `while` loop shown inline, or `brew install watch` |
| Listening sockets | `ss -ltn` | `lsof -iTCP -sTCP:LISTEN -n -P` |
| Open a URL | `xdg-open <url>` | `open <url>` |
| tmux | `sudo apt install tmux` | `brew install tmux` |

Everything else — every `compose` command, every `docker exec`, every SQL
query, every path under `runs/` — is identical on both.

One macOS gotcha that is not really a difference, just a trap: **start Docker
Desktop before the first command**, or everything fails with `Cannot connect
to the Docker daemon`. It is a VM and it takes a moment to come up.

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
the containers share a network and `http://logging:8000` resolves.

Define it as a shell **function**, and add it to `.run-env`:

```bash
cat >> .run-env <<'EOF'
compose() { docker compose -f deploy/compose/logging.yml -f deploy/compose/local.yml "$@"; }
EOF
```

Every command below calls `compose`. It works identically on Linux and macOS.

A function rather than `export COMPOSE="docker compose -f …"` because a
variable holding *a string of arguments* only expands into separate words in
a shell that word-splits. **bash does; zsh does not** — and zsh is the default
on macOS. There, `$COMPOSE up` tries to execute one long filename and fails
with `no such file or directory: docker compose -f …`. The zsh-only escape
`${=COMPOSE}` works but is easy to mistype as `$(=COMPOSE)`, which fails
differently and more confusingly:

```
zsh: COMPOSE not found
zsh: command not found: up
```

A function sidesteps all of it: no `setopt`, no `${=}`, no per-shell variant.

Those two files are the **data plane**. The admin control plane is a third,
added only when you want it — see
[Running with the admin service](#running-with-the-admin-service). Functions
are per-shell just as exports are, so if you change the definition remember
that **an already-open terminal keeps the old one until you `source .run-env`
again.**

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
compose up --no-deps postgres
```

Wait for `database system is ready to accept connections`.

If instead you get `✔ Container compose-postgres-1 Running` followed by
`Attaching to postgres-1` and then **nothing**, it is not hung: the container
was already up, so `up` had nothing to start and simply attached to the log
stream — and attaching only shows output produced *from that moment on*. An
idle PostgreSQL says nothing. To see what it printed while starting:

```bash
compose logs --tail 40 postgres
```

**`Ctrl-C` in an attached `up` stops the container**, including one that was
already running before you attached — compose prints `Gracefully Stopping…`
and it exits. There is no detach-without-stopping for `up`. When you want the
container left alone, read its logs with `compose logs -f postgres` instead,
or start it with `-d` in the first place.

### Terminal 2 — logging service

```bash
compose up --no-deps --build logging
```

Applies migrations at startup (`MECLOG_AUTO_MIGRATE=true`) — you never run
`mec-cast-logs migrate` by hand here. Wait for the uvicorn startup line, then
confirm from any terminal:

```bash
curl -s http://localhost:8000/health/ready
```

### Terminal 3 — Zenoh router

```bash
compose up --no-deps --build zenoh-router
```

The rendezvous point. Both the client and the edge dial into it; nothing
discovers anything by multicast (that is the whole reason Zenoh was chosen —
see [ADR-0001](../architecture/adr/0001-zenoh-over-dds.md)).

### Terminal 4 — edge ingest node

```bash
compose up --no-deps edge
```

Start the **consumer before the producer** so the first clouds are not
published into the void. It stamps arrival, computes latency, writes
`runs/$RUN_ID/edge/samples.csv`, and posts snapshots every 2 s.

### Terminal 5 — LiDAR client (the producer)

```bash
compose up --no-deps lidar-client
```

Override the workload without editing anything:

```bash
NUM_POINTS=60000 RATE_HZ=5.0 compose up --no-deps lidar-client
```

### Terminal 6 — netem impairment (optional)

```bash
compose up --no-deps netem
```

Shares the client's network namespace and impairs its egress, modelling the
5G uplink. Skip this terminal to measure the unimpaired floor. Change the
impairment:

```bash
NETEM_DELAY=50ms NETEM_JITTER=10ms NETEM_LOSS=1% compose up --no-deps netem
```

It applies `tc` once and then sleeps, so restarting *it* alone is how you
change impairment mid-run — the qdisc uses `replace`, so it is idempotent.

### Stopping

`Ctrl-C` in each terminal, in reverse order (producer first, so recorders
drain and flush). Then, from any terminal:

```bash
compose down
```

Add `-v` to also delete the database volume and start clean next time.

## Running with the admin service

Everything above names the run with `RUN_ID` in the environment. The admin
service does it from a web page instead: nodes subscribe to it on startup, and
you create, start and stop runs from a table. Neither replaces the other —
with no `ADMIN_URL` a node behaves exactly as it does above.

Full operator guide: [admin-service.md](../operations/admin-service.md).

### One extra compose file

```bash
compose() { docker compose -f deploy/compose/logging.yml -f deploy/compose/local.yml -f deploy/compose/admin.yml "$@"; }
```

Put that in `.run-env` in place of the two-file line, then **re-source it in
every terminal that is already open** — the old definition is still live there,
and compose will report `admin` as an orphan container if you miss one.

```bash
source .run-env && compose config --services | sort | tr '\n' ' '
```

That must list `admin` among the services. Checking what compose *resolves* is
better than checking what you typed: it catches a missing file as well as a
stale definition.

### What changes

Terminals 1–3 and 6 are unchanged. Terminals 4 and 5 gain `ADMIN_URL`, so they
must be **recreated**, not just restarted — a running container keeps the
environment it was created with:

```bash
compose up -d --force-recreate --no-deps edge lidar-client
```

The edge joins the active run on its own. The client waits for you to press
Start, because a robot should not begin streaming the moment it powers on; set
`ADMIN_AUTOSTART=true` in `deploy/compose/admin.yml` if you want it automatic.

Then open the page:

```bash
open http://localhost:8099/admin        # macOS
```

```bash
xdg-open http://localhost:8099/admin    # Linux
```

Press **Add run**, then **Start**. `RUN_ID` is ignored — the admin mints one.

### When nothing appears on the page

The usual cause is a stale `compose`: the nodes were created without
`ADMIN_URL` and took the standalone path, so they never dialled in and the page
shows zero nodes. Check what a node actually got:

```bash
docker inspect compose-edge-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep ADMIN
```

No output means it is running standalone. Re-source `.run-env` and recreate, as
above.

## Keeping six terminals in one window (tmux)

tmux is the one worth learning. It ships on most Linux boxes; on macOS
install it with `brew install tmux`. (`byobu` is Linux-only and not needed
here.)

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
| `gnb` | srsRAN O-DU host | ran-collector | `RUN_ID`, `LOGGING_HOST` |

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

The commands you will actually use, with what they mean. `compose` is the
two-file invocation from above; in the lab substitute
`docker compose -f deploy/lab/compose.<role>.yml`.

### Seeing what exists

```bash
compose ps
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
| `compose up -d` | all services, background |
| `compose up --no-deps <svc>` | one service, foreground, its logs |
| `compose stop <svc>` | SIGTERM, container kept |
| `compose start <svc>` | start a stopped container |
| `compose restart <svc>` | stop + start, same container |
| `compose down` | stop and remove containers + network |
| `compose down -v` | …and delete volumes (**database contents**) |

`stop` is graceful — 10 s by default. The recorders flush on shutdown, so
give them room when a run has data worth keeping:

```bash
compose stop -t 15 lidar-client edge
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
compose up -d --build edge
```

For changes to the telemetry crate or the ROS packages, rebuild the shared
image first — both the client and edge use it:

```bash
make build-ros2 && compose up -d edge lidar-client
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
compose logs -f edge
```

`-f` follows. Useful variants:

```bash
compose logs --tail 50 edge
```

```bash
compose logs -f --timestamps edge lidar-client
```

All services at once, colour-coded by service:

```bash
compose logs -f
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

Watch it grow live — the fastest confirmation that data is flowing. This
loop works on both platforms; `watch` is not installed on macOS:

```bash
while :; do clear; wc -l runs/$RUN_ID/*/samples.csv; sleep 2; done
```

On Linux, `watch -n 2 "wc -l runs/$RUN_ID/*/samples.csv"` is the shorthand.

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
compose restart edge
```

The recorder starts a fresh CSV section but keeps the same `RUN_ID`, so the
run stays joinable. Sequence numbers will show a gap — that is honest, and
visible in analysis.

**Restart after a code change** (restart alone will not pick it up):

```bash
make build-ros2 && compose up -d --build edge
```

**The logging service is down and components are running.** Nothing is lost
immediately: the recorder buffers snapshots and drops the oldest when the
buffer fills, counting every drop. Per-frame CSV is unaffected — it never
goes through HTTP. Bring the service back and snapshots resume:

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
compose down -v && compose up -d postgres logging
```

Never do that in the lab without a dump — see below.

**Everything is wedged and you want a clean slate (dev):**

```bash
compose down -v --remove-orphans && make build-ros2 && make up-local
```

## Checking system status

Start with what this host is running:

```bash
make version
```

Role, version, commit, submodule pins, every running container with the commit
its image was built from, and whether PTP is present. It inspects git, compose
and the containers rather than reading a version someone wrote down, so it
stays right on a host that was pulled but not redeployed — and says so:

```
WARNING: a running image was built from a different commit than this checkout.
```

Treat that as a stop. Measurements taken in that state cannot be attributed to
the code in front of you. Redeploy the role, or pull the matching image tag.

Then a quick pass, top to bottom:

```bash
compose ps
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
compose logs edge | grep -i drop
```

## Routine admin tasks

### Upgrading a host to a new release

Per host, per role. Do `infra` first — the edge and UE post snapshots to it.

```bash
cd ~/mec-cast && git fetch --tags && git checkout platform-v0.2.0
```

```bash
git submodule update --init --recursive
```

The submodule step is not optional. `git checkout` moves the pins but does not
move the working trees, so skipping it leaves the logging service at whatever
commit it was on — a mismatch that surfaces later as a schema rejection.

```bash
docker compose -f deploy/lab/compose.$ROLE.yml up -d --build
```

Then confirm, on that host:

```bash
make version
```

The version and commit should read `platform-v0.2.0`, and every container
should say **matches this checkout**. If one reports a different commit, its
image was cached from before the checkout — rebuild that service with
`--no-cache`, or pull the published `sha-` tag for this release.

Deploying from a workstation instead does all of this in one command, and
prints the same report at the end:

```bash
bash deploy/lab/deploy.sh edge morosev@edge-host
```

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
| `password authentication failed for user "postgres"` | Talking to a *host* PostgreSQL, not the container | `ss -ltn \| grep 5432` (Linux) or `lsof -iTCP:5432 -sTCP:LISTEN` (macOS); use `docker exec … psql` |
| No `samples.csv` appears | Producer and consumer disagree on `RUN_ID` | `compose exec edge printenv RUN_ID` in each |
| Edge sees no clouds | Router not up, or client started first | `compose logs zenoh-router`; restart `edge` then `lidar-client` |
| Snapshots missing, CSV fine | Logging service unreachable | `curl -s localhost:8000/health/ready` |
| `422` from the logging service | Extra top-level field; schema is `extra="forbid"` | [logging-submodule.md](../operations/logging-submodule.md) |
| Nonzero drop counters | Consumer slower than producer | `docker stats`; lower `RATE_HZ` or `NUM_POINTS` |
| `ptp.reliable: false` on a lab run | phc2sys not disciplining | `bash deploy/lab/ptp/verify-ptp.sh` |
| Port 8000 already allocated | Previous stack still up | `compose down`; `docker ps -a` |
| Code change has no effect | Image not rebuilt | `make build-ros2 && compose up -d --build` |
| Renderer logs in bursts with big `seq` gaps, round trips in the hundreds of ms | Uplink in congestion collapse — the edge is receiving a fraction of what is published. Not a renderer fault | `wc -l runs/$RUN_ID/*/samples.csv`: compare `pub` to `edge`. See below |
| Router floods `Route data with unknown scope N!` / `Declare token N for unknown scope M` | rmw_zenoh discovery declarations lost on the impaired link. More nodes and topics mean more declarations crossing it, so adding the renderer makes it more likely | Check delivery, not the log — data is unaffected. Lower `NETEM_LOSS`, or restart the stack so discovery re-runs |
| Client warns `Didn't receive DeclareFinal for interest …: Timeout(10s)!` | Same cause: the graph query did not complete over the lossy link | As above |

### Opening the renderer

The measurement path never depends on a renderer, so the default sink is
`null`: it records the full round trip and draws nothing. To see the cloud,
ask for the viewer:

```bash
NUM_POINTS=3000 NETEM_LOSS=0% RENDER_SINK=rerun compose -f deploy/compose/render.yml up -d
```

The node logs the address on startup, and **it is not the bare port** — open
what it prints:

```
render recording run <id> (sink=rerun) — viewer at
  http://localhost:9876/?url=rerun%2Bhttp://localhost:9877/proxy
```

Two ports, both published and both needed. 9876 serves the page; 9877 carries
the log stream, and the page connects to it *from your browser*, so it has to
be reachable from there too. Opening `http://localhost:9876` on its own gives
a viewer with no data source — rerun's `connect_to` does not bake the source
into the served page, which is why the node builds the query string for you.

Browsing from another machine — the lab UE is headless — set `VIEWER_HOST` to
the address you reach it on, or the URL will tell your own laptop to connect
to itself:

```bash
VIEWER_HOST=10.0.0.30 RENDER_SINK=rerun ...
```

**Every rerun run also writes `runs/<RUN_ID>/render/session.rrd`**, beside
`samples.csv`. That is the reliable path and usually the better one: the live
viewer needs two reachable ports, a browser that can run WebGPU or WebGL, and
you at the keyboard while the run happens. The file needs none of that — drag
it onto any Rerun viewer, including the page this node serves, and replay the
run afterwards. Set `record_rrd:=false` to skip it.

`RENDER_SINK=ros` is the third option: it republishes a plain
`sensor_msgs/PointCloud2` on `mec_cast/render/cloud` for RViz2 or Foxglove,
and needs no rerun at all.

### When the renderer looks broken but is not

Measured on the local topology, 3,000 points at 10 Hz with 20 ms netem, with
and without the renderer running:

| Setting | pub → edge | edge → render | Router `unknown scope` |
|---|---|---|---|
| 30,000 points, 0.5% loss | **1.9%** | 100% | many |
| 3,000 points, 0.5% loss | 93.7% | 100% | many |
| 3,000 points, 0% loss | 100% | 100% | 1 |

Three things fall out of that, and each answers a different false alarm.

**The renderer never causes uplink loss.** The same workload with and without
it delivered 1.53% and 1.54% to the edge — the difference is noise. The
downlink runs at 100% even while the uplink is collapsing, because it carries
a quarter of the bytes and is not behind the impairment.

Since it now logs a heartbeat every 5 s, that ambiguity is gone — a starved
renderer says so in words rather than falling silent:

```
[WARN] render alive but received NOTHING in 5s (total=1). The process is fine.
       Either the edge is not sending — it needs publish_result:=true, off by
       default — or the uplink is dropping frames before they reach it.
```

**`local.yml`'s defaults are over capacity on purpose.** 30,000 points at
10 Hz with 0.5% loss is the documented congestion-collapse case — see
[running-an-experiment.md](running-an-experiment.md#choosing-an-impairment-the-link-can-carry).
It is a legitimate experiment and a useless latency measurement. The renderer
simply makes it *visible*: it prints one line per frame received, so at 1.9%
delivery it appears to hang for seconds at a time and then emit a burst. Check
`RestartCount` before believing a node died:

```bash
docker inspect compose-render-1 --format '{{.RestartCount}} {{.State.Status}}'
```

**The router's `unknown scope` errors are discovery, not data.** Every one
observed was on the *client's* face — the only container behind netem — and
none referenced `mec_cast/cloud` or `mec_cast/result`. They are liveliness
tokens and interest declarations (`@ros2_lv/**`) whose key-expression
declarations were lost or reordered on the impaired link. They cluster in the
first second or two while the graph is being discovered; when that race goes
badly the router never resolves the scope and then logs once per message
thereafter, which looks alarming and changes nothing. Frame delivery was
identical with 11 errors and with 410.

Adding the renderer makes them more frequent because it puts one more node and
one more topic into the graph the client must discover — more declarations
across a lossy link — not because the return path is at fault.

## See also

- [running-an-experiment.md](running-an-experiment.md) — the measurement workflow
- [lab-topology.md](../operations/lab-topology.md) — hosts, addressing, PTP
- [timing-model.md](../architecture/timing-model.md) — what each metric means and when it is valid
- [logging-submodule.md](../operations/logging-submodule.md) — the schema contract and the service's security posture
