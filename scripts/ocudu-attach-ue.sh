#!/bin/bash
# Put the local-5g UE container behind srsUE and route the edge over 5G.
#
#   bash scripts/ocudu-attach-ue.sh [-c CONTAINER] [-a N6_ADDR] [-m MTU] [-w SECONDS]
#
#   -c CONTAINER  the UE container          (default mec5g-lidar-client-1)
#   -a N6_ADDR    edge address on the N6 side (default 10.53.1.1)
#   -m MTU        tun_srsue MTU             (default 1400: room for GTP-U)
#   -w SECONDS    how long to wait for srsUE (default 300)
#
# Safe to run any time, and it is the one step to repeat after ANY of:
#   * srsUE restarted (including every gNB restart, which needs one): the new
#     tun_srsue comes up without the route or the MTU;
#   * the UE container recreated (`up --build`, a changed knob, down/up): it
#     has a new netns, and /run/netns/ue1 still names the old one.
#
# What it does:
#   1. points /run/netns/ue1 at the container's network namespace — the name
#      srsUE's `[gw] netns = ue1` opens at start. If it had to move it, srsUE
#      must be (re)started afterwards, gNB first; the script says so and waits.
#   2. waits until tun_srsue exists in that namespace with an IPv4 address,
#      i.e. srsUE has a PDU session;
#   3. sets the MTU, disables IPv6 (router solicitations the UPF rejects), and
#      routes N6_ADDR/32 via tun_srsue. Only that address: the container's
#      default route stays on the compose bridge for logging and admin.
#
# The UE container itself waits for exactly this route before it opens its
# Zenoh session (deploy/compose/local-5g.yml), so it starts on its own.
# Procedure: docs/guides/local-5g-ocudu.md.
set -euo pipefail

CONTAINER=mec5g-lidar-client-1
N6_ADDR=10.53.1.1
MTU=1400
WAIT=300
NS=ue1
TUN=tun_srsue

while getopts "c:a:m:w:h" opt; do
  case "$opt" in
    c) CONTAINER=$OPTARG ;;
    a) N6_ADDR=$OPTARG ;;
    m) MTU=$OPTARG ;;
    w) WAIT=$OPTARG ;;
    h) sed -n '2,29p' "$0"; exit 0 ;;
    *) exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi

in_ns() { ip netns exec "$NS" "$@"; }

# --- 1. the namespace ------------------------------------------------------
PID=$(docker inspect -f '{{if .State.Running}}{{.State.Pid}}{{end}}' "$CONTAINER" 2>/dev/null || true)
if [ -z "$PID" ] || [ "$PID" = 0 ]; then
  echo "ERROR: container $CONTAINER is not running." >&2
  echo "  Start the stack first (docs/guides/local-5g-ocudu.md, step 2.2)." >&2
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

# --- 2. wait for srsUE -----------------------------------------------------
has_ip() { in_ns ip -4 -o addr show dev "$TUN" 2>/dev/null | grep -q inet; }

if ! has_ip; then
  echo
  if [ "$MOVED" = 1 ]; then
    echo "    srsUE must start AFTER this, so its $TUN lands in the new namespace."
  else
    echo "    No $TUN with an address in $NS yet."
  fi
  echo "    Restart the radio now, in this order:"
  echo "      Ctrl-C srsUE, Ctrl-C the gNB"
  echo "      start the gNB      -> wait for '==== gNB started ==='"
  echo "      start srsUE        -> wait for 'PDU Session Establishment successful'"
  echo
  printf "==> waiting up to %ss for %s" "$WAIT" "$TUN"
  for _ in $(seq 1 "$WAIT"); do
    has_ip && break
    printf "."; sleep 1
  done
  echo
  if ! has_ip; then
    echo "ERROR: no $TUN with an IPv4 address in $NS after ${WAIT}s." >&2
    echo "  Check srsUE's console and /tmp/ue.log; see Troubleshooting in the guide." >&2
    exit 1
  fi
elif [ "$MOVED" = 1 ]; then
  # Cannot happen with a fresh netns, but a stale symlink target could carry one.
  echo "    note: $TUN already present in the new namespace"
fi

UE_IP=$(in_ns ip -4 -o addr show dev "$TUN" | awk '{print $4}' | cut -d/ -f1)
echo "==> srsUE is attached: $TUN $UE_IP"

# --- 3. MTU and route ------------------------------------------------------
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
  echo "  ip route | grep 10.45.0.0/16   (expect: via 10.53.1.2; ~/ran-configs/up.sh adds it)" >&2
fi

echo
echo "The UE node starts within ~2 s:  docker logs -f $CONTAINER  -> '5G link up'"
