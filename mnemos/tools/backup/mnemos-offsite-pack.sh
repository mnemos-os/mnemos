#!/bin/bash
# Nightly tar+gpg pack for off-site upload. Runs on the NAS / backup
# target. Bundles the per-host backup tree AND any bare git repos
# alongside it into one AES256-encrypted tarball that a cloud-sync
# task (Google Drive, B2, etc.) then uploads.
#
# Reads passphrase from the PASS_FILE at the top of the script.
# Writes encrypted tarball to the STAGE directory.
# A separate sync task uploads from STAGE to off-site storage.

set -u -o pipefail

# Paths — adjust to your NAS layout
STAGE=/mnt/datapool/offsite-stage
PASS_FILE=/mnt/datapool/scripts/.mnemos-offsite.passphrase

# Sources to include in the tarball. Add repos, backup roots, or
# anything else you want off-site. Each path is tar'd as-is.
SOURCES=(
    /mnt/datapool/backups   # per-host rsync + pg_dump results
    /mnt/datapool/git       # bare git repos (canonical code)
)

STAMP=$(date -u +%Y-%m-%d)
OUT="$STAGE/mnemos-offsite-${STAMP}.tar.gpg"
LOCAL_RETENTION=14      # keep N local copies; remote has its own retention

log() { echo "[$(date -u +%FT%TZ)] $*"; }

[ -r "$PASS_FILE" ] || { log "ERROR: passphrase file not readable: $PASS_FILE"; exit 1; }

mkdir -p "$STAGE"

log "packing ${SOURCES[*]} -> $OUT"
tar -cf - "${SOURCES[@]}" 2>/dev/null \
    | gpg --batch --yes --symmetric --cipher-algo AES256 \
          --passphrase-file "$PASS_FILE" --no-symkey-cache \
    > "$OUT" || { log "ERROR: tar+gpg pipeline failed"; rm -f "$OUT"; exit 2; }

SIZE=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
log "pack ok size=$SIZE bytes"

# Local retention: keep most recent $LOCAL_RETENTION files
ls -1t "$STAGE"/mnemos-offsite-*.tar.gpg 2>/dev/null \
    | tail -n +$((LOCAL_RETENTION + 1)) \
    | while read -r OLD; do
        log "pruning local $OLD"
        rm -f "$OLD"
    done

log "done"
