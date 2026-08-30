#!/bin/bash
# Periodic pg_dump to a directory on the host, for the lab infra role.
#
# WHY A LOOP AND NOT CRON: the schedule is derived from the backup directory
# itself -- "is the newest dump older than BACKUP_EVERY?" -- not from a timer
# held in this container. That makes it restart-safe and self-correcting. A
# cron entry inside a container silently stops backing up whenever the
# container is down at the wrong minute, and says nothing about it; this wakes
# up, sees the gap, and closes it.
#
# An empty directory means "infinitely old", so the first dump happens
# immediately on deployment rather than a week later. Waiting a week to
# discover the credentials or the mount were wrong is the failure this avoids.
set -uo pipefail

DIR=/backups
DB="${POSTGRES_DB:-mec_cast_logs}"
USER="${POSTGRES_USER:-postgres}"
HOST="${BACKUP_PGHOST:-postgres}"

# Accepts 7d / 24h / 90m / 3600s / bare seconds.
to_seconds() {
    local v="$1" n="${1%[dhms]}" unit="${1##*[0-9]}"
    case "$v" in
        *[!0-9dhms]*) echo "invalid duration: $v" >&2; return 1 ;;
    esac
    [ -n "$n" ] || { echo "invalid duration: $v" >&2; return 1; }
    case "$unit" in
        d) echo $((n * 86400)) ;;
        h) echo $((n * 3600))  ;;
        m) echo $((n * 60))    ;;
        s|"") echo "$n"        ;;
        *) echo "invalid duration: $v" >&2; return 1 ;;
    esac
}

EVERY=$(to_seconds "${BACKUP_EVERY:-7d}") || exit 2
CHECK=$(to_seconds "${BACKUP_CHECK_EVERY:-1h}") || exit 2
KEEP="${BACKUP_KEEP:-8}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

log "every ${BACKUP_EVERY:-7d} (${EVERY}s), keep ${KEEP}, checking every ${BACKUP_CHECK_EVERY:-1h}"
log "writing to ${DIR} (bind-mounted from the host)"

mkdir -p "$DIR" || { log "FATAL: cannot create $DIR"; exit 1; }
if ! touch "$DIR/.writable" 2>/dev/null; then
    log "FATAL: $DIR is not writable. Check BACKUP_DIR on the host and its permissions."
    exit 1
fi
rm -f "$DIR/.writable"

while true; do
    newest=$(find "$DIR" -maxdepth 1 -name "${DB}-*.dump" -printf '%T@\n' 2>/dev/null \
             | sort -rn | head -1)
    now=$(date +%s)
    if [ -z "$newest" ]; then
        age=$((EVERY + 1))          # nothing yet: dump now
    else
        age=$((now - ${newest%.*}))
    fi

    if [ "$age" -ge "$EVERY" ]; then
        stamp=$(date -u +%Y%m%dT%H%M%SZ)
        target="$DIR/${DB}-${stamp}.dump"
        # Dump to .part and rename: a dump interrupted half-written must never
        # look like the newest good backup, or it both fails to restore AND
        # pushes the next real one a full interval away.
        if pg_dump -h "$HOST" -U "$USER" -Fc "$DB" > "$target.part" 2>/tmp/pgdump.err; then
            mv "$target.part" "$target"
            log "wrote $(basename "$target") ($(du -h "$target" | cut -f1))"
            # Prune oldest beyond KEEP.
            mapfile -t all < <(find "$DIR" -maxdepth 1 -name "${DB}-*.dump" -printf '%T@ %p\n' \
                               | sort -rn | cut -d' ' -f2-)
            if [ "${#all[@]}" -gt "$KEEP" ]; then
                for old in "${all[@]:$KEEP}"; do
                    rm -f "$old" && log "pruned $(basename "$old")"
                done
            fi
        else
            rm -f "$target.part"
            log "FAILED: $(tr '\n' ' ' < /tmp/pgdump.err)"
        fi
    fi
    sleep "$CHECK"
done
