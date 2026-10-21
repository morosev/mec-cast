#!/bin/bash
# FlexRIC (OpenAirInterface) as the near-RT RIC for the E2 path, beside
# oran-sc-ric.sh. Same subcommands, same variables: switching RIC is a change
# of script name. One RIC per host at a time -- both listen on E2 SCTP 36421.
#
#   bash deploy/lab/ric/flexric.sh up        # build the image if needed, start nearRT-RIC
#   bash deploy/lab/ric/flexric.sh status    # containers, connected E2 nodes
#   KPM_SECONDS=30 bash deploy/lab/ric/flexric.sh kpm   # KPM monitor through the bridge
#   INFRA_HOST=10.0.0.10 bash deploy/lab/ric/flexric.sh xapp      # the mec-cast xApp
#   INFRA_HOST=10.0.0.10 bash deploy/lab/ric/flexric.sh xapp -d   # ...detached
#   bash deploy/lab/ric/flexric.sh emu       # FlexRIC's emulated gNB: E2 without a radio
#   bash deploy/lab/ric/flexric.sh logs [ric|xapp|emu]
#   bash deploy/lab/ric/flexric.sh down
#
# Runs on the infra role. FlexRIC (OAI CSSL v1.0) is built from its pinned tag
# inside the mec-cast-flexric image (deploy/lab/ric/flexric/Dockerfile) and
# never vendored. Everything runs with host networking: the gNB reaches the
# RIC's E2 port on this host, and the xApp reaches the admin and the logging
# service as this host does. No amd64 emulation: FlexRIC builds natively, on
# arm64 too.
#
# The xApp is the same mec_cast_xapp as on oran-sc-ric, with E2_ADAPTER=flexric:
# it runs mec-cast-e2-bridge (ran/xapp/flexric), a small FlexRIC xApp in C,
# because FlexRIC's Python SDK has no KPM or RC.
#
# Variables:
#   FLEXRIC_BIND  address the RIC listens on for E2 and E42 (default 0.0.0.0: every
#                 interface, as oran-sc-ric publishes on all)
#   STYLE         KPM report style, 4 (every UE in the slice, per UE) or 1 (node)
#   METRICS       KPM metrics (default: the five OCUDU 26.10 serves)
#   UE_IDS        E2 UE ids, for the xApp's run params ("0")
#   E2_NODE_ID    default: the first connected node
#   KPM_SECONDS   kpm only: stop after this long (default until Ctrl-C)
#   FLEXRIC_SLICE PLMN:SST:SD a PRB quota names (00101:1:1). FlexRIC builds the
#                 RC message itself, so this is configurable here, unlike oran-sc-ric
#   INFRA_HOST    xapp only, required: where the admin (:8099) and logging (:8000) are
#   KPM_LOGGING, XAPP_CAPABILITIES, XAPP_HOST, ZONE   xapp only, as oran-sc-ric.sh
#   FLEXRIC_E2AP, FLEXRIC_KPM   image build: E2AP_V3 / KPM_V3_00 (what OCUDU speaks)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$HERE/../../.." && pwd)"
IMAGE=${FLEXRIC_IMAGE:-mec-cast-flexric}
RIC=mec-cast-flexric-ric
XAPP=mec-cast-flexric-xapp
EMU=mec-cast-flexric-emu
FLEXRIC_BIND=${FLEXRIC_BIND:-0.0.0.0}
# Style 4: per UE, every UE whose S-NSSAI has the slice's SST. FlexRIC builds
# styles 1 and 4; the single-UE style 2 that oran-sc-ric.sh uses is not one.
STYLE=${STYLE:-4}
METRICS=${METRICS:-DRB.UEThpDl,DRB.UEThpUl,DRB.RlcSduTransmittedVolumeDL,DRB.RlcSduTransmittedVolumeUL,DRB.RlcPacketDropRateDl}
UE_IDS=${UE_IDS:-0}
FLEXRIC_SLICE=${FLEXRIC_SLICE:-00101:1:1}

# runs/ is often created by the mec-cast containers, as root.
if ! mkdir -p "$ROOT_DIR/runs/ric" 2>/dev/null; then
  echo "ERROR: cannot write $ROOT_DIR/runs (owned by $(stat -c %U "$ROOT_DIR/runs" 2>/dev/null || echo '?'))." >&2
  echo "  The containers created it as root. Give it back once: sudo chown \$USER: $ROOT_DIR/runs" >&2
  exit 1
fi

