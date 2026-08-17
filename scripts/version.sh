#!/bin/bash
# What is actually deployed on THIS host.
#
#   bash scripts/version.sh        # or: make version
#
# Nothing here is read from a file that someone had to remember to update.
# It inspects git, compose and the running containers, because the moment you
# need this answer is exactly the moment a written-down version would be
# wrong — a half-finished deploy, a host nobody pulled on.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

BOLD=$(tput bold 2>/dev/null || true)
DIM=$(tput dim 2>/dev/null || true)
RST=$(tput sgr0 2>/dev/null || true)

say() { printf "  %-13s %s\n" "$1" "$2"; }

echo "${BOLD}mec-cast — deployed state on $(hostname)${RST}"
echo

# ---------------------------------------------------------------- source
# Two ways source arrives on a host, and they answer this question
# differently. `git pull` leaves a checkout that can speak for itself.
# deploy/lab/deploy.sh rsyncs without .git and leaves .deployed-version
# instead. Prefer git; fall back to the stamp; never invent an answer.
if ! SHORT=$(git rev-parse --short HEAD 2>/dev/null); then
  if [ -f .deployed-version ]; then
    # shellcheck disable=SC1091
    . ./.deployed-version
    SHORT=${DEPLOYED_SHA:0:7}
    say "version" "${DEPLOYED_VERSION:-unknown}  ${DIM}(rsync deploy, no git here)${RST}"
    say "commit" "$SHORT"
    say "deployed" "${DEPLOYED_AT:-?} by ${DEPLOYED_FROM:-?} as role ${DEPLOYED_ROLE:-?}"
    NO_GIT=1
  else
    say "version" "${BOLD}UNKNOWN${RST} — no git checkout and no .deployed-version"
    echo
    echo "  This host was not set up by \`git clone\` or by deploy/lab/deploy.sh."
    echo "  Anything it measures cannot be attributed to a commit. Re-deploy."
    SHORT="unknown"
    NO_GIT=1
  fi
fi
if [ -z "${NO_GIT:-}" ]; then
  # Match platform tags only. Plain `git describe --tags` would latch onto
  # v1.0.3 and report "17 commits after the legacy client release", which
  # describes a different artifact's version line entirely.
  DESCRIBE=$(git describe --tags --match 'platform-v*' --always 2>/dev/null || echo "$SHORT")
  [ "$DESCRIBE" = "$SHORT" ] && DESCRIBE="$SHORT  ${DIM}(no platform tag yet)${RST}"
  BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
  DIRTY=""
  [ -n "$(git status --porcelain 2>/dev/null)" ] && DIRTY=" ${BOLD}(uncommitted changes)${RST}"

  say "version" "$DESCRIBE$DIRTY"
  say "commit" "$SHORT  on $BRANCH"

  # Submodule pins matter for reproducibility: a run is only repeatable if
  # these match too, and run.json records them per run for the same reason.
  if [ -f .gitmodules ]; then
    git submodule status 2>/dev/null | while read -r sha path _; do
      say "submodule" "$(printf '%-22s %s' "$path" "${sha#[-+ ]}" | cut -c1-60)"
    done
  fi
fi

# ---------------------------------------------------------------- role
echo
ROLE="none running"
for r in ue edge gnb infra; do
  f="deploy/lab/compose.$r.yml"
  [ -f "$f" ] || continue
  if docker compose -f "$f" ps -q 2>/dev/null | grep -q .; then
    ROLE="$r  ${DIM}(compose.$r.yml)${RST}"
    break
  fi
done
# The local all-in-one topology is not a lab role but is worth naming.
if [ "$ROLE" = "none running" ] &&
   docker compose -f deploy/compose/logging.yml -f deploy/compose/local.yml ps -q 2>/dev/null | grep -q .; then
  ROLE="local  ${DIM}(compose/local.yml — dev topology)${RST}"
fi
say "role" "$ROLE"

# ---------------------------------------------------------------- containers
echo
mapfile -t RUNNING < <(
  docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null \
    | grep -E 'mec-cast|zenoh|lidar|edge|ran-collector|logging|postgres' || true
)

if [ ${#RUNNING[@]} -eq 0 ]; then
  say "containers" "none running"
else
  echo "  ${BOLD}containers${RST}"
  MISMATCH=0
  for line in "${RUNNING[@]}"; do
    name=${line%%$'\t'*}
    image=${line##*$'\t'}
    # The digest is the only unambiguous identity — a tag can be re-pointed.
    digest=$(docker inspect --format '{{index .RepoDigests 0}}' "$name" 2>/dev/null | cut -d@ -f2)
    rev=$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
          "$name" 2>/dev/null)
    ver=$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' \
          "$name" 2>/dev/null)
    printf "    %-24s %s\n" "$name" "$image"

    # Print provenance whether or not it matches. A silent line would leave
    # "matches" and "image predates the labels" looking identical.
    if [ -z "$rev" ] || [ "$rev" = "unknown" ] || [ "$rev" = "<no value>" ]; then
      printf "    %-24s %sunlabelled — built before OCI stamps, provenance unknown%s\n" \
             "" "$DIM" "$RST"
    elif [ "${rev:0:7}" = "$SHORT" ]; then
      printf "    %-24s %sbuilt %s (%s) — matches this checkout%s\n" \
             "" "$DIM" "$ver" "${rev:0:7}" "$RST"
    else
      printf "    %-24s %sbuilt %s (%s) — checkout is %s%s\n" \
             "" "$BOLD" "$ver" "${rev:0:7}" "$SHORT" "$RST"
      MISMATCH=1
    fi
    [ -n "$digest" ] && printf "    %-24s %s%s…%s\n" "" "$DIM" "${digest:0:26}" "$RST"
  done
  if [ "$MISMATCH" = 1 ]; then
    echo
    echo "  ${BOLD}WARNING${RST}: a running image was built from a different commit"
    echo "  than this checkout. Measurements taken now are not attributable to"
    echo "  the source you are looking at. Redeploy, or pull the matching image."
  fi
fi

# ---------------------------------------------------------------- clock
echo
if [ -e /dev/ptp0 ]; then
  say "PTP" "/dev/ptp0 present — run deploy/lab/ptp/verify-ptp.sh to check sync"
else
  say "PTP" "no /dev/ptp0 — cross-host metrics will record ptp.reliable=false"
fi
