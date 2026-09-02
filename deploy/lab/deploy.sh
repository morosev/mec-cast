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
# INFRA_HOST used to fail on the far side as "required variable is missing",
# which reads like the variable was never set at all:
#
#   INFRA_HOST=10.0.0.5 bash deploy/lab/deploy.sh edge iconic@edge-host
#
# Required per role: edge needs INFRA_HOST; ue and gnb need EDGE_HOST and
# INFRA_HOST; infra needs none.
#
# INFRA_HOST names the host, not a service: it serves the logging service on
# :8000 and, since the admin moved off the edge, the control plane on :8099.
# It was called LOGGING_HOST until 2026-08-30, which named only half of what
# it addresses.
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
# Accept the old name for now. Without this an existing shell or runbook
# fails on a variable the operator never set and has never heard of, which
# reads as a bug in the script rather than as a rename.
if [ -z "${INFRA_HOST:-}" ] && [ -n "${LOGGING_HOST:-}" ]; then
  INFRA_HOST="$LOGGING_HOST"
  export INFRA_HOST
  echo "NOTE: LOGGING_HOST is deprecated — it now addresses the admin too." >&2
  echo "  Use INFRA_HOST. Continuing with INFRA_HOST=$INFRA_HOST." >&2
fi

case "$ROLE" in
  edge)    REQUIRED="INFRA_HOST" ;;
  ue|gnb)  REQUIRED="EDGE_HOST INFRA_HOST" ;;
  infra)   REQUIRED="" ;;
esac
for v in $REQUIRED; do
  if [ -z "${!v:-}" ]; then
    echo "ERROR: $v is required for role '$ROLE' and is not set in this shell." >&2
    echo "  $v=<address> bash deploy/lab/deploy.sh $ROLE $TARGET" >&2
    exit 2
  fi
done

# --- build contexts this role needs ----------------------------------------
# rsync copies the working tree, and an uninitialised submodule is an empty
# directory: it transfers happily and fails on the far side as
# `"/src": not found` from buildkit, which names neither the submodule nor
# this machine. The remote cannot fix it either — .git is excluded from the
# sync, so there is no repository there to update. Check the sending side.
case "$ROLE" in
  infra) NEEDS_CONTEXT="services/logging services/admin" ;;
  *)     NEEDS_CONTEXT="" ;;
esac
for c in $NEEDS_CONTEXT; do
  # MECLOG_BUILD_CONTEXT overrides the logging context with a sibling
  # checkout; when it is set, that path is what compose will build.
  if [ "$c" = "services/logging" ] && [ -n "${MECLOG_BUILD_CONTEXT:-}" ]; then
    continue
  fi
  if [ ! -f "$ROOT_DIR/$c/pyproject.toml" ]; then
    echo "ERROR: $c is empty; role '$ROLE' builds an image from it." >&2
    # Only services/logging is a submodule. Telling someone to run
    # `git submodule update` for services/admin sends them after a submodule
    # that does not exist, while the real cause -- a broken or partial
    # checkout -- goes unmentioned.
    if [ "$c" = "services/logging" ]; then
      echo "  On THIS machine:  git submodule update --init --recursive" >&2
      echo "  ...or point at a sibling checkout:" >&2
      echo "    MECLOG_BUILD_CONTEXT=../../../mec-cast-logging-service \\" >&2
      echo "      bash deploy/lab/deploy.sh $ROLE $TARGET" >&2
    else
      echo "  $c is part of this repository, not a submodule, so this is a" >&2
      echo "  broken or partial checkout. On THIS machine:  git status $c" >&2
    fi
    exit 2
  fi
done

# Optional knobs: forwarded when set, left to the compose file's default when
# not. Keep in step with the ${...} references in deploy/lab/compose.*.yml.
OPTIONAL="POSTGRES_PASSWORD MECLOG_BUILD_CONTEXT METRICS_PORT RUN_ID \
          RENDER_SINK PUBLISH_RESULT RESULT_RELIABILITY RESULT_QOS_DEPTH \
          PATTERN NUM_POINTS RATE_HZ SEED ADMIN_URL \
          LIDAR_INSTANCES RENDER_INSTANCES VIEWER_HOST CELL \
          BACKUP_DIR BACKUP_EVERY BACKUP_KEEP BACKUP_CHECK_EVERY"

# Built as `NAME=value ...` for the remote command line. printf %q quotes each
# value so a password or a path with spaces survives the trip through ssh,
# which re-parses its argument as a shell command.
# `${!v+x}` is true when v is SET, including when it is set to empty —
# unlike `${!v:-}`, which cannot tell empty from absent. That distinction is
# load-bearing for ADMIN_URL: `ADMIN_URL= deploy.sh ue …` is how an operator
# says "no control plane, use RUN_ID", and dropping the empty value silently
# gave them the opposite.
REMOTE_ENV=""
for v in $REQUIRED $OPTIONAL; do
  [ -n "${!v+x}" ] && REMOTE_ENV="$REMOTE_ENV $v=$(printf '%q' "${!v}")"
done

echo "==> Syncing repo to $TARGET (excluding third_party, runs, target)"
# .run-env is excluded because it is the operator's own config for THIS host
# -- the shell function and this machine's EDGE_HOST/INFRA_HOST. It is
# gitignored, so it is not in the source tree, and --delete would remove it on
# every deploy. Silently: the next `compose ps` on that host then fails with an
# interpolation error naming a variable, rather than naming the deploy that
# deleted the file which set it.
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.run-env' \
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
# Two roles can share a host, so the stamp accumulates them rather than
# recording only the last one deployed. Reading the previous value first
# means a host that has had `infra` and then `edge` deployed reports both,
# which is what `make version` on that host should say.
PREV_ROLES=$(ssh "$TARGET" "grep -h '^DEPLOYED_ROLES=' $REMOTE_DIR/.deployed-version 2>/dev/null | tail -1 | cut -d= -f2-" || true)
ALL_ROLES=$(printf '%s\n%s\n' "$PREV_ROLES" "$ROLE" | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ' ')
ALL_ROLES=${ALL_ROLES% }
# shellcheck disable=SC2029
ssh "$TARGET" "cat > $REMOTE_DIR/.deployed-version" <<EOF
# Written by deploy/lab/deploy.sh. Do not edit; it is overwritten every deploy.
DEPLOYED_VERSION=$DEPLOY_VER
DEPLOYED_SHA=$DEPLOY_SHA
# The role this deploy pushed, and every role ever deployed to this host.
# Both are kept: the first answers "what did I just do", the second "what
# is this machine".
DEPLOYED_ROLE=$ROLE
DEPLOYED_ROLES=$ALL_ROLES
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
