#!/bin/bash
# The O-RAN SC near-RT RIC for the E2 path (ADR-0010), pinned and outside
# this repository.
#
#   bash deploy/lab/ric/ric.sh up        # clone at the pin if needed, start
#   bash deploy/lab/ric/ric.sh status    # containers, E2 port, connected E2 nodes
#   bash deploy/lab/ric/ric.sh kpm       # stock KPM monitor xApp, logged under runs/ric/
#   KPM_SECONDS=30 bash deploy/lab/ric/ric.sh kpm   # ...stopping by itself after 30 s
#   INFRA_HOST=10.0.0.10 bash deploy/lab/ric/ric.sh xapp      # the mec-cast xApp
#   INFRA_HOST=10.0.0.10 bash deploy/lab/ric/ric.sh xapp -d   # ...detached
#   bash deploy/lab/ric/ric.sh down
#
# Runs on the infra role. The RIC is srsRAN's oran-sc-ric (AGPL-3.0): it is
# cloned to $RIC_DIR (default ~/oran-sc-ric), checked out at RIC_PIN below and
# never modified or vendored. compose.override.yml beside this script is our
# only addition: it publishes e2term's SCTP port so the lab gNB can reach it.
#
# The mec-cast xApp (ran/xapp, E2_ADAPTER=osc) runs inside the RIC's xApp
# runner, where the routing table delivers indications; INFRA_HOST is how it
# reaches the admin and the logging service from inside the RIC's network.
#
# Variables: RIC_DIR, RIC_E2_BIND (host address to publish E2 on, default all),
# STYLE (kpm report style, 2), METRICS (kpm metrics), UE_IDS (styles 2/5, "0"),
# E2_NODE_ID (default: the first node the RIC reports connected), KPM_SECONDS
# (kpm only: stop after this long; default until Ctrl-C). STYLE, METRICS and
# UE_IDS reach the xApp too, as its KPM_STYLE / KPM_METRICS / KPM_UE_IDS
# defaults; run params still override them per run.
# KPM_LOGGING=0 keeps the xApp's KPM rows and controls out of the logging
# service (ran-kpm/ is still written); the JSON tap's RAN_LOGGING is separate.
set -euo pipefail

RIC_REPO=https://github.com/srsran/oran-sc-ric.git
# 2025-10-14 "xApps: add e2sm-ccc module and an example xApp using it".
# Bump deliberately, and say so in the commit: the xApp is written against it.
RIC_PIN=621ade26251f69a4ad079ba98bb708d8e5aeeb98

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$HERE/../../.." && pwd)"
RIC_DIR=${RIC_DIR:-$HOME/oran-sc-ric}
# Style 2: one UE, the LiDAR UE. Style 5 needs at least two UE ids, and an
# agent asked for a UE it does not have rejects the whole subscription.
# Verified against OCUDU 26.10 (docs/guides/local-5g-ocudu.md, Part 3).
STYLE=${STYLE:-2}
# What OCUDU 26.10's KPM module serves, beyond its dummy CQI/RSRP/RSRQ. One
# unsupported name fails the WHOLE subscription ("Action not supported" in the
# gNB log, RICSubscriptionFailure cause misc/0 at submgr): that is why
# DRB.PacketSuccessRateUlgNBUu, which srsRAN listed and OCUDU does not serve,
# is no longer here.
METRICS=${METRICS:-DRB.UEThpDl,DRB.UEThpUl,DRB.RlcSduTransmittedVolumeDL,DRB.RlcSduTransmittedVolumeUL,DRB.RlcPacketDropRateDl}
UE_IDS=${UE_IDS:-0}

# The RIC images, and the .debs the xApp runner image installs, are amd64
# only. On an arm64 host build and run them as amd64. The lab's infra host is
# x86-64 and needs none of it.
#
# HOW they run as amd64 matters. QEMU user emulation >= 9.0.1 crashes Go
# programs in the garbage collector ("fatal error: lfstack.push", QEMU issue
# 2560) -- the build of rtmgr_sim, and the RIC's Go services at runtime. Use
# Rosetta in an Apple Silicon VM (Lima: --rosetta), or QEMU 8.1.x
# (tonistiigi/binfmt:qemu-v8.1.5). See docs/guides/local-5g-ocudu.md.
amd64_hint() {
  local bf=/proc/sys/fs/binfmt_misc
  if [ -e "$bf/rosetta" ]; then
    return
  elif [ -e "$bf/qemu-x86_64" ]; then
    echo "NOTE: amd64 runs under QEMU here. QEMU >= 9.0.1 crashes the RIC's Go code"
    echo "  (lfstack.push). If the build or a RIC service dies that way, switch to"
    echo "  Rosetta (Lima VM) or QEMU 8.1.x -- docs/guides/local-5g-ocudu.md, Part 3."
  else
    echo "WARNING: arm64 host with no amd64 emulation registered; the RIC images cannot run."
    echo "  Rosetta (Lima VM) or: docker run --privileged --rm tonistiigi/binfmt:qemu-v8.1.5 --install amd64"
  fi
}
case "$(uname -m)" in
  arm64|aarch64) export DOCKER_DEFAULT_PLATFORM=${DOCKER_DEFAULT_PLATFORM:-linux/amd64}; ARM64=1 ;;
  *) ARM64=0 ;;
