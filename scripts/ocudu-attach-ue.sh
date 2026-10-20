#!/bin/bash
# Bring the local-5g radio up behind the UE container, and route the edge over it.
#
#   bash scripts/ocudu-attach-ue.sh [-R] [-n] [-S] [-c CONTAINER] [-a N6_ADDR] [-m MTU] [-w SECONDS]
#
#   (default)     prepare the host, point ue1 at the UE container, (re)start the
#                 gNB and srsUE if the link is not already up, route the edge
#   -R            restart the radio even if the link is up: after editing
#                 gnb_zmq.yaml / ue_zmq.conf, or to retry E2 after the RIC's 60 s
#   -n            do not touch the radio: print what to restart and wait for it
#                 (the old, manual behaviour)
#   -S            stop the radio (srsUE, then the gNB) and reset ue1; nothing else
#   -c CONTAINER  the UE container          (default mec5g-lidar-client-1)
#   -a N6_ADDR    edge address on the N6 side (default 10.53.1.1)
#   -m MTU        tun_srsue MTU             (default 1400: room for GTP-U)
#   -w SECONDS    how long to wait for the PDU session (default 120; 300 with -n)
#
# The radio runs in two root tmux sessions, so the consoles stay usable:
#   sudo tmux attach -t gnb      sudo tmux attach -t srsue   (type t for the trace)
#   detach with Ctrl-b d. Console copies: /tmp/gnb.console, /tmp/srsue.console.
#
# Where things are (as in docs/guides/local-5g-ocudu.md; override if yours differ):
#   GNB_BIN    ~/src/ocudu/build/apps/gnb/gnb      GNB_CONF  ~/ran-configs/gnb_zmq.yaml
#   SRSUE_BIN  ~/src/srsRAN_4G/build/srsue/src/srsue  UE_CONF ~/ran-configs/ue_zmq.conf
#   OCUDU_DOCKER  ~/src/ocudu/docker  (the Open5GS core's compose project)
#
# Safe to run any time, and it is the one step to repeat after ANY of:
#   * the UE container was recreated (`make up-local-5g` after a new commit,
#     RAN=1, a changed knob, down/up): it has a new netns, and ue1 names the old;
#   * the gNB or srsUE stopped or was restarted by hand: the new tun_srsue comes
#     up without the route or the MTU;
#   * a host reboot (it redoes the per-boot setup too).
#
# What it does:
#   1. per-boot host setup, idempotent: SCTP, the Open5GS core and the UE-pool
#      return route (what ~/ran-configs/up.sh does);
#   2. points /run/netns/ue1 at the container's network namespace -- the name
#      srsUE's `[gw] netns = ue1` opens at start, which is how tun_srsue lands
#      inside the container;
#   3. if the link is not up (or -R): stops srsUE and the gNB, starts the gNB,
#      waits for "gNB started", starts srsUE, waits for the PDU session. ZMQ has
#      no resynchronisation, so the two always restart together, gNB first;
#   4. sets the MTU, disables IPv6 (router solicitations the UPF rejects), and
#      routes N6_ADDR/32 via tun_srsue. Only that address: the container's
#      default route stays on the compose bridge for logging and admin.
#
# Start the RIC (deploy/lab/ric/ric.sh up) BEFORE this when you use E2: the gNB
# sets up E2 when it starts. The UE container waits for exactly the route of
# step 4 before it opens its Zenoh session (deploy/compose/local-5g.yml), so it
# starts on its own. Procedure: docs/guides/local-5g-ocudu.md.
set -euo pipefail

CONTAINER=mec5g-lidar-client-1
N6_ADDR=10.53.1.1
MTU=1400
WAIT=
MODE=auto
NS=ue1
TUN=tun_srsue

# Resolved as the invoking user, before sudo, so ~ means their home.
export GNB_BIN=${GNB_BIN:-$HOME/src/ocudu/build/apps/gnb/gnb}
export GNB_CONF=${GNB_CONF:-$HOME/ran-configs/gnb_zmq.yaml}
export SRSUE_BIN=${SRSUE_BIN:-$HOME/src/srsRAN_4G/build/srsue/src/srsue}
export UE_CONF=${UE_CONF:-$HOME/ran-configs/ue_zmq.conf}
export OCUDU_DOCKER=${OCUDU_DOCKER:-$HOME/src/ocudu/docker}

