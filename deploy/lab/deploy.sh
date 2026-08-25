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
#
# Variables are read from THIS shell and forwarded to the remote compose. An
# export here does not otherwise survive the ssh hop, so a locally-set
# LOGGING_HOST used to fail on the far side as "required variable is missing",
# which reads like the variable was never set at all:
#
#   LOGGING_HOST=10.0.0.5 bash deploy/lab/deploy.sh edge iconic@edge-host
#
# Required per role: edge needs LOGGING_HOST; ue and gnb need EDGE_HOST and
# LOGGING_HOST; infra needs none.
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

# --- environment forwarded to the remote compose ---------------------------
# Fail here rather than after a two-minute rsync and image build: compose
# would report it as a missing variable on the far side, which is true but
# points at the wrong machine.
case "$ROLE" in
  edge)    REQUIRED="LOGGING_HOST" ;;
  ue|gnb)  REQUIRED="EDGE_HOST LOGGING_HOST" ;;
  infra)   REQUIRED="" ;;
esac
for v in $REQUIRED; do
  if [ -z "${!v:-}" ]; then
    echo "ERROR: $v is required for role '$ROLE' and is not set in this shell." >&2
    echo "  $v=<address> bash deploy/lab/deploy.sh $ROLE $TARGET" >&2
    exit 2
  fi
done

# Optional knobs: forwarded when set, left to the compose file's default when
# not. Keep in step with the ${...} references in deploy/lab/compose.*.yml.
OPTIONAL="POSTGRES_PASSWORD MECLOG_BUILD_CONTEXT METRICS_PORT RUN_ID \
          RENDER_SINK PUBLISH_RESULT RESULT_RELIABILITY RESULT_QOS_DEPTH \
          PATTERN NUM_POINTS RATE_HZ SEED ADMIN_URL"

# Built as `NAME=value ...` for the remote command line. printf %q quotes each
# value so a password or a path with spaces survives the trip through ssh,
# which re-parses its argument as a shell command.
REMOTE_ENV=""
for v in $REQUIRED $OPTIONAL; do
  [ -n "${!v:-}" ] && REMOTE_ENV="$REMOTE_ENV $v=$(printf '%q' "${!v}")"
done

echo "==> Syncing repo to $TARGET (excluding third_party, runs, target)"
rsync -az --delete \
  --exclude '.git/' \
  --exclude 'third_party/' \
  --exclude 'target/' \
  --exclude 'runs/' \
  --exclude '**/node_modules/' \
  --exclude '**/.venv/' \
  "$ROOT_DIR/" "$TARGET:$REMOTE_DIR/"

# .git is excluded above, so a push-deployed host cannot answer "what version
# am I running?" from git. Leave a stamp instead, written by the deploy rather
# than maintained by hand, and let scripts/version.sh fall back to it. Hosts
# the admin `git pull`s keep their .git and never consult this file.
echo "==> Stamping deployed version"
DEPLOY_SHA=$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)
DEPLOY_VER=$(git -C "$ROOT_DIR" describe --tags --match 'platform-v*' --always --dirty \
             2>/dev/null || echo unknown)
# shellcheck disable=SC2029
ssh "$TARGET" "cat > $REMOTE_DIR/.deployed-version" <<EOF
# Written by deploy/lab/deploy.sh. Do not edit; it is overwritten every deploy.
DEPLOYED_VERSION=$DEPLOY_VER
DEPLOYED_SHA=$DEPLOY_SHA
DEPLOYED_ROLE=$ROLE
DEPLOYED_AT=$(date -Iseconds)
DEPLOYED_FROM=$(whoami)@$(hostname)
EOF

echo "==> Starting role '$ROLE' on $TARGET"
[ -n "$REMOTE_ENV" ] && echo "    forwarding:$(echo "$REMOTE_ENV" | tr ' ' '\n' | grep -oE '^[A-Z_]+' | tr '\n' ' ')"
# shellcheck disable=SC2029
ssh "$TARGET" "cd $REMOTE_DIR && \
  env $REMOTE_ENV docker compose -f deploy/lab/compose.$ROLE.yml up -d --build"

echo "==> Verifying PTP on $TARGET"
# shellcheck disable=SC2029
ssh "$TARGET" "bash -s" < "$ROOT_DIR/deploy/lab/ptp/verify-ptp.sh" || \
  echo "WARNING: PTP verification failed — cross-host metrics will be untrustworthy."

echo "==> Role '$ROLE' deployed to $TARGET"
echo
# The deploy is not finished when compose returns; it is finished when the host
# can tell you what it is running. Print that here so the admin sees it without
# a second login, and sees it for the host rather than for their workstation.
# shellcheck disable=SC2029
ssh "$TARGET" "cd $REMOTE_DIR && bash scripts/version.sh" || \
  echo "WARNING: could not read deployed version from $TARGET."
