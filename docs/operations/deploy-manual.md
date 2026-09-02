# Deploy manual

Getting mec-cast onto machines and keeping them current — local box and lab
testbed. The full command set, in the order you actually need it.

This is the **procedure**. What the roles are, which host runs which, and the
addressing live in [lab-topology.md](lab-topology.md); why the router sits on
the edge, why PTP is off the user plane, and the rest of the reasoning live in
the [ADRs](../architecture/adr/README.md). Day-to-day operation of a deployment
that already exists is [admin-manual.md](admin-manual.md).

- [Prerequisites](#prerequisites)
- [One-time setup per machine](#one-time-setup-per-machine)
- [Database backups (infra role)](#database-backups-infra-role)
- [Local deployment](#local-deployment)
- [Lab deployment](#lab-deployment)
- [Watching a run in the lab](#watching-a-run-in-the-lab)
- [Starting and stopping a role](#starting-and-stopping-a-role)
- [Updating to a new version](#updating-to-a-new-version)
- [Verifying what actually landed](#verifying-what-actually-landed)

## Prerequisites

One-time, on any machine that will run components:

```bash
bash scripts/bootstrap-dev.sh
```

Confirm the two things everything else assumes:

```bash
docker compose version && docker ps >/dev/null && echo "docker OK"
```

Two optional tools are referenced throughout and neither ships by default on
Ubuntu:

```bash
sudo apt install -y jq uuid-runtime
```

On macOS `uuidgen` is built in; only `jq` is missing:

```bash
brew install jq
```

`jq` only pretty-prints JSON — `python3 -m json.tool` is given as the
alternative everywhere it appears. `uuid-runtime` provides `uuidgen`; where it
is absent the scripts fall back to `/proc/sys/kernel/random/uuid`, which is
Linux-only. That is why every snippet reads `uuidgen || cat /proc/...` in that
order: it keeps them working on macOS.

### The UE role only: rerun

Rerun is how the point cloud is *looked at*. Nothing measured depends on it —
the default sink is `null`, which records the full round trip and draws
nothing — so it is a testing convenience and belongs on the UE alone.

**The SDK, inside the ROS image.** Only `mec_cast_render` imports it, and only
when `sink=rerun`. It is most of the image's weight — measured, 2.38 GB with it
against 1.4 GB without — so build without it on every role that never renders:

```bash
docker build -f deploy/docker/ros.Dockerfile --build-arg WITH_RERUN=0 -t mec-cast-ros .
```

Edge, gNB and infra never render. Leave the default (`WITH_RERUN=1`) on the UE.

**The viewer, on the UE host.** Separate from the SDK: this is the application
that displays the stream. Install it once, on the UE and nowhere else:

```bash
python3 -m venv ~/.rrviewer && ~/.rrviewer/bin/pip install "rerun-sdk==0.36.3"
```

Match the version to the SDK the image pins (`>=0.36,<0.37` in
`ros.Dockerfile`) — a viewer from a different minor release may refuse the
recording. Watching a run is
[Watching a run in the lab](#watching-a-run-in-the-lab) below; on a laptop it
is [local-development.md](../guides/local-development.md#watching-it-live--the-native-viewer).

### Linux and macOS

The pipeline runs in containers and behaves identically. Only the host side
differs:

| | Linux | macOS |
|---|---|---|
| Default shell | bash | **zsh** — which is why the guides define `compose` as a function, never a `$COMPOSE` variable |
| Docker | daemon on the host | Docker Desktop or colima — **a VM that must be running first**; there is no group to add yourself to |
| `uuidgen` | `sudo apt install uuid-runtime` | built in |
| `jq` | `sudo apt install jq` | `brew install jq` |
| `watch` | built in | absent — use the `while` loop shown inline, or `brew install watch` |
| Listening sockets | `ss -ltn` | `lsof -iTCP -sTCP:LISTEN -n -P` |
| Open a URL | `xdg-open <url>` | `open <url>` |
| tmux | `sudo apt install tmux` | `brew install tmux` |

One macOS trap that is not really a difference: **start Docker Desktop before
the first command**, or everything fails with `Cannot connect to the Docker
daemon`. It is a VM and takes a moment.

## One-time setup per machine

Three things bite on a host's first deploy, and each fails in a way that points
somewhere other than the cause.

**The remote user must be in the `docker` group.** Otherwise the deploy reaches
the build step and stops with:

```
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
```

That is the *remote* user, not yours. On each lab host, once:

```bash
sudo usermod -aG docker $USER
```

Group membership applies only to new sessions, so log out and back in. Verify
from your workstation before deploying anything:

```bash
ssh <user>@<host> docker ps
```

**Do not run `deploy.sh` under `sudo`.** It needs no root locally — it is
`rsync` plus a few `ssh` calls. Under `sudo` those run as root and use *root's*
SSH identity rather than yours, so a working key is ignored and every step
prompts for a password. It cannot help with the error above either, which is
enforced on the far side.

**Set up key authentication**, or every `ssh` call prompts:

```bash
ssh-copy-id <user>@<host>
```

Worth doing for its own sake: the last deploy step runs the version report on
the host so you see what landed, and that is easy to abandon at a fifth
password prompt.

## Database backups (infra role)

The infra role deploys a `backup` service alongside PostgreSQL. It is on by
default at a weekly cadence, so a deployment that sets nothing still gets
backups — and takes its first one immediately, which is what proves the
configuration works.

Set the directory at deploy time:

```bash
BACKUP_DIR=/srv/mec-cast-backups BACKUP_EVERY=24h \
  bash deploy/lab/deploy.sh infra ops@infra-host
```

`BACKUP_DIR` is a path on the **infra host**. Point it at a mounted share
(NFS, SMB, an attached disk) to land the dumps on another machine — the
service only needs a writable directory, and does not care what is behind it.

Two constraints worth knowing before choosing a path:

- **Keep it outside `~/mec-cast`.** This script rsyncs that tree with
  `--delete`; anything inside it that is not in the source is removed on the
  next deploy.
- **It is created as root** if it does not exist, because the backup container
  runs as root to write there. Pre-create it with the ownership you want if
  that matters on your host.

`BACKUP_EVERY`, `BACKUP_KEEP` and `BACKUP_CHECK_EVERY` are forwarded the same
way and can be changed later without redeploying — see
[admin-manual.md](admin-manual.md#backup-and-restore).

**This covers PostgreSQL only.** The per-frame CSVs live on whichever host
produced them and no schedule collects them; `scripts/collect-runs.sh` merges
them into one tree and can archive it, run by hand when a campaign ends.

## Local deployment

Everything on one machine, with `netem` standing in for the radio. One command:

```bash
make up-local
```

To drive runs from the admin page instead of `RUN_ID` in the environment:

```bash
make up-admin
```

With the return path and a renderer at the UE:

```bash
RUN_ID=$(uuidgen) RENDER_SINK=rerun NETEM_LOSS=0% PATTERN=sphere make up-render
```

Running the components one per terminal — which is what you want when one of
them misbehaves — is [local-development.md](../guides/local-development.md).

## Lab deployment

Four roles across four hosts. Same containers, different composition, and real
5G instead of `netem`. The roles, hosts and required environment are tabulated
in [lab-topology.md](lab-topology.md).

`RUN_ID` is required only when running **without** the admin — see
[Without the admin service](#without-the-admin-service). With the admin it is
ignored, and the run id is minted for you.

**Order matters: `infra` → `edge` → `gnb` → `ue`.** Everything posts to the
logging service, and the UE dials the edge's Zenoh router. Start the UE last or
it retries against a router that is not there yet.

### The scripted path

From your workstation, once per host:

```bash
INFRA_HOST=10.0.0.10 bash deploy/lab/deploy.sh infra ops@infra-host
```

```bash
INFRA_HOST=10.0.0.10 bash deploy/lab/deploy.sh edge ops@edge-host
```

```bash
EDGE_HOST=10.0.0.20 INFRA_HOST=10.0.0.10 bash deploy/lab/deploy.sh gnb ops@gnb-host
```

```bash
EDGE_HOST=10.0.0.20 INFRA_HOST=10.0.0.10 bash deploy/lab/deploy.sh ue ops@ue-host
```

Each call rsyncs the repo (excluding `third_party/`, `runs/`, `target/`),
builds on the far side, runs that role's compose file, verifies PTP, and prints
the version report.

**Variables are read from your shell and forwarded.** An `export` does not
survive the SSH hop on its own, so `deploy.sh` collects them and passes them
explicitly. Missing ones fail immediately with a message naming the variable
and the role, rather than two minutes later as a compose interpolation error on
the far side.

Optional variables it forwards when set: `RUN_ID`, `PATTERN`, `NUM_POINTS`,
`RATE_HZ`, `SEED`, `RENDER_SINK`, `PUBLISH_RESULT`, `RESULT_RELIABILITY`,
`RESULT_QOS_DEPTH`, `ADMIN_URL`, `LIDAR_INSTANCES`, `RENDER_INSTANCES`,
`VIEWER_HOST`, `CELL`, `POSTGRES_PASSWORD`, `METRICS_PORT`,
`MECLOG_BUILD_CONTEXT`.

### Two roles on one machine

Supported. Service names do not collide, ports do not overlap, `/dev/ptp0` is
shared read-only, and `deploy.sh` passes no `--remove-orphans`, so deploying a
second role does not evict the first. Deploy them one after the other:

```bash
INFRA_HOST=10.0.0.10 bash deploy/lab/deploy.sh infra ops@small-lab-host
```

```bash
INFRA_HOST=10.0.0.10 bash deploy/lab/deploy.sh edge ops@small-lab-host
```

The version report on that host then lists both roles.

### Without the admin service

The control plane is optional in the lab exactly as it is on a laptop. Pass an
**empty** `ADMIN_URL` and name the run yourself:

```bash
ADMIN_URL= RUN_ID=$(uuidgen) EDGE_HOST=10.0.0.20 INFRA_HOST=10.0.0.10 \
  bash deploy/lab/deploy.sh ue ops@ue-host
```

Two rules, and both are easy to get wrong silently:

- **`RUN_ID` must be identical on every role.** It becomes `trace_id`, the key
  that correlates UE, edge and RAN records for one experiment. Different values
  break nothing at runtime — the data simply cannot be joined, which you
  discover at analysis. Mint it once and paste the same value into each deploy.
- **`ADMIN_URL=` must be empty, not omitted.** Every role defaults it to a live
  address, so omitting it means "dial the admin" — and with no admin deployed
  the client waits for a Start that never comes while `RUN_ID` is ignored.

Empty is a real value here, which is why `deploy.sh` forwards variables that are
*set* rather than merely non-empty, and why the compose files use `${ADMIN_URL-…}`
rather than `${ADMIN_URL:-…}`: the `:-` form cannot tell empty from absent.

Deploying by hand on the host, the same thing:

```bash
ADMIN_URL= RUN_ID=<the-one-run-id> INFRA_HOST=10.0.0.10 EDGE_HOST=10.0.0.20 \
  docker compose -f deploy/lab/compose.ue.yml up -d --build
```

Confirm a node took the standalone path — it names the run in its first log
line, rather than reporting an admin connection:

```bash
docker compose -f deploy/lab/compose.ue.yml logs ue-agent | grep "streaming run"
```

### Doing it by hand on each host

The same thing without the script — which is what you want when debugging. SSH
in, then:

```bash
cd ~/mec-cast && export RUN_ID=<the-one-run-id-for-all-hosts>
```

**infra:**

```bash
docker compose -f deploy/lab/compose.infra.yml up -d --build
```

**edge** (`INFRA_HOST` is the infra host's management-LAN address — it serves both the logging service on `:8000` and the admin on `:8099`):

```bash
INFRA_HOST=10.0.0.10 docker compose -f deploy/lab/compose.edge.yml up -d --build
```

**gNB** — also point srsRAN at it in `gnb.yml` under `metrics:` with
`addr: <gnb-host>` and `port: 55555`:

```bash
INFRA_HOST=10.0.0.10 docker compose -f deploy/lab/compose.gnb.yml up -d --build
```

**UE** (`EDGE_HOST` must be reachable across the UPF):

```bash
INFRA_HOST=10.0.0.10 EDGE_HOST=10.0.0.20 docker compose -f deploy/lab/compose.ue.yml up -d --build
```

To run one in the foreground for debugging, drop `-d` and name the service.

### Watching a run in the lab

The renderer runs on the UE, so the stream is served there. Turn it on for the
role:

```bash
RENDER_SINK=rerun RENDER_INSTANCES=1 PUBLISH_RESULT=1 \
  EDGE_HOST=10.0.0.20 INFRA_HOST=10.0.0.10 \
  bash deploy/lab/deploy.sh ue ops@ue-host
```

`PUBLISH_RESULT=1` belongs on the **edge** role, not the UE — the renderer
draws what the edge sends back, and the edge does not send it by default. A
renderer without it sits healthy and empty, which the admin page reports as
`WF_RENDER_STARVED`.

Then watch, with the viewer installed on the UE:

```bash
ssh -X ops@ue-host '~/.rrviewer/bin/rerun --port auto rerun+http://localhost:9877/proxy'
```

`ssh -X` because a lab UE has no display of its own; the window opens on your
workstation. That needs `xauth` on the UE and an X server on your side — WSLg
provides one on Windows, as does any Linux desktop.

**`--port auto` is not optional.** Without it the viewer defaults to 9876,
finds the render node's own web server already listening there, decides
another viewer is running, streams its data to that instead, and exits looking
like it did nothing.

If `ssh -X` is unavailable, forward the stream port and run a viewer on your
workstation instead — but that means installing rerun there too, which is the
thing this section is trying to avoid:

```bash
ssh -L 9877:localhost:9877 ops@ue-host
```

Every rerun run also writes `runs/<RUN_ID>/render-0/session.rrd` on the UE.
Copying that back and opening it locally needs no live connection at all, and
is usually the better answer for a campaign you want to review later.

### PTP — before trusting any cross-host number

Lab roles mount `/dev/ptp0`. Cross-host latency means nothing unless the clocks
are disciplined:

```bash
bash deploy/lab/ptp/verify-ptp.sh
```

Run it on **every** measuring host. `deploy.sh` runs it for you and warns on
failure. Every snapshot also records `context.ptp.reliable`, so a bad run can
be filtered afterwards — but noticing during the run is far cheaper. The
reasoning is [ADR-0003](../architecture/adr/0003-ptp-on-management-lan.md); the
setup is [deploy/lab/ptp/](../../deploy/lab/ptp/README.md).

## Starting and stopping a role

**Set the role's variables on the host first.** Compose interpolates the whole
file before it does anything, so `${INFRA_HOST:?}` stops even a read-only
command:

```
error while interpolating services.ue-agent.environment.ADMIN_URL:
required variable INFRA_HOST is missing a value: set INFRA_HOST
```

That is `docker compose ps` failing, not a broken deployment. Put the values
and a shorthand in `~/mec-cast/.run-env` on that host, once:

```bash
cat > .run-env <<'EOF'
export INFRA_HOST=10.0.0.10
export EDGE_HOST=10.0.0.20
compose() { docker compose -f deploy/lab/compose.ue.yml "$@"; }
EOF
source .run-env
```

Use the role's own file, and `source` it in each terminal — a function defined
in a subshell dies with it. `.run-env` is gitignored and is excluded from the
deploy rsync, so it survives `deploy.sh`; nothing else you leave in
`~/mec-cast` will.

On the host, per role:

```bash
docker compose -f deploy/lab/compose.$ROLE.yml up -d
```

```bash
docker compose -f deploy/lab/compose.$ROLE.yml stop
```

```bash
docker compose -f deploy/lab/compose.$ROLE.yml restart edge
```

`stop` is graceful — 10 s by default. The recorders flush on shutdown, so give
them room when the run has data worth keeping:

```bash
docker compose -f deploy/lab/compose.$ROLE.yml stop -t 15
```

Tearing a role down completely, keeping the database:

```bash
docker compose -f deploy/lab/compose.$ROLE.yml down --remove-orphans
```

**Never add `-v` on the infra host.** That deletes the `pgdata` volume — every
aggregated snapshot of every run. Take a dump first; see
[admin-manual.md](admin-manual.md#backup-and-restore).

## Updating to a new version

Per host, per role. Do `infra` first — the edge and UE post snapshots to it.

```bash
cd ~/mec-cast && git fetch --tags && git checkout platform-v0.4.0
```

```bash
git submodule update --init --recursive
```

**The submodule step is not optional.** `git checkout` moves the pins but not
the working trees, so skipping it leaves the logging service at whatever commit
it was on — a mismatch that surfaces later as a schema rejection. See
[logging-submodule.md](logging-submodule.md).

```bash
docker compose -f deploy/lab/compose.$ROLE.yml up -d --build
```

Then confirm on that host:

```bash
make version
```

The version and commit should read `platform-v0.4.0`, and every container
should say **matches this checkout**. If one reports a different commit, its
image was cached from before the checkout — rebuild that service with
`--no-cache`, or pull the published `sha-` tag for the release.

Deploying from a workstation does all of this in one command and prints the
same report at the end:

```bash
bash deploy/lab/deploy.sh edge ops@edge-host
```

Re-running it rsyncs, rebuilds and restarts that role, which is also how you
keep hosts in sync after any change. Deploy `infra` first when the change
touches the logging schema.

## Verifying what actually landed

```bash
make version
```

Role or roles, version, commit, submodule pins, every running container with
the commit its image was built from, and whether PTP is present. It inspects
git, compose and the containers rather than reading a version someone wrote
down, so it stays right on a host that was pulled but not redeployed — and says
so:

```
WARNING: a running image was built from a different commit than this checkout.
```

Treat that as a stop. Measurements taken in that state cannot be attributed to
the code in front of you. Redeploy the role, or pull the matching image tag.

A host deployed by `deploy.sh` has no `.git` (it is excluded from the rsync),
so the report falls back to the `.deployed-version` stamp the deploy leaves
behind. Hosts you `git pull` keep their checkout and never consult it.

### Pre-campaign checklist

1. `bash deploy/lab/ptp/verify-ptp.sh` on UE, edge and gNB — all must pass.
2. `curl -sf http://$INFRA_HOST:8000/health/ready`
3. Confirm the gNB's `gnb.yml` `metrics.addr/port` points at the gNB host's
   collector (default port 55555).
4. One short smoke run; confirm `runs/<id>/{pub-0,edge-0,ran}/samples.csv` all
   appear and `context.ptp.reliable` is `true` in the snapshots.

Step 4 is what catches a mis-set `RUN_ID` before it costs you a campaign.

## See also

- [admin-manual.md](admin-manual.md) — operating a deployment that already exists
- [lab-topology.md](lab-topology.md) — roles, hosts, addressing
- [local-development.md](../guides/local-development.md) — running components by hand on a laptop
- [deploy/README.md](../../deploy/README.md) — what is in the deploy tree
