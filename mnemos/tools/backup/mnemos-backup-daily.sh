#!/bin/bash
# Generic MNEMOS host backup: rsync user data to a configured target,
# optionally dumping one or more Postgres databases first.
#
# Reads config from /etc/mnemos-backup.conf (see sample in this directory).
# Designed to run non-interactively from a systemd timer.
#
# Exit codes:
#   0   everything succeeded
#   1   config missing / malformed
#   2   pg_dump failed
#   3   rsync failed
#   4   target directory unreachable

set -u -o pipefail

CONFIG=${MNEMOS_BACKUP_CONFIG:-/etc/mnemos-backup.conf}

log() { echo "[$(date -u +%FT%TZ)] $*"; }
fail() { log "ERROR: $*"; exit "$1"; }

[ -r "$CONFIG" ] || fail 1 "config not readable: $CONFIG"
# shellcheck disable=SC1090
source "$CONFIG"

: "${BACKUP_TARGET:?BACKUP_TARGET must be set in $CONFIG}"
: "${BACKUP_HOSTNAME:=$(hostname -s)}"
: "${BACKUP_DIRECTORIES:=}"
: "${INCLUDE_PG:=false}"
: "${PG_DATABASES:=}"
: "${PG_CONTAINER:=}"     # if non-empty, dump via `docker exec <container>`
: "${PG_DUMP_OPTS:=-Fc}"
: "${RSYNC_EXTRA_OPTS:=}"

TARGET_DIR="${BACKUP_TARGET%/}/${BACKUP_HOSTNAME}"
mkdir -p "$TARGET_DIR" || fail 4 "cannot create $TARGET_DIR"
[ -w "$TARGET_DIR" ] || fail 4 "$TARGET_DIR not writable"

STAMP=$(date -u +%Y%m%d-%H%M%S)
log "backup start host=$BACKUP_HOSTNAME target=$TARGET_DIR stamp=$STAMP"

# ---- Postgres dump step (optional) -----------------------------------------

if [ "$INCLUDE_PG" = "true" ] && [ -n "$PG_DATABASES" ]; then
    DUMP_DIR="$TARGET_DIR/pgdump"
    mkdir -p "$DUMP_DIR" || fail 2 "cannot create $DUMP_DIR"

    IFS=',' read -ra DBS <<< "$PG_DATABASES"
    for DB in "${DBS[@]}"; do
        DB_TRIM=$(echo "$DB" | xargs)
        OUT="$DUMP_DIR/${DB_TRIM}-${STAMP}.dump"
        log "pg_dump db=$DB_TRIM out=$OUT"

        if [ -n "$PG_CONTAINER" ]; then
            # Dump from a docker container. Config must provide PG_USER.
            : "${PG_USER:?PG_USER required for container dump}"
            docker exec "$PG_CONTAINER" \
                pg_dump -U "$PG_USER" -d "$DB_TRIM" $PG_DUMP_OPTS > "$OUT" \
                || fail 2 "pg_dump failed for $DB_TRIM via container $PG_CONTAINER"
        else
            # Dump from local Postgres.
            pg_dump -d "$DB_TRIM" $PG_DUMP_OPTS > "$OUT" \
                || fail 2 "pg_dump failed for $DB_TRIM"
        fi

        chmod 600 "$OUT"
        log "dump ok size=$(stat -c%s "$OUT" 2>/dev/null || echo ?) db=$DB_TRIM"
    done
fi

# ---- rsync directories -----------------------------------------------------

if [ -n "$BACKUP_DIRECTORIES" ]; then
    FS_DIR="$TARGET_DIR/fs"
    mkdir -p "$FS_DIR" || fail 3 "cannot create $FS_DIR"

    IFS=':' read -ra DIRS <<< "$BACKUP_DIRECTORIES"
    for SRC in "${DIRS[@]}"; do
        SRC_TRIM=$(echo "$SRC" | xargs)
        [ -z "$SRC_TRIM" ] && continue
        if [ ! -e "$SRC_TRIM" ]; then
            log "skip: $SRC_TRIM does not exist"
            continue
        fi

        # Preserve the source path shape inside the fs/ tree so restores
        # can be targeted. --delete scopes to this one source subtree only.
        REL_NAME=$(echo "$SRC_TRIM" | sed 's|^/||; s|/$||; s|/|_|g')
        DEST="$FS_DIR/$REL_NAME"

        log "rsync src=$SRC_TRIM -> $DEST"
        # -rlptDH = recurse, links, perms, times, devices, hardlinks
        # --no-owner --no-group: NFS with id-squash rejects chown/chgrp, so
        #   we preserve content + perms but accept the target's ownership.
        # shellcheck disable=SC2086
        rsync -rlptDH --delete --no-owner --no-group \
            --exclude='.cache/' --exclude='__pycache__/' --exclude='node_modules/' \
            --exclude='*.pyc' --exclude='.venv/' --exclude='venv/' \
            $RSYNC_EXTRA_OPTS \
            "$SRC_TRIM/" "$DEST/" \
            || fail 3 "rsync failed for $SRC_TRIM"
    done
fi

# ---- write a manifest so retention.sh has something to walk ---------------

MANIFEST="$TARGET_DIR/last-backup.json"
cat > "$MANIFEST" <<EOF
{
  "hostname": "$BACKUP_HOSTNAME",
  "stamp": "$STAMP",
  "completed_at": "$(date -u +%FT%TZ)",
  "include_pg": $INCLUDE_PG,
  "pg_databases": "$PG_DATABASES",
  "directories": "$BACKUP_DIRECTORIES"
}
EOF

log "backup done host=$BACKUP_HOSTNAME stamp=$STAMP"