esac

export MECCAST_ROOT="$ROOT_DIR"
# runs/ is often created by the mec-cast containers, as root. Then this script,
# run as the operator, cannot add runs/ric and dies on a bare mkdir error.
if ! mkdir -p "$ROOT_DIR/runs/ric" 2>/dev/null; then
  echo "ERROR: cannot write $ROOT_DIR/runs (owned by $(stat -c %U "$ROOT_DIR/runs" 2>/dev/null || echo '?'))." >&2
  echo "  The containers created it as root. Give it back once: sudo chown \$USER: $ROOT_DIR/runs" >&2
  exit 1
fi

compose() { docker compose --project-directory "$RIC_DIR" -f "$RIC_DIR/docker-compose.yml" -f "$HERE/compose.override.yml" "$@"; }

# Stop processes in the xApp runner whose command line contains $1. `compose
# exec` without a TTY (as under `| tee`) does not forward Ctrl-C, so a monitor
# outlives its terminal and keeps its HTTP/RMR ports: the next one then dies
# with "Address already in use" before subscribing. Python, because the runner
# image (python:3.8-slim) has no pkill.
stop_in_runner() {
  compose exec -T python_xapp_runner python3 - "$1" <<'PY' 2>/dev/null || true
import os, signal, sys
pat, me = sys.argv[1], os.getpid()
for pid in filter(str.isdigit, os.listdir("/proc")):
    if int(pid) in (me, os.getppid()):
        continue
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().decode().split("\0")
    except OSError:
        continue
    # An ARGUMENT that is the script or module itself (./kpm_mon_xapp.py,
    # -m mec_cast_xapp), never a shell whose command text merely mentions it.
    if any(os.path.basename(a) == pat for a in argv):
        os.kill(int(pid), signal.SIGTERM)
        print(f"    stopped a leftover {pat} (pid {pid})")
PY
}

checkout() {
  if [ ! -d "$RIC_DIR/.git" ]; then
    echo "==> cloning oran-sc-ric into $RIC_DIR"
    git clone --quiet "$RIC_REPO" "$RIC_DIR"
  fi
  local head
  head=$(git -C "$RIC_DIR" rev-parse HEAD)
  if [ "$head" != "$RIC_PIN" ]; then
    if [ -n "$(git -C "$RIC_DIR" status --porcelain)" ]; then
      echo "ERROR: $RIC_DIR has local changes and is not at the pin; refusing to move it." >&2
      echo "  It should be a clean copy: git -C $RIC_DIR status" >&2
      exit 1
    fi
    echo "==> checking out the pin $RIC_PIN"
    git -C "$RIC_DIR" fetch --quiet origin
    git -C "$RIC_DIR" checkout --quiet "$RIC_PIN"
  fi
}

# E2 nodes the RIC knows, as "inventoryName connectionStatus" lines. Asked of
# e2mgr's REST API from inside the RIC network (it is not published).
e2_nodes() {
  compose exec -T python_xapp_runner python3 -c '
import json, os, urllib.request
ip = os.environ.get("E2MGR_IP", "10.0.2.11")
with urllib.request.urlopen(f"http://{ip}:3800/v1/nodeb/states", timeout=5) as r:
    for n in json.load(r):
        print(n.get("inventoryName"), n.get("connectionStatus"))
' 2>/dev/null
}

sctp_hint() {
  # Docker can only publish an SCTP port if the host kernel has SCTP.
  if [ "$(uname -s)" = Linux ] && ! grep -qw '^sctp' /proc/modules 2>/dev/null \
     && [ ! -d /sys/module/sctp ]; then
    echo "WARNING: the sctp kernel module is not loaded; E2 cannot be published."
    echo "  sudo modprobe sctp      (persist: echo sctp | sudo tee /etc/modules-load.d/sctp.conf)"
  fi
}

