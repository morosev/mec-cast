#!/bin/bash
# Delete run directories older than a cutoff.
#
#   bash scripts/prune-runs.sh                 # dry run, 30 days
#   bash scripts/prune-runs.sh -d 90           # dry run, 90 days
#   bash scripts/prune-runs.sh -d 90 -a        # actually delete
#   bash scripts/prune-runs.sh -r -a           # only runs already removed
#
# DRY RUN BY DEFAULT. This deletes measurement data -- per-frame CSVs are the
# source of truth for whole-run statistics, and nothing else holds them. The
# telemetry in PostgreSQL is windowed summaries of the same runs and is pruned
# separately (`mec-cast-logs purge --days N`); neither is a backup of the other.
#
# Age comes from the manifest's created_utc when there is one. Most run
# directories have no manifest -- run-experiment.sh writes CSVs without one --
# so those fall back to directory mtime, which is what `ls -t` already sorts by.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

DAYS=30
APPLY=0
REMOVED_ONLY=0
RUNS_DIR="${RUNS_DIR:-runs}"

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while getopts "d:arD:h" opt; do
    case "$opt" in
        d) DAYS="$OPTARG" ;;
        a) APPLY=1 ;;
        r) REMOVED_ONLY=1 ;;
        D) RUNS_DIR="$OPTARG" ;;
        h) usage 0 ;;
        *) usage 2 ;;
    esac
done

case "$DAYS" in
    ''|*[!0-9]*) echo "ERROR: -d takes a whole number of days, got '$DAYS'" >&2; exit 2 ;;
esac

# Guard against a mistyped -D taking out something that is not a runs
# directory. A real one holds run-id directories; an empty or wrong path
# should stop here rather than after the loop has decided everything is old.
if [ ! -d "$RUNS_DIR" ]; then
    echo "ERROR: $RUNS_DIR is not a directory." >&2
    exit 2
fi
if [ -z "$(find "$RUNS_DIR" -maxdepth 1 -mindepth 1 -type d -print -quit)" ]; then
    echo "Nothing to do: $RUNS_DIR holds no run directories."
    exit 0
fi

CUTOFF=$(( $(date +%s) - DAYS * 86400 ))
printf 'runs dir : %s\n' "$RUNS_DIR"
printf 'cutoff   : older than %s days (before %s)\n' "$DAYS" "$(date -u -d "@$CUTOFF" +%Y-%m-%d)"
printf 'mode     : %s\n\n' "$([ "$APPLY" = 1 ] && echo 'DELETE' || echo 'dry run — nothing is removed')"

total_kb=0
count=0
kept_live=0

while IFS= read -r dir; do
    name=$(basename "$dir")
    manifest="$dir/run.json"
    state=""
    when=""

    if [ -f "$manifest" ]; then
        state=$(sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p' "$manifest" | head -1)
        when=$(sed -n 's/.*"created_utc"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$manifest" | head -1)
        [ -n "$when" ] || when=$(sed -n 's/.*"started_utc"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$manifest" | head -1)
    fi

    if [ -n "$when" ]; then
        age_ts=$(date -u -d "$when" +%s 2>/dev/null) || age_ts=""
    fi
    # No manifest, or an unparseable stamp: fall back to the directory itself.
    [ -n "${age_ts:-}" ] || age_ts=$(stat -c %Y "$dir")

    # A run that is not in a terminal state is either live or mid-flight, and
    # deleting it out from under the admin corrupts a run in progress rather
    # than reclaiming space. Age is not the deciding factor here.
    case "$state" in
        draft|starting|running|stopping)
            kept_live=$((kept_live + 1))
            unset age_ts
            continue
            ;;
    esac

    if [ "$REMOVED_ONLY" = 1 ] && [ "$state" != "removed" ]; then
        unset age_ts
        continue
    fi

    if [ "$age_ts" -lt "$CUTOFF" ]; then
        kb=$(du -sk "$dir" | cut -f1)
        total_kb=$((total_kb + kb))
        count=$((count + 1))
        printf '  %-40s %s  %6s MB  %s\n' \
            "$name" "$(date -u -d "@$age_ts" +%Y-%m-%d)" "$((kb / 1024))" "${state:-no manifest}"
        if [ "$APPLY" = 1 ]; then
            rm -rf -- "$dir" || echo "    FAILED to remove $dir" >&2
        fi
    fi
    unset age_ts
done < <(find "$RUNS_DIR" -maxdepth 1 -mindepth 1 -type d | sort)

echo
if [ "$count" = 0 ]; then
    echo "Nothing older than $DAYS days."
else
    printf '%s %d run(s), %d MB%s\n' \
        "$([ "$APPLY" = 1 ] && echo 'Deleted' || echo 'Would delete')" \
        "$count" "$((total_kb / 1024))" \
        "$([ "$APPLY" = 1 ] && echo '' || echo ' — re-run with -a to apply')"
fi
[ "$kept_live" = 0 ] || echo "Kept $kept_live run(s) that are not in a terminal state."

if [ "$APPLY" = 1 ] && [ "$count" != 0 ]; then
    echo
    echo "The admin holds its run table in memory and will keep showing these"
    echo "rows until it reloads. Restart it to catch up:"
    echo "  docker compose -f deploy/lab/compose.infra.yml restart admin"
fi
