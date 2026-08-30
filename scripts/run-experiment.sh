#!/bin/bash
# Run one measured experiment and record everything needed to reproduce it.
#
#   bash scripts/run-experiment.sh [options]
#
#   -d SECONDS    run duration            (default 60)
#   -n POINTS     points per cloud        (default 5000)
#   -r HZ         publish rate            (default 10.0)
#   -l DELAY      netem one-way delay     (default 20ms)
#   -j JITTER     netem jitter            (default 5ms)
#   -L LOSS       netem loss              (default 0.5%)
#   -t TAG        free-text label for the run
#
# Writes runs/<run_id>/run.json describing the exact configuration and the
# git SHA of the repo and every submodule. That file is what makes a number
# in a paper traceable back to the code and settings that produced it.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

DURATION=60
NUM_POINTS=5000
RATE_HZ=10.0
NETEM_DELAY=20ms
NETEM_JITTER=5ms
NETEM_LOSS=0.5%
SEED=42
TAG=""

while getopts "d:n:r:l:j:L:s:t:h" opt; do
  case "$opt" in
    d) DURATION=$OPTARG ;;
    n) NUM_POINTS=$OPTARG ;;
    r) RATE_HZ=$OPTARG ;;
    l) NETEM_DELAY=$OPTARG ;;
    j) NETEM_JITTER=$OPTARG ;;
    L) NETEM_LOSS=$OPTARG ;;
    s) SEED=$OPTARG ;;
    t) TAG=$OPTARG ;;
    h) sed -n '2,18p' "$0"; exit 0 ;;
    *) exit 2 ;;
  esac
done

# uuidgen lives in uuid-runtime, which is not installed everywhere. The
# kernel source always exists on Linux. Without this fallback RUN_ID silently
# became empty: run.json landed in runs/ while the CSVs went to runs/dev-run/.
RUN_ID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)
if [ -z "$RUN_ID" ]; then
  echo "ERROR: could not generate a RUN_ID" >&2
  exit 1
fi
RUN_DIR="runs/$RUN_ID"
mkdir -p "$RUN_DIR"

COMPOSE="docker compose -f deploy/compose/logging.yml -f deploy/compose/local.yml"

export RUN_ID NUM_POINTS RATE_HZ SEED NETEM_DELAY NETEM_JITTER NETEM_LOSS

# Normally the services/logging submodule supplies this. Only a clone made
# without --recurse-submodules needs the sibling-working-tree fallback.
if [ ! -f services/logging/pyproject.toml ] && \
   [ -f ../mec-cast-logging-service/pyproject.toml ]; then
  export MECLOG_BUILD_CONTEXT=../../../mec-cast-logging-service
fi

cleanup() {
  echo "==> Stopping producers so recorders drain and flush"
  $COMPOSE stop -t 15 lidar-client edge >/dev/null 2>&1 || true
  $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- Provenance -----------------------------------------------------------
cat > "$RUN_DIR/run.json" <<JSON
{
  "run_id": "$RUN_ID",
  "tag": "$TAG",
  "started_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host": "$(hostname)",
  "duration_s": $DURATION,
  "workload": {
    "num_points": $NUM_POINTS,
    "rate_hz": $RATE_HZ,
    "seed": $SEED,
    "modality": "pointcloud"
  },
  "network": {
    "impairment": "netem",
    "delay": "$NETEM_DELAY",
    "jitter": "$NETEM_JITTER",
    "loss": "$NETEM_LOSS"
  },
  "transport": "rmw_zenoh_cpp",
  "git": {
    "repo": "$(git rev-parse HEAD 2>/dev/null || echo unknown)",
    "dirty": $(test -n "$(git status --porcelain 2>/dev/null)" && echo true || echo false),
    "submodules": $(git submodule status 2>/dev/null | awk '{printf "%s\"%s\": \"%s\"", (NR>1?", ":"") , $2, $1}' | sed 's/^/{/; s/$/}/' || echo '{}')
  }
}
JSON

echo "==> Run $RUN_ID  ($TAG)"
echo "    ${NUM_POINTS} pts @ ${RATE_HZ} Hz for ${DURATION}s"
echo "    netem: delay=$NETEM_DELAY jitter=$NETEM_JITTER loss=$NETEM_LOSS"

# --- Capacity check -------------------------------------------------------
# netem's packet loss does not drop frames directly on a reliable transport:
# it drives congestion control to lower the sending rate, and the publisher's
# KEEP_LAST(10) queue then sheds the backlog — measured at 98% frame loss for
# 30k points at 0.4% loss, while the same impairment at 3k points loses
# nothing.
#
# CAVEAT: the Mathis bound below models TCP, and the transport is now Reliable
# UDP (ADR-0006). It is kept as an order-of-magnitude over-subscription
# warning, NOT as a capacity prediction for the configured transport, and it
# is due a rework. It is also not a link capacity in any case: nothing caps
# bandwidth in the compose topology, and the same workload runs loss-free
# unimpaired.
#
# Warn before burning a run rather than after. This never blocks: an
# over-capacity run is a legitimate thing to measure when it is deliberate.
capacity_check() {
  local delay_ms loss_pct demand capacity
  delay_ms=$(printf '%s' "$NETEM_DELAY" | tr -dc '0-9.')
  loss_pct=$(printf '%s' "$NETEM_LOSS" | tr -dc '0-9.')
  [ -z "$delay_ms" ] && delay_ms=0
  [ -z "$loss_pct" ] && loss_pct=0

  # 12 bytes per point (three float32s), plus envelope overhead we ignore.
  demand=$(awk -v n="$NUM_POINTS" -v r="$RATE_HZ" 'BEGIN{printf "%.0f", n*12*r}')

  if awk -v p="$loss_pct" 'BEGIN{exit !(p > 0)}'; then
    capacity=$(awk -v d="$delay_ms" -v p="$loss_pct" \
      'BEGIN{rtt=(2*d)/1000; if(rtt<=0) rtt=0.001; printf "%.0f", 1448/(rtt*sqrt(p/100))}')
    if awk -v a="$demand" -v b="$capacity" 'BEGIN{exit !(a > b)}'; then
      echo
      echo "    WARNING: workload looks larger than the impaired link can carry."
      echo "      offered   ~$(awk -v v="$demand"   'BEGIN{printf "%.2f", v/1048576}') MB/s"
      echo "      rough limit ~$(awk -v v="$capacity" 'BEGIN{printf "%.2f", v/1048576}') MB/s" \
           "(Mathis/TCP model, ${delay_ms}ms delay, ${loss_pct}% loss)"
      echo "      Expect heavy frame loss at the edge and queue-inflated latency."
      echo "      Lower -n / -r, or -L, if you wanted a measurable glass-to-glass number."
      echo
    fi
  fi
}
capacity_check

$COMPOSE up -d --build

echo "==> Waiting for logging service"
for _ in $(seq 1 90); do
  if curl -sf http://localhost:8000/health/ready >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "==> Streaming for ${DURATION}s"
sleep "$DURATION"

$COMPOSE stop -t 15 lidar-client edge

echo "==> Done. Artifacts:"
echo "    $RUN_DIR/run.json"
ls -la "$RUN_DIR"/*/samples.csv 2>/dev/null || echo "    (no CSV — check container logs)"
echo
echo "Query snapshots:"
echo "  curl -sG http://localhost:8000/api/v1/logs --data-urlencode 'trace_id=$RUN_ID' | jq"