while getopts "RnSc:a:m:w:h" opt; do
  case "$opt" in
    R) MODE=restart ;;
    n) MODE=manual ;;
    S) MODE=stop ;;
    c) CONTAINER=$OPTARG ;;
    a) N6_ADDR=$OPTARG ;;
    m) MTU=$OPTARG ;;
    w) WAIT=$OPTARG ;;
    h) sed -n '2,50p' "$0"; exit 0 ;;
    *) exit 2 ;;
  esac
done
WAIT=${WAIT:-$([ "$MODE" = manual ] && echo 300 || echo 120)}

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi

in_ns() { ip netns exec "$NS" "$@"; }
has_ip() { in_ns ip -4 -o addr show dev "$TUN" 2>/dev/null | grep -q inet; }

# --- the radio processes ---------------------------------------------------
# SIGINT, as Ctrl-C would: the gNB writes its E2AP pcap only on a clean exit.
stop_proc() {
  local name=$1
  pgrep -x "$name" >/dev/null || return 0
  echo "==> stopping $name"
  pkill -INT -x "$name" || true
  for _ in $(seq 1 15); do pgrep -x "$name" >/dev/null || return 0; sleep 1; done
  echo "    $name ignored SIGINT for 15 s; killing it"
  pkill -KILL -x "$name" || true
  sleep 1
}

stop_radio() {
  stop_proc srsue                     # UE first: it would only log the gNB vanishing
  stop_proc gnb
  tmux kill-session -t srsue 2>/dev/null || true
  tmux kill-session -t gnb 2>/dev/null || true
}

# One root tmux session per process. The command is typed into a shell rather
# than given to new-session, so the console is piped to a file from the first
# line, and a process that dies leaves its last words on screen.
start_in_tmux() {
  local name=$1 dir=$2 cmd=$3 log=/tmp/$1.console
  : > "$log"
  tmux new-session -d -s "$name" -x 200 -y 50
  tmux pipe-pane -o -t "$name" "cat >> $log"
  tmux send-keys -t "$name" "cd '$dir' && $cmd" Enter
}

wait_for_line() {  # name pattern seconds
  local name=$1 pattern=$2 secs=$3 log=/tmp/$1.console seen=0
  for _ in $(seq 1 "$secs"); do
    grep -q "$pattern" "$log" 2>/dev/null && return 0
    # Gone only counts once it was there: the console holds the shell's
    # prompt before the process has even started.
    if pgrep -x "$name" >/dev/null; then
      seen=1
    elif [ "$seen" = 1 ]; then
      break                            # it started and already exited
    fi
    sleep 1
  done
  echo "ERROR: $name did not print '$pattern' within ${secs}s. Its console:" >&2
  tail -n 15 "$log" >&2 || true
  echo "  Full output: $log, or: sudo tmux attach -t $name" >&2
  return 1
}

start_radio() {
  for f in "$GNB_BIN" "$SRSUE_BIN"; do
    [ -x "$f" ] || { echo "ERROR: $f is not built (set GNB_BIN / SRSUE_BIN if it lives elsewhere)." >&2; exit 1; }
  done
  for f in "$GNB_CONF" "$UE_CONF"; do
    [ -r "$f" ] || { echo "ERROR: no $f (set GNB_CONF / UE_CONF)." >&2; exit 1; }
  done
  command -v tmux >/dev/null || { echo "ERROR: tmux is not installed: sudo apt install -y tmux" >&2; exit 1; }

  stop_radio
  echo "==> starting the gNB      (sudo tmux attach -t gnb)"
  start_in_tmux gnb "$(dirname "$GNB_BIN")" "./$(basename "$GNB_BIN") -c '$GNB_CONF'"
  wait_for_line gnb "gNB started" 60
  echo "==> starting srsUE        (sudo tmux attach -t srsue; type t for the trace)"
  start_in_tmux srsue "$(dirname "$SRSUE_BIN")" "./$(basename "$SRSUE_BIN") '$UE_CONF'"
}

if [ "$MODE" = stop ]; then
  stop_radio
  if ! ip netns exec "$NS" true 2>/dev/null || [ -L "/run/netns/$NS" ]; then
    rm -f "/run/netns/$NS"; ip netns add "$NS"
    echo "==> $NS is a plain namespace again"
  fi
  exit 0
fi

# --- 1. the host ------------------------------------------------------------
modprobe sctp
if ! ip addr | grep -q "10.53.1.1/"; then
  echo "==> starting the Open5GS core ($OCUDU_DOCKER)"
  (cd "$OCUDU_DOCKER" && docker compose up -d 5gc)
  for _ in $(seq 1 30); do ip addr | grep -q "10.53.1.1/" && break; sleep 1; done
  ip addr | grep -q "10.53.1.1/" || { echo "ERROR: the core's bridge 10.53.1.1 never came up." >&2; exit 1; }
