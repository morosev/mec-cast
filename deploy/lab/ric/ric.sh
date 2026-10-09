#!/bin/bash
# The O-RAN SC near-RT RIC for the E2 path (ADR-0010), pinned and outside
# this repository.
#
#   bash deploy/lab/ric/ric.sh up        # clone at the pin if needed, start
#   bash deploy/lab/ric/ric.sh status    # containers, E2 port, connected E2 nodes
#   bash deploy/lab/ric/ric.sh kpm       # stock KPM monitor xApp, logged under runs/ric/
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
# STYLE (kpm report style, 5), METRICS (kpm metrics), UE_IDS (style 5, "0"),
# E2_NODE_ID (default: the first node the RIC reports connected).
set -euo pipefail

RIC_REPO=https://github.com/srsran/oran-sc-ric.git
# 2025-10-14 "xApps: add e2sm-ccc module and an example xApp using it".
# Bump deliberately, and say so in the commit: the xApp is written against it.
RIC_PIN=621ade26251f69a4ad079ba98bb708d8e5aeeb98

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$HERE/../../.." && pwd)"
RIC_DIR=${RIC_DIR:-$HOME/oran-sc-ric}
STYLE=${STYLE:-5}
# The KPMs srsRAN's E2 agent exposes beyond its dummy CQI/RSRP/RSRQ.
METRICS=${METRICS:-DRB.UEThpDl,DRB.UEThpUl,DRB.RlcSduTransmittedVolumeDL,DRB.RlcSduTransmittedVolumeUL,DRB.RlcPacketDropRateDl,DRB.PacketSuccessRateUlgNBUu}
UE_IDS=${UE_IDS:-0}

# The RIC images, and the .debs the xApp runner image installs, are amd64
# only. On an arm64 host (Apple Silicon, for local testing) build and run them
# under emulation rather than fail on "package architecture (amd64) does not
# match system (arm64)". The lab's infra host is x86-64 and needs none of it.
case "$(uname -m)" in
  arm64|aarch64) export DOCKER_DEFAULT_PLATFORM=${DOCKER_DEFAULT_PLATFORM:-linux/amd64} ;;
esac

export MECCAST_ROOT="$ROOT_DIR"
mkdir -p "$ROOT_DIR/runs"

compose() { docker compose --project-directory "$RIC_DIR" -f "$RIC_DIR/docker-compose.yml" -f "$HERE/compose.override.yml" "$@"; }

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
    echo "    -> $log (Ctrl-C to stop)"
    compose exec python_xapp_runner ./kpm_mon_xapp.py --e2_node_id="$node" \
      --kpm_report_style="$STYLE" --ue_ids="$UE_IDS" --metrics="$METRICS" 2>&1 | tee "$log"
    ;;
  xapp)
    : "${INFRA_HOST:?set INFRA_HOST: the admin (:8099) and logging (:8000) host, as the RIC network reaches it}"
    detach=""
    [ "${2:-}" = "-d" ] && detach="-d"
    # websockets carries the admin client; the runner image does not have it.
    # 13.x is the last release that supports the runner's Python 3.8.
    compose exec -T python_xapp_runner python3 -c 'import websockets' 2>/dev/null \
      || compose exec -T python_xapp_runner pip install -q "websockets>=12,<14"
    echo "==> mec-cast xApp (osc adapter) -> admin ws://$INFRA_HOST:8099, logging http://$INFRA_HOST:8000"
    compose exec $detach \
      -e PYTHONPATH=/opt/mec-cast/ran/py/src:/opt/mec-cast/ran/xapp/src:/opt/mec-cast/admin_client \
      -e E2_ADAPTER=osc \
      -e OSC_XAPPS_DIR=/opt/xApps \
      -e ADMIN_URL="${ADMIN_URL-ws://$INFRA_HOST:8099/ws/node}" \
      -e LOGGING_URL="http://$INFRA_HOST:8000" \
      -e RUNS_DIR=/runs \
      -e XAPP_HOST="${XAPP_HOST:-$(hostname -s)}" \
      -e CELL="${CELL:-}" \
      -e VCS_REF="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" \
      -e XAPP_CAPABILITIES="${XAPP_CAPABILITIES:-kpm_monitor,rc_control}" \
      -e KPM_STYLE="${STYLE}" \
      ${E2_NODE_ID:+-e E2_NODE_ID="$E2_NODE_ID"} \
      python_xapp_runner python3 -m mec_cast_xapp
    ;;
  down)
    compose down
    ;;
  *)
    sed -n '2,25p' "$0"
    exit 2
    ;;
esac