case "${1:-}" in
  up)
    checkout
    sctp_hint
    [ "$ARM64" = 1 ] && amd64_hint
    compose up -d --build
    echo
    echo "RIC up at pin ${RIC_PIN:0:12}. E2 (SCTP) on ${RIC_E2_BIND:-0.0.0.0}:36421."
    echo "Point the gNB at it — gnb.yml e2: addr: <this host>, bind_addr: <gnb host>, port: 36421"
    echo "The RIC refuses a reconnect within 60 s of a disconnect (E2 SETUP FAILURE): wait."
    ;;
  status)
    compose ps --format 'table {{.Service}}\t{{.Status}}'
    echo
    echo "E2 nodes known to the RIC:"
    e2_nodes | sed 's/^/  /' || echo "  (none, or e2mgr not answering)"
    ;;
  kpm)
    out="$ROOT_DIR/runs/ric"
    mkdir -p "$out"
    log="$out/kpm-$(date -u +%Y%m%dT%H%M%SZ)-style$STYLE.log"
    node=${E2_NODE_ID:-$(e2_nodes | awk '$2 == "CONNECTED" {print $1; exit}')}
    if [ -z "$node" ]; then
      echo "ERROR: no E2 node is CONNECTED to the RIC. Check the gNB's e2: block and" >&2
      echo "  'bash deploy/lab/ric/ric.sh status'; after a gNB restart wait 60 s." >&2
      exit 1
    fi
    echo "==> kpm_mon_xapp node=$node style=$STYLE ue_ids=$UE_IDS metrics=$METRICS"
    echo "    -> $log (${KPM_SECONDS:+stops after ${KPM_SECONDS}s, }Ctrl-C to stop)"
    stop_in_runner kpm_mon_xapp.py
    trap 'stop_in_runner kpm_mon_xapp.py' EXIT
    since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    # -u: unbuffered, or nothing reaches tee until the buffer fills.
    compose exec -T python_xapp_runner ${KPM_SECONDS:+timeout "$KPM_SECONDS"} \
      python3 -u ./kpm_mon_xapp.py --e2_node_id="$node" \
      --kpm_report_style="$STYLE" --ue_ids="$UE_IDS" --metrics="$METRICS" 2>&1 | tee "$log" || true
    echo "--- submgr verdict for this monitor:"
    docker logs --since "$since" ric_submgr 2>&1 | grep -o 'subscription failed.*' \
      || echo "    no subscription failure"
    ;;
  xapp)
    : "${INFRA_HOST:?set INFRA_HOST: the admin (:8099) and logging (:8000) host, as the RIC network reaches it}"
    detach=""
    [ "${2:-}" = "-d" ] && detach="-d"
    # websockets carries the admin client; the runner image does not have it.
    # 13.x is the last release that supports the runner's Python 3.8.
    compose exec -T python_xapp_runner python3 -c 'import websockets' 2>/dev/null \
      || compose exec -T python_xapp_runner pip install -q "websockets>=12,<14"
    stop_in_runner mec_cast_xapp
    echo "==> mec-cast xApp (osc adapter) -> admin ws://$INFRA_HOST:8099, logging http://$INFRA_HOST:8000"
    echo "    defaults: style=$STYLE ue_ids=$UE_IDS metrics=$METRICS (run params override)"
    compose exec $detach \
      -e PYTHONPATH=/opt/mec-cast/ran/py/src:/opt/mec-cast/ran/xapp/src:/opt/mec-cast/admin_client \
      -e E2_ADAPTER=osc \
      -e OSC_XAPPS_DIR=/opt/xApps \
      -e ADMIN_URL="${ADMIN_URL-ws://$INFRA_HOST:8099/ws/node}" \
      -e LOGGING_URL="http://$INFRA_HOST:8000" \
      -e KPM_LOGGING="${KPM_LOGGING:-1}" \
      -e RUNS_DIR=/runs \
      -e XAPP_HOST="${XAPP_HOST:-$(hostname -s)}" \
      -e ZONE="${ZONE:-}" \
      -e VCS_REF="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" \
      -e XAPP_CAPABILITIES="${XAPP_CAPABILITIES:-kpm_monitor,rc_control}" \
      -e KPM_STYLE="${STYLE}" \
      -e KPM_METRICS="${METRICS}" \
      -e KPM_UE_IDS="${UE_IDS}" \
      ${E2_NODE_ID:+-e E2_NODE_ID="$E2_NODE_ID"} \
      python_xapp_runner python3 -m mec_cast_xapp
    ;;
  down)
    compose down
    ;;
  *)
    sed -n '2,27p' "$0"
    exit 2
    ;;
esac
