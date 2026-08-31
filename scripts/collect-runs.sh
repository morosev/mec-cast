#!/bin/bash
# Collect run data from every host into one tree, and optionally archive it.
#
#   bash scripts/collect-runs.sh ops@ue ops@edge ops@gnb        # dry run
#   bash scripts/collect-runs.sh -a ops@ue ops@edge ops@gnb     # transfer
#   bash scripts/collect-runs.sh -a -b /srv/archive ops@ue ...  # + .tar.gz
#   bash scripts/collect-runs.sh -a -r <run-id> ops@ue ...      # one run
#
# A run is NOT in one place. Each host writes only the sites it runs -- the UE
# has pub-* and render-*, the edge has edge-0, the gNB has ran, infra has
# run.json -- so `runs/<id>/` is a different fragment on each machine and no
# single host has ever held a whole run. This merges them.
#
# DRY RUN BY DEFAULT, and it never deletes anything anywhere: no --delete, on
# either side. The remote fragments stay put, and a second host's files are
# never removed because this host did not have them.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

DEST="${RUNS_DIR:-runs}"
REMOTE_RUNS="mec-cast/runs"
APPLY=0
ARCHIVE=""
ONLY_RUN=""

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while getopts "ad:b:r:s:h" opt; do
    case "$opt" in
        a) APPLY=1 ;;
        d) DEST="$OPTARG" ;;
        b) ARCHIVE="$OPTARG" ;;
        r) ONLY_RUN="$OPTARG" ;;
        s) REMOTE_RUNS="$OPTARG" ;;
        h) usage 0 ;;
        *) usage 2 ;;
    esac
done
shift $((OPTIND - 1))

if [ "$#" -eq 0 ]; then
    echo "ERROR: name at least one source." >&2
    echo "  user@host          collects ~/$REMOTE_RUNS from it" >&2
    echo "  /some/path         collects a local or mounted directory" >&2
    exit 2
fi

command -v rsync >/dev/null || { echo "ERROR: rsync is not installed." >&2; exit 2; }

mkdir -p "$DEST" || { echo "ERROR: cannot create $DEST" >&2; exit 2; }

printf 'destination : %s\n' "$DEST"
printf 'sources     : %s\n' "$*"
[ -n "$ONLY_RUN" ] && printf 'run filter  : %s\n' "$ONLY_RUN"
printf 'mode        : %s\n\n' \
    "$([ "$APPLY" = 1 ] && echo 'TRANSFER' || echo 'dry run — nothing is copied')"

RSYNC_OPTS=(-az --no-owner --no-group --info=stats1)
[ "$APPLY" = 1 ] || RSYNC_OPTS+=(--dry-run)
if [ -n "$ONLY_RUN" ]; then
    # Anchored so a run id cannot match a directory deeper in the tree.
    RSYNC_OPTS+=(--include="/$ONLY_RUN/" --include="/$ONLY_RUN/**"
                 --include='/admin-journal.jsonl' --exclude='/*')
fi

failed=0
for src in "$@"; do
    case "$src" in
        *@*|*:*) from="${src%%:*}:$REMOTE_RUNS/" ;;   # a host
        *)       from="${src%/}/" ;;                  # a path
    esac

    printf '=== %s ===\n' "$from"
    if rsync "${RSYNC_OPTS[@]}" "$from" "$DEST/" 2>&1 | sed 's/^/  /'; then
        :
    else
        echo "  FAILED: $from" >&2
        failed=$((failed + 1))
    fi
    echo
done

runs=$(find "$DEST" -maxdepth 1 -mindepth 1 -type d | wc -l)
sites=$(find "$DEST" -name samples.csv 2>/dev/null | wc -l)
printf 'collected   : %s run directory(ies), %s samples.csv\n' "$runs" "$sites"
[ "$failed" = 0 ] || printf 'sources failed: %s — the tree is INCOMPLETE\n' "$failed"

if [ "$APPLY" != 1 ]; then
    echo
    echo "Dry run. Re-run with -a to transfer."
    exit 0
fi

# --- optional archive ------------------------------------------------------
if [ -n "$ARCHIVE" ]; then
    mkdir -p "$ARCHIVE" || { echo "ERROR: cannot create $ARCHIVE" >&2; exit 2; }
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    # First 8 characters, not up to the first dash: a UUID splits fine
    # that way but any other id shape does not.
    name="mec-cast-runs${ONLY_RUN:+-${ONLY_RUN:0:8}}-$stamp.tar.gz"
    target="$ARCHIVE/$name"

    echo
    echo "archiving to $target"
    # Written to .part and renamed: an interrupted archive must never look
    # like a finished one, which is the whole reason to keep it.
    if tar -czf "$target.part" -C "$DEST" . && mv "$target.part" "$target"; then
        printf '  wrote %s (%s)\n' "$name" "$(du -h "$target" | cut -f1)"
        echo "  verify:  tar -tzf $target | head"
    else
        rm -f "$target.part"
        echo "  FAILED to write $target" >&2
        exit 1
    fi
fi

[ "$failed" = 0 ] || exit 1
