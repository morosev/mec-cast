#!/bin/bash
# Deploy one mec-cast role to a lab host.
#
#   bash deploy/lab/deploy.sh <role> <user@host>
#
# Roles:
#   ue      LiDAR + ROS2 lidar client, behind the 5G modem
#   edge    Zenoh router + edge ingest node (the MEC application server)
#   infra   Logging service + PostgreSQL
#   gnb     srsRAN metrics collector (runs alongside the gNB)
#
# Deliberately simple: rsync the repo, build there, run compose. For a
# four-host lab this beats a configuration-management system — it stays
# debuggable at 2am in the lab, which is when you will be using it.
set -euo pipefail

ROLE=${1:-}
TARGET=${2:-}

if [ -z "$ROLE" ] || [ -z "$TARGET" ]; then
  sed -n '2,16p' "$0"
  exit 2
fi

case "$ROLE" in
  ue|edge|infra|gnb) ;;
  *) echo "ERROR: unknown role '$ROLE'"; exit 2 ;;
esac

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
REMOTE_DIR="~/mec-cast"

echo "==> Syncing repo to $TARGET (excluding third_party, runs, target)"
rsync -az --delete \
  --exclude '.git/' \
  --exclude 'third_party/' \
  --exclude 'target/' \
  --exclude 'runs/' \
  --exclude '**/node_modules/' \
  --exclude '**/.venv/' \
  "$ROOT_DIR/" "$TARGET:$REMOTE_DIR/"

echo "==> Starting role '$ROLE' on $TARGET"
# shellcheck disable=SC2029
ssh "$TARGET" "cd $REMOTE_DIR && \
  docker compose -f deploy/lab/compose.$ROLE.yml up -d --build"

echo "==> Verifying PTP on $TARGET"
# shellcheck disable=SC2029
ssh "$TARGET" "bash -s" < "$ROOT_DIR/deploy/lab/ptp/verify-ptp.sh" || \
  echo "WARNING: PTP verification failed — cross-host metrics will be untrustworthy."

echo "==> Role '$ROLE' deployed to $TARGET"