fi
ip route | grep -q "^10.45.0.0/16" || ip route add 10.45.0.0/16 via 10.53.1.2   # downlink return path

# --- 2. the namespace -------------------------------------------------------
PID=$(docker inspect -f '{{if .State.Running}}{{.State.Pid}}{{end}}' "$CONTAINER" 2>/dev/null || true)
if [ -z "$PID" ] || [ "$PID" = 0 ]; then
  echo "ERROR: container $CONTAINER is not running." >&2
  echo "  Start the stack first: make up-local-5g (docs/guides/local-5g-ocudu.md, 2.2)." >&2
  exit 1
fi

if [ "$(ip netns identify "$PID" 2>/dev/null)" = "$NS" ]; then
  echo "==> $NS already is $CONTAINER's namespace (pid $PID)"
  MOVED=0
else
  echo "==> pointing $NS at $CONTAINER's namespace (pid $PID)"
  ip netns del "$NS" 2>/dev/null || true
  rm -f "/run/netns/$NS"
  mkdir -p /run/netns
  ln -sfT "/proc/$PID/ns/net" "/run/netns/$NS"
  if [ "$(ip netns identify "$PID" 2>/dev/null)" != "$NS" ]; then
    echo "ERROR: $NS still does not resolve to pid $PID" >&2
    exit 1
  fi
  MOVED=1
fi
in_ns sysctl -qw net.ipv6.conf.all.disable_ipv6=1 || true

# --- 3. the radio -----------------------------------------------------------
# A tun_srsue in this namespace is proof srsUE started after ue1 pointed here.
# Without one -- or with -R -- the radio restarts; srsUE cannot be moved.
if [ "$MODE" = restart ] || ! has_ip; then
  if [ "$MODE" = manual ]; then
    echo
    echo "    Restart the radio now, in this order:"
    echo "      Ctrl-C srsUE, Ctrl-C the gNB"
    echo "      start the gNB      -> wait for '==== gNB started ==='"
    echo "      start srsUE        -> wait for 'PDU Session Establishment successful'"
    echo
  else
    [ "$MOVED" = 1 ] && echo "    srsUE must start after this, so its $TUN lands in the new namespace."
    start_radio
  fi
  printf "==> waiting up to %ss for the PDU session" "$WAIT"
  for _ in $(seq 1 "$WAIT"); do
    has_ip && break
    printf "."; sleep 1
  done
  echo
  if ! has_ip; then
    echo "ERROR: no $TUN with an IPv4 address in $NS after ${WAIT}s." >&2
    [ "$MODE" != manual ] && tail -n 15 /tmp/srsue.console >&2 2>/dev/null
    echo "  srsUE: /tmp/srsue.console and /tmp/ue.log; see Troubleshooting in the guide." >&2
    exit 1
  fi
fi

UE_IP=$(in_ns ip -4 -o addr show dev "$TUN" | awk '{print $4}' | cut -d/ -f1)
echo "==> srsUE is attached: $TUN $UE_IP"

# --- 4. MTU and route -------------------------------------------------------
in_ns ip link set "$TUN" mtu "$MTU"
# TUN is layer 3: no gateway or neighbour resolution needed. `replace` makes
# a re-run harmless.
in_ns ip route replace "$N6_ADDR/32" dev "$TUN"

ROUTE=$(in_ns ip route get "$N6_ADDR")
case "$ROUTE" in
  *"dev $TUN"*) echo "==> $N6_ADDR now routes over 5G: $ROUTE" ;;
  *) echo "ERROR: $N6_ADDR does not route via $TUN: $ROUTE" >&2; exit 1 ;;
esac

if in_ns ping -c 2 -W 2 "$N6_ADDR" >/dev/null 2>&1; then
  echo "==> $N6_ADDR answers across the 5G link"
else
  echo "WARNING: $N6_ADDR does not answer. Is the host's return route there?" >&2
  echo "  ip route | grep 10.45.0.0/16   (expect: via 10.53.1.2)" >&2
fi

echo
echo "The UE node starts within ~2 s:  docker logs -f $CONTAINER  -> '5G link up'"
echo "Consoles: sudo tmux attach -t gnb | srsue   (Ctrl-b d to detach)"
