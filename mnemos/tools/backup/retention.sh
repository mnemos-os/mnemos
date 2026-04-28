#!/bin/bash
# ARGONAS-side retention: walk the backup tree and delete old pg_dump files
# past retention thresholds. Run as a systemd timer on ARGONAS or any host
# with write access to the backup target.
#
# Retention policy (defaults):
#   * keep all pg_dumps from the last 7 days (daily)
#   * keep 1 pg_dump per week for the 4 prior weeks (weekly)
#   * keep 1 pg_dump per month for the 12 prior months (monthly)
#   * delete everything older
#
# Filesystem rsync trees are NOT rotated — they're mirrored in place and
# overwritten on each run, so they have a single "latest" view. If you want
# historical fs snapshots, enable ZFS snapshots on the dataset instead.
#
# Reads config from /etc/mnemos-backup.conf (same file the push-side uses).

set -u -o pipefail

CONFIG=${MNEMOS_BACKUP_CONFIG:-/etc/mnemos-backup.conf}
[ -r "$CONFIG" ] && source "$CONFIG"   # optional: only BACKUP_TARGET is needed

: "${BACKUP_TARGET:=/mnt/argonas/datapool/backups}"
: "${RETENTION_DAILY_DAYS:=7}"
: "${RETENTION_WEEKLY_WEEKS:=4}"
: "${RETENTION_MONTHLY_MONTHS:=12}"

log() { echo "[$(date -u +%FT%TZ)] $*"; }

# Age cutoff in seconds.
MAX_AGE_DAYS=$(( RETENTION_DAILY_DAYS + RETENTION_WEEKLY_WEEKS * 7 + RETENTION_MONTHLY_MONTHS * 31 ))
log "retention sweep target=$BACKUP_TARGET max_age_days=$MAX_AGE_DAYS"

# Find candidate dumps — *.dump files in any */pgdump/ subdirectory.
# Strict: only touch the pgdump tree; leave fs/ and manifests alone.
find "$BACKUP_TARGET" -mindepth 3 -maxdepth 4 -type f -name '*.dump' \
    -path '*/pgdump/*' -mtime +"$MAX_AGE_DAYS" -print -delete 2>&1 \
    | while read -r line; do log "pruned $line"; done

# Second pass: in the window between daily (keep all) and max-age (keep some),
# thin out to one-per-week for the weekly band and one-per-month for the
# monthly band. Implemented as a per-db sort-and-keep.
CUTOFF_DAILY=$(date -u -d "$RETENTION_DAILY_DAYS days ago" +%s)
CUTOFF_WEEKLY=$(date -u -d "$((RETENTION_DAILY_DAYS + RETENTION_WEEKLY_WEEKS * 7)) days ago" +%s)

find "$BACKUP_TARGET" -mindepth 3 -maxdepth 4 -type d -name pgdump \
    -printf '%p\n' | while read -r DIR; do
    # Collect files per-DB (filename prefix before the first '-')
    declare -A SEEN_WEEK SEEN_MONTH
    ls -1tr "$DIR" 2>/dev/null | while read -r FILE; do
        FPATH="$DIR/$FILE"
        [ -f "$FPATH" ] || continue
        MTIME=$(stat -c %Y "$FPATH")

        if [ "$MTIME" -ge "$CUTOFF_DAILY" ]; then
            # Daily band: keep all.
            continue
        elif [ "$MTIME" -ge "$CUTOFF_WEEKLY" ]; then
            # Weekly band: keep one per (db, iso-week)
            DB=$(echo "$FILE" | sed 's/-[0-9]\{8\}-.*//')
            WEEK=$(date -u -d "@$MTIME" +%G-%V)
            KEY="$DB-$WEEK"
            if [ -n "${SEEN_WEEK[$KEY]:-}" ]; then
                rm -f "$FPATH" && log "pruned weekly-dup $FPATH"
            else
                SEEN_WEEK[$KEY]=1
            fi
        else
            # Monthly band: keep one per (db, year-month)
            DB=$(echo "$FILE" | sed 's/-[0-9]\{8\}-.*//')
            MONTH=$(date -u -d "@$MTIME" +%Y-%m)
            KEY="$DB-$MONTH"
            if [ -n "${SEEN_MONTH[$KEY]:-}" ]; then
                rm -f "$FPATH" && log "pruned monthly-dup $FPATH"
            else
                SEEN_MONTH[$KEY]=1
            fi
        fi
    done
done

log "retention done"
