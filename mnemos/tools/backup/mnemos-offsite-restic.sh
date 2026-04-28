#!/bin/bash
# Nightly off-site backup via restic (block-level dedup + client-side
# encryption) to Google Drive using rclone as the transport. Runs on
# the NAS under a scheduled cron.
#
# Why restic instead of tar+gpg+Cloud-Sync:
# - Block-level dedup: typical nightly increment is <1 GB even on a
#   400 GB+ corpus, because most of the corpus is stable.
# - Client-side AES-256 encryption: Drive never sees plaintext.
# - GFS retention (daily/weekly/monthly) is a one-line config.
# - Snapshot-browseable via `restic snapshots` + `restic mount`.
#
# The rclone remote `gdrive-mnemos-offsite:` must be configured on this
# host (see README for config generation from TrueNAS OAuth, or run
# `rclone config` interactively for a fresh install).

set -u -o pipefail

export RESTIC_REPOSITORY="rclone:gdrive-mnemos-offsite:mnemos-restic"
export RESTIC_PASSWORD_FILE=/mnt/datapool/scripts/.mnemos-offsite.passphrase

# Sources to back up — add whatever else you want off-site
SOURCES=(
    /mnt/datapool/backups       # per-host pushes + TrueNAS pulls + pg_dump dir
    /mnt/datapool/git           # bare repos (canonical code)
)

log() { echo "[$(date -u +%FT%TZ)] $*"; }

log "backup start sources=${SOURCES[*]}"
restic backup "${SOURCES[@]}" \
    --tag nightly \
    --host "$(hostname -s)" \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='node_modules' \
    --exclude='.cache' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='.nvm' \
    --verbose=1 2>&1 | tail -20 || { log "ERROR: restic backup failed"; exit 2; }

log "forget + prune — keep 7 daily, 4 weekly, 12 monthly"
restic forget \
    --keep-daily 7 \
    --keep-weekly 4 \
    --keep-monthly 12 \
    --tag nightly \
    --host "$(hostname -s)" \
    --prune 2>&1 | tail -10

log "snapshots:"
restic snapshots --compact 2>&1 | tail -15

log "repo stats:"
restic stats --mode raw-data 2>&1 | tail -8

log "done"
