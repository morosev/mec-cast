#!/bin/bash
# Verify that this host's clock is PTP-disciplined well enough for
# cross-machine one-way latency measurement.
#
#   bash deploy/lab/ptp/verify-ptp.sh [max_offset_ns]
#
# Exits non-zero when the host is not adequately synchronised. Run this on
# BOTH endpoints before trusting any cross-host number: without it, the
# network and glass-to-glass metrics silently measure clock skew instead of
# latency, and nothing downstream can tell the difference.
set -uo pipefail

MAX_OFFSET_NS=${1:-1000}
FAIL=0

note() { printf '  %s\n' "$1"; }

echo "==> PTP hardware clock"
if [ -e /dev/ptp0 ]; then
  note "/dev/ptp0 present"
else
  note "MISSING /dev/ptp0 — no PTP hardware clock on this host."
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

echo "==> ptp4l (syncs the NIC clock to the grandmaster)"
if unit_active ptp4l; then
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

echo "==> Measured offset"
if command -v phc_ctl >/dev/null 2>&1 && [ -e /dev/ptp0 ]; then
  phc_ctl /dev/ptp0 cmp 2>&1 | sed 's/^/  /' || true
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
    note "Could not read offsetFromMaster (is this host a PTP slave?)"
  fi
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "PTP OK — cross-host one-way metrics are valid on this node."
else
  echo "PTP NOT OK — treat cross-host one-way metrics as unreliable."
  echo "Local-only metrics (encode, decode, jitter buffer, processing) stay valid."
fi
exit "$FAIL"
