#!/bin/bash
# Verify that this host's clock is PTP-disciplined well enough for
# cross-machine one-way latency measurement.
#
#   bash deploy/lab/ptp/verify-ptp.sh [max_offset_ns]
#   bash deploy/lab/ptp/verify-ptp.sh --peer <host> [max_offset_ns]
#
# PTP_IFACE=<iface>   resolve the PHC from the NIC ptp4l disciplines
# PTP_DEVICE=/dev/ptpN  name it directly (wins over PTP_IFACE)
#
# --peer is the check that actually matters and the only one that can fail
# for the right reason. Everything else here is local: it asks whether this
# host tracks its own PHC, which says nothing about whether that PHC agrees
# with the PHC at the other end of the measurement.
#
# Exits non-zero when the host is not adequately synchronised. Run this on
# BOTH endpoints before trusting any cross-host number: without it, the
# network and glass-to-glass metrics silently measure clock skew instead of
# latency, and nothing downstream can tell the difference.
set -uo pipefail

PEER=""
if [ "${1:-}" = "--peer" ]; then
  PEER=${2:-}
  shift 2
  [ -n "$PEER" ] || { echo "--peer needs a host" >&2; exit 2; }
fi
# An argument this script does not understand must be an error, never a
# shrug. An older copy given --peer parsed it as the offset threshold, never
# compared anything, and printed "cross-host one-way metrics are valid" --
# a stale checkout answering a question it cannot even ask. Unknown input
# now exits 2 instead of silently narrowing the check.
if [ $# -gt 1 ]; then
  echo "unexpected arguments: $*" >&2
  echo "usage: verify-ptp.sh [--peer <host>] [max_offset_ns]" >&2
  exit 2
fi
if [ -n "${1:-}" ] && ! [ "$1" -eq "$1" ] 2>/dev/null; then
  echo "max_offset_ns must be an integer, got: $1" >&2
  echo "usage: verify-ptp.sh [--peer <host>] [max_offset_ns]" >&2
  echo "(a --peer flag this copy does not support means it is out of date:" >&2
  echo " git pull, then re-run)" >&2
  exit 2
fi

MAX_OFFSET_NS=${1:-1000}
FAIL=0

note() { printf '  %s\n' "$1"; }

# Which PHC to check. /dev/ptp0 is a guess, not an answer: index 0 is
# whichever NIC the driver registered first. On a four-PHC host, ptp4l
# disciplined ptp2 (ens3f0), chrony followed ptp3 (ens3f1, free-running) and
# the containers were handed ptp0 -- three different clocks, every one of them
# reporting healthily about itself.
PTP_IFACE=${PTP_IFACE:-}
if [ -z "${PTP_DEVICE:-}" ] && [ -n "$PTP_IFACE" ] && command -v ethtool >/dev/null 2>&1; then
  IDX=$(ethtool -T "$PTP_IFACE" 2>/dev/null | awk '/PTP Hardware Clock:/ {print $4}')
  [ -n "${IDX:-}" ] && PTP_DEVICE=/dev/ptp$IDX
fi
PTP_DEVICE=${PTP_DEVICE:-/dev/ptp0}

echo "==> PTP hardware clock"
if [ -e "$PTP_DEVICE" ]; then
  NAME=$(cat "/sys/class/ptp/${PTP_DEVICE#/dev/}/clock_name" 2>/dev/null || true)
  note "$PTP_DEVICE present${NAME:+ ($NAME)}"
  if [ -z "${PTP_IFACE:-}" ] && [ "$PTP_DEVICE" = /dev/ptp0 ]; then
    note "Checking /dev/ptp0 by default. If ptp4l runs on a specific NIC,"
    note "set PTP_IFACE=<iface> (or PTP_DEVICE) so this checks the clock it"
    note "disciplines rather than whichever one enumerated first."
  fi
else
  note "MISSING $PTP_DEVICE — no PTP hardware clock at that path."
  note "Metrics will fall back to CLOCK_REALTIME (NTP-grade, ~1-5 ms)."
  FAIL=1
fi

# `systemctl is-active ptp4l` does NOT match `ptp4l@ens3f0.service`. A
# templated unit is the normal way to run ptp4l -- one instance per interface
# -- so the bare name reported a host with four hours of uptime and 70 ns
# offset as "NOT active", which is the worst kind of wrong: it sends an
# operator to fix a working machine, and teaches them to distrust the check.
unit_active() {
  systemctl is-active --quiet "$1" 2>/dev/null && return 0
  systemctl list-units --no-legend --state=active "$1@*.service" 2>/dev/null \
    | grep -q . && return 0
  return 1
}

# A VM taking time from its hypervisor via ptp_kvm has a real PHC and no
# ptp4l, because nothing here speaks PTP on the wire -- the guest reads the
# host's clock directly. Demanding ptp4l would fail a correctly configured
# guest. Accepting it silently is worse: see the warning.
PHC_NAME=$(cat "/sys/class/ptp/${PTP_DEVICE#/dev/}/clock_name" 2>/dev/null || true)
KVM_PHC=0
if [ -e /dev/ptp_kvm ] || [ "$PHC_NAME" = "KVM virtual PTP" ]; then
  KVM_PHC=1
fi

echo "==> ptp4l (syncs the NIC clock to the grandmaster)"
if [ "$KVM_PHC" -eq 1 ]; then
  note "ptp_kvm — this host is a VM reading its hypervisor's clock."
  note "No ptp4l is expected or wanted here."
  note ""
  note "THIS CLOCK IS ONLY AS GOOD AS THE HYPERVISOR'S CLOCK."
  note "Nothing on this host can detect that. The hypervisor must be"
  note "disciplined to the SAME grandmaster as every other measuring host;"
  note "if it is on plain NTP, this guest inherits that error in full while"
  note "still reporting a flawless local offset and ptp.reliable: true."
  note "Verify with:  --peer <other-measuring-host>"
elif unit_active ptp4l; then
  note "ptp4l active"
else
  note "ptp4l NOT active. Start with: sudo systemctl start ptp4l"
  note "  (or the per-interface unit: sudo systemctl start ptp4l@<iface>)"
  FAIL=1
fi

# What matters is that CLOCK_REALTIME is disciplined FROM the PHC, not which
# daemon does it. phc2sys is one way; chrony with a PHC refclock is another,
# and is what a host running chrony for its other time sources will use.
# Checking only for phc2sys called a correctly-disciplined host broken.
echo "==> CLOCK_REALTIME disciplined from the NIC clock"
if unit_active phc2sys; then
  note "phc2sys active"
elif command -v chronyc >/dev/null 2>&1 \
     && chronyc sources 2>/dev/null | grep -qE '^#\*.*PHC'; then
  note "chrony is disciplining from a PHC refclock (selected source)"
  chronyc sources 2>/dev/null | grep -E '^#\*' | sed 's/^/    /'
else
  note "NOTHING is disciplining CLOCK_REALTIME from the PHC."
  note "  phc2sys:  sudo systemctl start phc2sys"
  note "  ...or configure chrony with a PHC refclock and confirm it is"
  note "  the SELECTED source: chronyc sources | grep PHC"
  FAIL=1
fi

# The identity of the clock this host ultimately follows. This is the thing
# that has to match between two endpoints, and the only way to check it
# without inferring it from an offset. Two hosts can both be locked, both
# report tens of ns, and follow DIFFERENT grandmasters -- which is invisible
# in every local indicator and shows up only as a constant skew in the data.
gm_identity() {
  command -v pmc >/dev/null 2>&1 || return 1
  pmc -u -b 0 'GET PARENT_DATA_SET' 2>/dev/null \
    | awk '/grandmasterIdentity/ {print $2; exit}'
}
gm_domain() {
  command -v pmc >/dev/null 2>&1 || return 1
  pmc -u -b 0 'GET DEFAULT_DATA_SET' 2>/dev/null \
    | awk '/domainNumber/ {print $2; exit}'
}

echo "==> Time root (must MATCH on both endpoints)"
if [ "$KVM_PHC" -eq 1 ]; then
  note "This guest's root is its HYPERVISOR — it cannot be read from here."
  note "Run this section on the hypervisor and compare with the other"
  note "measuring hosts:  sudo pmc -u -b 0 'GET PARENT_DATA_SET'"
else
  GM=$(gm_identity || true)
  DOMAIN=$(gm_domain || true)
  if [ -n "${GM:-}" ]; then
    note "grandmasterIdentity: $GM"
    [ -n "${DOMAIN:-}" ] && note "domainNumber:        $DOMAIN"
    note "This must be IDENTICAL on every measuring host. Two grandmasters,"
    note "or one grandmaster in two domains, gives two islands of perfect"
    note "time offset by whatever they disagree by."
  elif [ "$(id -u)" -ne 0 ]; then
    note "pmc needs root to read ptp4l's socket — re-run with sudo to see"
    note "which grandmaster this host follows."
  else
    note "Could not read the grandmaster identity."
  fi
fi

echo "==> Measured offset"
# Reading this number and not judging it is how an 11.15 s gap sat in plain
# view. chrony reports nanoseconds against the refclock it was pointed at, so
# it cannot notice that the refclock is the wrong device -- it is measuring
# itself against the thing that is wrong. Comparing the DISCIPLINED PHC to
# CLOCK_REALTIME is the one check that catches it.
if command -v phc_ctl >/dev/null 2>&1 && [ -e "$PTP_DEVICE" ]; then
  CMP=$(phc_ctl "$PTP_DEVICE" cmp 2>&1 || true)
  echo "$CMP" | sed 's/^/  /'
  SYS_OFF=$(echo "$CMP" | sed -n 's/.*offset from CLOCK_REALTIME is \(-\?[0-9]\+\)ns.*/\1/p')
  if [ -n "${SYS_OFF:-}" ]; then
    ABS_SYS=${SYS_OFF#-}
    # 1 ms: far above any real PTP discipline, far below the seconds that a
    # wrong-device or free-running clock produces. Anything here is structural,
    # not drift.
    if [ "$ABS_SYS" -gt 1000000 ]; then
      note ""
      note "CLOCK_REALTIME IS NOT FOLLOWING $PTP_DEVICE (${SYS_OFF} ns apart)."
      note "Something disciplines the system clock from a DIFFERENT source."
      note "Check what chrony is actually pointed at — its refid is a free"
      note "text label and may read PHC0 while naming another device:"
      note "  grep refclock /etc/chrony/chrony.conf"
      note "  ethtool -T <iface> | grep 'PTP Hardware Clock'"
      FAIL=1
    fi
  fi
fi
if command -v pmc >/dev/null 2>&1; then
  OFFSET=$(pmc -u -b 0 'GET CURRENT_DATA_SET' 2>/dev/null \
           | awk '/offsetFromMaster/ {print $2}' | head -1)
  if [ -n "${OFFSET:-}" ]; then
    note "offsetFromMaster: ${OFFSET} ns (threshold ${MAX_OFFSET_NS} ns)"
    # Integer compare on the absolute value.
    ABS=${OFFSET#-}
    ABS=${ABS%%.*}
    if [ "${ABS:-999999}" -gt "$MAX_OFFSET_NS" ]; then
      note "OFFSET EXCEEDS THRESHOLD — one-way metrics are not trustworthy."
      FAIL=1
    fi
  else
    if [ "$(id -u)" -ne 0 ]; then
      note "Could not read offsetFromMaster — pmc needs root for ptp4l's"
      note "socket. Re-run with sudo for this figure; ptp4l's own journal"
      note "carries it either way: journalctl -u 'ptp4l*' -n1"
    else
      note "Could not read offsetFromMaster (is this host a PTP slave?)"
    fi
  fi
fi

# Two hosts can each be perfectly disciplined to a DIFFERENT root and both
# report success above: one to a PTP grandmaster, one to a hypervisor on NTP.
# Every local indicator stays green and every cross-host delay is wrong by
# their disagreement -- which is how a run recorded network_ns=-11231127381
# with every gauge on the page green. Only comparing the two ends finds it.
if [ -n "$PEER" ]; then
  echo "==> Agreement with $PEER"
  # Comparing a host to itself always says "agrees" (or, without a loopback
  # key, fails on ssh and looks like a sync problem). Either way it answers
  # nothing: the question is whether the TWO ENDS of a measurement share a
  # grandmaster, and one host cannot disagree with itself. Caught rather than
  # allowed, because `--peer <the host I am typing on>` is the natural mistake.
  PEER_HOST=${PEER#*@}
  SELF=0
  case "$PEER_HOST" in
    localhost | 127.0.0.1 | ::1) SELF=1 ;;
  esac
  for n in "$(hostname 2>/dev/null)" "$(hostname -s 2>/dev/null)" \
           "$(hostname -f 2>/dev/null)" $(hostname -I 2>/dev/null); do
    [ -n "$n" ] && [ "$PEER_HOST" = "$n" ] && SELF=1
  done
  if [ "$SELF" -eq 1 ]; then
    note "$PEER is THIS host — a host cannot disagree with itself."
    note "Run this from one endpoint naming the OTHER, e.g. on the UE host:"
    note "  bash deploy/lab/ptp/verify-ptp.sh --peer <edge-host>"
    FAIL=1
    PEER=""
  fi
fi

if [ -n "$PEER" ]; then
  T0=$(date +%s%N)
  REMOTE=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$PEER" 'date +%s%N' 2>/dev/null || true)
  T1=$(date +%s%N)
  if ! [ "${REMOTE:-}" -eq "${REMOTE:-}" ] 2>/dev/null; then
    note "Could not read the clock on $PEER over ssh (BatchMode: keys only)."
    FAIL=1
  else
    RTT=$(( T1 - T0 ))
    SKEW=$(( REMOTE - (T0 + T1) / 2 ))
    ABS=${SKEW#-}
    # The round trip bounds how precisely this can measure. It is milliseconds
    # over ssh, which is useless for validating PTP and ample for catching the
    # failure that actually happens: two hosts seconds apart.
    LIMIT=$(( RTT / 2 + 1000000 ))
    note "skew ${SKEW} ns (± $(( RTT / 2 )) ns from a ${RTT} ns round trip)"
    if [ "$ABS" -gt "$LIMIT" ]; then
      note "THE TWO HOSTS DISAGREE BY MORE THAN THE MEASUREMENT ERROR."
      note "They are disciplined to different time roots. One-way delays"
      note "between them measure that gap, not the network."
      FAIL=1
    else
      note "agree to within ssh measurement error — no gross skew"
      note "(this rules out ms-and-worse skew; it cannot confirm ns sync)"
    fi
  fi
fi

echo
if [ "$FAIL" -eq 0 ] && [ -z "$PEER" ]; then
  echo "PTP OK locally — this host tracks its own PHC."
  echo "This does NOT establish that it agrees with the other endpoint."
  echo "Run with --peer <host> before trusting any cross-host number."
elif [ "$FAIL" -eq 0 ]; then
  echo "PTP OK — cross-host one-way metrics are valid on this node."
else
  echo "PTP NOT OK — treat cross-host one-way metrics as unreliable."
  echo "Local-only metrics (encode, decode, jitter buffer, processing) stay valid."
fi
exit "$FAIL"
