# Local development

Running the pipeline on one machine, by hand, one container per terminal.

`make up-local` does all of this in one command. Read this when you need to
know *what it is doing*, or when one component misbehaves and you want it in
the foreground.

Getting software onto machines is
[deploy-manual.md](../operations/deploy-manual.md); operating a deployment is
[admin-manual.md](../operations/admin-manual.md); the measurement workflow is
[running-an-experiment.md](running-an-experiment.md).

- [Four concepts first](#four-concepts-first)
- [One container per terminal](#one-container-per-terminal)
- [Driving it from the command line only](#driving-it-from-the-command-line-only)
- [Six terminals in one window (tmux)](#six-terminals-in-one-window-tmux)
- [Opening the renderer](#opening-the-renderer)
- [Watching it live — the native viewer](#watching-it-live--the-native-viewer)
- [When the renderer looks broken but is not](#when-the-renderer-looks-broken-but-is-not)

## Four concepts first

**1. `RUN_ID` is the join key.** It becomes the `trace_id` on every log record
and the directory name under `runs/`. Every component in one experiment **must**
share it. Start components with different `RUN_ID`s and nothing is technically
broken — the data simply cannot be correlated, which you will not discover
until analysis. Each terminal is a separate shell, so put it in a file:

```bash
echo "export RUN_ID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)" > .run-env
```

Then in **every** terminal, first thing:

```bash
cd ~/mec-cast && source .run-env && echo "RUN_ID=$RUN_ID"
```

`.run-env` is gitignored. It defaults to `dev-run` if you skip this, which is
fine for poking at the system and useless for a campaign.

**2. Two compose files, always passed together.** `logging.yml` has the
backend, `local.yml` has the pipeline. They must be one compose project so the
containers share a network and `http://logging:8000` resolves. Define it as a
shell **function** and add it to `.run-env`:

```bash
cat >> .run-env <<'EOF'
compose() { docker compose -f deploy/compose/logging.yml -f deploy/compose/local.yml "$@"; }
EOF
```

A function rather than `export COMPOSE="docker compose -f …"` because a
variable holding *a string of arguments* only expands into separate words in a
shell that word-splits. **bash does; zsh does not** — and zsh is the macOS
default. There, `$COMPOSE up` tries to execute one long filename. The zsh-only
escape `${=COMPOSE}` works but is easy to mistype as `$(=COMPOSE)`, which fails
differently and more confusingly. A function sidesteps all of it.

Those two files are the **data plane**. The admin control plane is a third,
added only when you want it. Functions are per-shell just as exports are, so
**an already-open terminal keeps the old definition until you `source .run-env`
again.**

**3. Service name vs container name.** Compose commands take the *service* name
(`postgres`); raw `docker` commands take the *container* name
(`compose-postgres-1` — project, service, replica):

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

**4. Where data lands.** Per-frame CSV is written inside the container to
`/runs`, bind-mounted to `runs/` in the repo — so it survives any teardown and
is readable directly from the host. Aggregated snapshots go over HTTP to the
logging service and live in PostgreSQL, in the named volume `pgdata`. `make
down` keeps that volume; `make down-hard` deletes it.

Directory leaves are instance-suffixed — `runs/$RUN_ID/pub-0/`, `pub-1/`,
`edge-0/`, `render-0/` — one per node instance, since a UE can host several
LiDARs and renderers in one process.

## One container per terminal

> `compose` is the shell function defined under [Four concepts first](#four-concepts-first).

Six components, started in this order. Each command runs in the foreground,
printing that component's logs, and stops with `Ctrl-C`.

`--no-deps` is what makes this "one container per terminal": without it,
compose helpfully starts the dependencies too and you lose the isolation you
opened six terminals to get.

Every terminal starts with `cd ~/mec-cast && source .run-env`.

### Terminal 1 — PostgreSQL

```bash
compose up --no-deps postgres
```

Wait for `database system is ready to accept connections`.

If instead you get `✔ Container compose-postgres-1 Running` and then
**nothing**, it is not hung: the container was already up, so `up` had nothing
to start and simply attached to the log stream — and attaching only shows
output produced from that moment on. An idle PostgreSQL says nothing:

```bash
compose logs --tail 40 postgres
```

**`Ctrl-C` in an attached `up` stops the container**, including one that was
already running before you attached. There is no detach-without-stopping.

### Terminal 2 — logging service

```bash
compose up --no-deps --build logging
```

Applies migrations at startup (`MECLOG_AUTO_MIGRATE=true`). Wait for the
uvicorn startup line, then confirm from any terminal:

```bash
curl -s http://localhost:8000/health/ready
```

### Terminal 3 — Zenoh router

```bash
compose up --no-deps --build zenoh-router
```

The rendezvous point. Both the client and the edge dial into it; nothing
discovers anything by multicast, which is the whole reason Zenoh was chosen —
see [ADR-0001](../architecture/adr/0001-zenoh-over-dds.md).

### Terminal 4 — edge ingest node

```bash
compose up --no-deps edge
```

Start the **consumer before the producer** so the first clouds are not
published into the void. It stamps arrival, computes latency, writes
`runs/$RUN_ID/edge-0/samples.csv`, and posts snapshots every 2 s.

### Terminal 5 — the UE agent (the producer)

```bash
compose up --no-deps lidar-client
```

Override the workload without editing anything:

```bash
NUM_POINTS=60000 RATE_HZ=5.0 compose up --no-deps lidar-client
```

Several LiDARs in one process, each with its own output directory:

```bash
LIDAR_INSTANCES=2 compose up --no-deps lidar-client
```

### Terminal 6 — netem impairment (optional)

```bash
compose up --no-deps netem
```

Shares the client's network namespace and impairs its egress, modelling the 5G
uplink. Skip this terminal to measure the unimpaired floor:

```bash
NETEM_DELAY=50ms NETEM_JITTER=10ms NETEM_LOSS=1% compose up --no-deps netem
```

It applies `tc` once and then sleeps, so restarting *it* alone is how you change
impairment mid-run — the qdisc uses `replace`, so it is idempotent.

### Stopping

`Ctrl-C` in each terminal, in reverse order (producer first, so the recorders
drain and flush). Then from any terminal:

```bash
make down
```

That keeps the database. `make down-hard` also deletes it.

## Driving it from the command line only

The nodes need no admin service and no environment: every variable has a ROS
parameter equivalent, with the environment demoted to a default. A laptop with
no infra host can run a two-LiDAR experiment:

```bash
ros2 run mec_cast_ue ue_agent --ros-args \
  -p run_id:=dev-001 -p lidar_count:=2 -p render_count:=1 \
  -p num_points:=3000 -p rate_hz:=10.0 -p pattern:=sphere \
  -p reliability:=reliable -p runs_dir:=./runs
```

With no `admin_url` the node starts recording immediately under its `run_id`.
`make up-local` and `make up-render` exercise exactly this path, which is what
keeps it working.

## Six terminals in one window (tmux)

tmux is the one worth learning. It ships on most Linux boxes; on macOS
`brew install tmux`.

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

```bash
tmux attach -t meccast
```

This matters in the lab: an SSH drop kills plain foreground containers, but a
detached tmux session keeps running. Start long experiments inside tmux.

All six panes at once:

```bash
tmux new -s meccast \; split-window -h \; split-window -v \; select-pane -t 0 \; split-window -v \; select-layout tiled
```

## Opening the renderer

The measurement path never depends on a renderer, so the default sink is
`null`: it records the full round trip and draws nothing. To see the cloud:

```bash
RUN_ID=$(uuidgen) RENDER_SINK=rerun NETEM_LOSS=0% PATTERN=sphere make up-render
```

Every variable there is load-bearing, and leaving any off fails in a way that
looks like a broken renderer:

- **`RUN_ID`** — `up-render` does not mint one the way `up-local` does, so
  without it the run is `dev-run`. Not an error, but the recorder *appends*, so
  every unnamed run piles into a single `dev-run` directory under `runs/`
  and no new directory ever appears.
- **`NETEM=0`** — drop the impairment sidecar entirely, on any `up-` target:
  `NETEM=0 make up-render-admin`. `make up-unimpaired` is the shorthand for
  the plain topology. This is the floor worth knowing: measured at the 5,000
  point default, the whole pipeline costs **p50 6.0 ms, p99 10.0 ms** with a
  clean link, of which 1.5 ms is network and 4 ms is the edge's own
  processing. Everything above that is the impairment, not the platform.
  It must be repeated on every `up`: an impaired `up` after an unimpaired one
  recreates the sidecar.
- **`NETEM_LOSS=0%`** — keeps the delay but removes the loss. The default
  `0.5%` is survivable at the default 5,000
  points (94% of frames reach the edge) but it does not leave the tail alone:
  p50 56 ms against p99 849 ms. Set it to `0%` when you want a clean latency
  figure, and raise `NUM_POINTS` instead when you want to study collapse. At
  30,000 points the same 0.5% delivers 1.9%, frames die on the uplink before
  the edge sees them, and the renderer draws nothing while reporting itself
  perfectly healthy. See
  [Choosing an impairment the link can carry](running-an-experiment.md#choosing-an-impairment-the-link-can-carry).
- **`RENDER_SINK=rerun`** — without it the sink is `null`: the node measures and
  draws nothing, so the published ports accept nothing.
- **`PATTERN`** — optional. `sphere` reads as a shape; the default
  `uniform_cube` is noise and barely compresses on the downlink.

The node logs the address on startup, and **it is not the bare port**:

```
render recording run <id> (sink=rerun) — viewer at
  http://localhost:9876/?url=rerun%2Bhttp://localhost:9877/proxy
```

Two ports, both published and both needed. 9876 serves the page; 9877 carries
the log stream, and the page connects to it *from your browser*, so it has to be
reachable from there too. Opening `http://localhost:9876` alone gives a viewer
with no data source — rerun's `connect_to` does not bake the source into the
served page, which is why the node builds the query string.

With several renderers, instance *j* serves `9876+2j` / `9877+2j`.

Browsing from another machine — the lab UE is headless — set `VIEWER_HOST` to
the address you reach it on, or the URL tells your own laptop to connect to
itself:

```bash
VIEWER_HOST=10.0.0.30 RENDER_SINK=rerun ...
```

### Watching it live — the native viewer

The page above works in an ordinary desktop browser, but it needs two
reachable ports, a browser that can run WebGPU or WebGL, and the exact `?url=`
query. The **native viewer connects straight to the gRPC stream** and needs
none of them. It is the better way to watch a run happen.

The viewer belongs on the machine running the **UE role** — locally that is
this one. It is a testing convenience: no measurement depends on it, and the
edge, gNB and infra roles need neither the viewer nor the SDK (see
[deploy-manual.md](../operations/deploy-manual.md#the-ue-role-only-rerun)).

One-time install. **In WSL or a Linux shell, not PowerShell** — this is a
Linux binary:

```bash
python3 -m venv ~/.rrviewer && ~/.rrviewer/bin/pip install "rerun-sdk==0.36.3"
```

Match the version to the SDK pinned in `deploy/docker/ros.Dockerfile`
(`>=0.36,<0.37`); a viewer from another minor release may refuse the
recording.

Then, while a run is streaming:

```bash
~/.rrviewer/bin/rerun --port auto rerun+http://localhost:9877/proxy
```

A window opens and fills with the live cloud. Three things to know:

- **`--port auto` is not optional.** Without it the viewer defaults to port
  9876, finds the render node's own web server already listening there,
  concludes "another viewer is already running", and streams its data *to*
  that instead of opening a window — so it appears to do nothing at all. Its
  log says so plainly, which is the only clue.
- **Match the SDK version** to the one in the image (`rerun-sdk==0.36.3`).
  A viewer newer than the stream may refuse the recording.
- **Under WSLg there is no GPU passthrough**, so the viewer falls back to a
  software rasterizer and warns about it. Fine at 3,000–6,000 points, slow at
  30,000. Lower `NUM_POINTS` for a viewing session — the measurement does not
  care what you are looking at.

For a lab UE, forward the stream port first and the same command works:

```bash
ssh -L 9877:localhost:9877 ops@ue-host
```

**Every rerun run also writes `runs/<RUN_ID>/render-0/session.rrd`**, beside
`samples.csv`. That is the archive: it replays a finished run without the
stream, the ports, or you being at the keyboard while it happened. Open it with
`rerun runs/<RUN_ID>/render-0/session.rrd`, or drag it onto a running viewer.
Use the native viewer above for watching live; use this for reviewing later or
sending someone a run.

They grow quickly — a long run at 10 Hz with real point counts reached 224 MB.
Set `record_rrd:=false` when you only want the measurement.

`RENDER_SINK=ros` republishes a plain `sensor_msgs/PointCloud2` on
`mec_cast/render/cloud` for RViz2 or Foxglove, and needs no rerun at all.

## When the renderer looks broken but is not

Measured on the local topology, 3,000 points at 10 Hz with 20 ms netem:

| Setting | pub → edge | edge → render | Router `unknown scope` |
|---|---|---|---|
| 30,000 points, 0.5% loss | **1.9%** | 100% | many |
| 3,000 points, 0.5% loss | 93.7% | 100% | many |
| 3,000 points, 0% loss | 100% | 100% | 1 |

Three things fall out, each answering a different false alarm.

**The renderer never causes uplink loss.** The same workload with and without it
delivered 1.53% and 1.54% to the edge — the difference is noise. The downlink
runs at 100% even while the uplink collapses, because it carries a quarter of
the bytes and is not behind the impairment.

**Check the workload before blaming the renderer.** The default is now 5,000
points, which fits the impaired link — 94% of frames reach the edge. But
`NUM_POINTS=30000` at 10 Hz with 0.5% loss is the documented
congestion-collapse case — see
[running-an-experiment.md](running-an-experiment.md#choosing-an-impairment-the-link-can-carry)
— and delivers **1.9%**. That is a legitimate experiment and a useless latency
measurement, and the renderer is what makes it visible: it prints one line per
frame received, so at that rate it appears to hang and then emit a burst. It
logs a heartbeat every 5 s, so a starved renderer says so in words:

```
[WARN] render alive but received NOTHING in 5s (total=1). The process is fine.
       Either the edge is not sending — it needs publish_result:=true, off by
       default — or the uplink is dropping frames before they reach it.
```

Check `RestartCount` before believing a node died:

```bash
docker inspect compose-render-1 --format '{{.RestartCount}} {{.State.Status}}'
```

**The router's `unknown scope` errors are discovery, not data.** Every one
observed was on the *client's* face — the only container behind netem — and none
referenced `mec_cast/cloud` or `mec_cast/result`. They are liveliness tokens and
interest declarations (`@ros2_lv/**`) whose declarations were lost or reordered
on the impaired link. They cluster in the first second or two while the graph is
discovered; when that race goes badly the router never resolves the scope and
logs once per message thereafter, which looks alarming and changes nothing.
Frame delivery was identical with 11 errors and with 410.

Adding the renderer makes them more frequent because it puts one more node and
one more topic into the graph the client must discover — not because the return
path is at fault.

## See also

- [running-an-experiment.md](running-an-experiment.md) — the measurement workflow
- [admin-manual.md](../operations/admin-manual.md) — logs, database, backup, troubleshooting
- [deploy-manual.md](../operations/deploy-manual.md) — prerequisites and deployment
- [timing-model.md](../architecture/timing-model.md) — what each metric means and when it is valid