# FlexRIC reads NEAR_RIC_IP from its config: where the RIC binds, and where an
# xApp or an agent on this host finds it.
CONF_DIR="$ROOT_DIR/runs/ric/flexric"
mkdir -p "$CONF_DIR"
write_conf() {  # file ip
  printf '[NEAR-RIC]\nNEAR_RIC_IP = %s\n\n[XAPP]\nDB_DIR = /tmp/\n' "$2" > "$1"
}
write_conf "$CONF_DIR/ric.conf" "$FLEXRIC_BIND"
write_conf "$CONF_DIR/local.conf" 127.0.0.1

running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }

build() {
  if [ -n "${FLEXRIC_REBUILD:-}" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building $IMAGE (FlexRIC at its pin; several minutes the first time)"
    docker build -f "$HERE/flexric/Dockerfile" -t "$IMAGE" \
      --build-arg E2AP_VERSION="${FLEXRIC_E2AP:-E2AP_V3}" \
      --build-arg KPM_VERSION="${FLEXRIC_KPM:-KPM_V3_00}" \
      "$ROOT_DIR"
  fi
}

# The other RIC holds E2's port: refuse rather than start a second listener
# that binds nothing, or steals the gNB from a run that is using the first.
other_ric() {
  if docker ps --format '{{.Names}}' | grep -qx 'ric_e2term'; then
    echo "ERROR: oran-sc-ric is running here (ric_e2term holds E2 SCTP 36421)." >&2
    echo "  Stop it first: bash deploy/lab/ric/oran-sc-ric.sh down" >&2
    exit 1
  fi
}

sctp_hint() {
  if [ "$(uname -s)" = Linux ] && ! grep -qw '^sctp' /proc/modules 2>/dev/null \
     && [ ! -d /sys/module/sctp ]; then
    echo "WARNING: the sctp kernel module is not loaded; E2 cannot listen."
    echo "  sudo modprobe sctp      (persist: echo sctp | sudo tee /etc/modules-load.d/sctp.conf)"
  fi
}

# The code is mounted, as oran-sc-ric.sh mounts it into that RIC's runner.
CODE_MOUNTS=(
  -v "$ROOT_DIR/ran/py:/opt/mec-cast/ran/py:ro"
  -v "$ROOT_DIR/ran/xapp:/opt/mec-cast/ran/xapp:ro"
  -v "$ROOT_DIR/ros2/src/mec_cast_admin_client/mec_cast_admin_client:/opt/mec-cast/admin_client/mec_cast_admin_client:ro"
  -v "$CONF_DIR/local.conf:/usr/local/etc/flexric/flexric.conf:ro"
)

# A one-off container on the RIC's host network, with the code.
oneoff() { docker run --rm --init --network host "${CODE_MOUNTS[@]}" -e FLEXRIC_SLICE="$FLEXRIC_SLICE" "$IMAGE" "$@"; }

first_node() {
  oneoff python3 -m mec_cast_xapp.adapters.flexric --nodes 2>/dev/null | awk '$2 == "CONNECTED" {print $1; exit}'
}

case "${1:-}" in
  up)
    other_ric
    sctp_hint
    build
    if running "$RIC"; then
      echo "==> $RIC is already running"
    else
      docker rm -f "$RIC" >/dev/null 2>&1 || true
      docker run -d --name "$RIC" --network host --restart unless-stopped \
        -v "$CONF_DIR/ric.conf:/usr/local/etc/flexric/flexric.conf:ro" \
        "$IMAGE" stdbuf -oL nearRT-RIC >/dev/null
    fi
    echo
    echo "FlexRIC up ($(docker inspect -f '{{index .Config.Labels "mec-cast.flexric.ref"}} {{index .Config.Labels "mec-cast.flexric.e2ap"}} {{index .Config.Labels "mec-cast.flexric.kpm"}}' "$IMAGE")). E2 (SCTP) on $FLEXRIC_BIND:36421, E42 on :36422."
    echo "Point the gNB at it — gnb.yml e2: addr: <this host>, bind_addr: <gnb host>, port: 36421"
    echo "Start it before the gNB: the gNB sets up E2 when it starts."
    ;;
  status)
    for c in "$RIC" "$XAPP" "$EMU"; do
      state=$(docker inspect -f '{{.State.Status}} since {{.State.StartedAt}}' "$c" 2>/dev/null) || state="not running"
      printf '  %-24s %s\n' "$c" "$state"
    done
    echo
    echo "E2 nodes known to the RIC:"
    if running "$RIC"; then
      oneoff python3 -m mec_cast_xapp.adapters.flexric --nodes 2>/dev/null | sed 's/^/  /' \
        || echo "  (none, or the RIC not answering on E42)"
    else
      echo "  (the RIC is not running: flexric.sh up)"
    fi
    ;;
  kpm)
    running "$RIC" || { echo "ERROR: FlexRIC is not running: bash deploy/lab/ric/flexric.sh up" >&2; exit 1; }
    node=${E2_NODE_ID:-$(first_node)}
    if [ -z "$node" ]; then
      echo "ERROR: no E2 node is CONNECTED to FlexRIC. Check the gNB's e2: block and" >&2
      echo "  'bash deploy/lab/ric/flexric.sh status'." >&2
      exit 1
    fi
    log="$ROOT_DIR/runs/ric/kpm-flexric-$(date -u +%Y%m%dT%H%M%SZ)-style$STYLE.log"
    echo "==> KPM monitor via mec-cast-e2-bridge: node=$node style=$STYLE metrics=$METRICS"
    echo "    -> $log (${KPM_SECONDS:+stops after ${KPM_SECONDS}s, }Ctrl-C to stop)"
    oneoff python3 -m mec_cast_xapp.adapters.flexric --node "$node" --style "$STYLE" \
      --metrics "$METRICS" --ue-ids "$UE_IDS" ${KPM_SECONDS:+--seconds "$KPM_SECONDS"} 2>&1 | tee "$log" || true
    ;;
  xapp)
    : "${INFRA_HOST:?set INFRA_HOST: the admin (:8099) and logging (:8000) host, as this host reaches it}"
    running "$RIC" || { echo "ERROR: FlexRIC is not running: bash deploy/lab/ric/flexric.sh up" >&2; exit 1; }
    docker rm -f "$XAPP" >/dev/null 2>&1 || true
    mode=(--rm)
    [ "${2:-}" = "-d" ] && mode=(-d --restart unless-stopped)
    [ -t 0 ] && [ "${2:-}" != "-d" ] && mode+=(-it)
    echo "==> mec-cast xApp (flexric adapter) -> admin ws://$INFRA_HOST:8099, logging http://$INFRA_HOST:8000"
    echo "    defaults: style=$STYLE ue_ids=$UE_IDS metrics=$METRICS slice=$FLEXRIC_SLICE (run params override)"
    docker run "${mode[@]}" --init --name "$XAPP" --network host "${CODE_MOUNTS[@]}" \
      -v "$ROOT_DIR/runs:/runs" \
      -e E2_ADAPTER=flexric \
      -e FLEXRIC_SLICE="$FLEXRIC_SLICE" \
      -e ADMIN_URL="${ADMIN_URL-ws://$INFRA_HOST:8099/ws/node}" \
      -e LOGGING_URL="http://$INFRA_HOST:8000" \
      -e KPM_LOGGING="${KPM_LOGGING:-1}" \
      -e RUNS_DIR=/runs \
      -e XAPP_HOST="${XAPP_HOST:-$(hostname -s)}" \
      -e ZONE="${ZONE:-}" \
      -e VCS_REF="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" \
      -e XAPP_CAPABILITIES="${XAPP_CAPABILITIES:-kpm_monitor,rc_control}" \
      -e KPM_STYLE="$STYLE" \
      -e KPM_METRICS="$METRICS" \
      -e KPM_UE_IDS="$UE_IDS" \
      ${E2_NODE_ID:+-e E2_NODE_ID="$E2_NODE_ID"} \
      "$IMAGE" python3 -m mec_cast_xapp
    [ "${2:-}" = "-d" ] && echo "    detached; its log: bash deploy/lab/ric/flexric.sh logs xapp"
    ;;
  emu)
    running "$RIC" || { echo "ERROR: FlexRIC is not running: bash deploy/lab/ric/flexric.sh up" >&2; exit 1; }
    docker rm -f "$EMU" >/dev/null 2>&1 || true
    docker run -d --name "$EMU" --network host \
      -v "$CONF_DIR/local.conf:/usr/local/etc/flexric/flexric.conf:ro" \
      "$IMAGE" stdbuf -oL emu_agent_gnb >/dev/null
    echo "==> FlexRIC's emulated gNB is up: an E2 node with synthetic KPM and an RC that"
    echo "    accepts controls. It proves the RIC, the bridge and the xApp, not a real gNB."
    echo "    Stop it before connecting a real gNB: docker rm -f $EMU"
    ;;
  logs)
    case "${2:-ric}" in
      ric) docker logs -f "$RIC" ;;
      xapp) docker logs -f "$XAPP" ;;
      emu) docker logs -f "$EMU" ;;
      *) echo "logs: ric, xapp or emu" >&2; exit 2 ;;
    esac
    ;;
  down)
    docker rm -f "$XAPP" "$EMU" "$RIC" >/dev/null 2>&1 || true
    echo "FlexRIC stopped (the image $IMAGE is kept; FLEXRIC_REBUILD=1 flexric.sh up rebuilds it)."
    ;;
  *)
    sed -n '2,38p' "$0"
    exit 2
    ;;
esac
