# MNEMOS Backup Scaffolding

Generic backup templates that any operator can adapt. Three layers:

1. **Per-host push** — systemd timer fires `mnemos-backup-daily.sh` on
   each Linux host; the script rsyncs configured directories (and
   optionally `pg_dump`s configured databases) to a central target.
2. **Central retention** — `retention.sh` runs on the backup target and
   thins out old `pg_dump` files to daily/weekly/monthly snapshots.
3. **Off-site** — the backup target itself should be replicated
   off-site; see "Off-site replication" below.

Nothing in this directory is fleet-specific; each host reads
`/etc/mnemos-backup.conf` for its own list of paths and databases.

---

## Installation (per Linux host)

```bash
# 1. copy the script to a privileged path
sudo install -m 0755 tools/backup/mnemos-backup-daily.sh \
    /usr/local/bin/mnemos-backup-daily.sh

# 2. copy the config example and customize
sudo install -m 0644 tools/backup/mnemos-backup.conf.example \
    /etc/mnemos-backup.conf
sudo editor /etc/mnemos-backup.conf

# 3. install systemd units
sudo install -m 0644 tools/backup/mnemos-backup-daily.service \
    /etc/systemd/system/
sudo install -m 0644 tools/backup/mnemos-backup-daily.timer \
    /etc/systemd/system/
sudo systemctl daemon-reload

# 4. enable and start
sudo systemctl enable --now mnemos-backup-daily.timer

# 5. verify
systemctl list-timers mnemos-backup-daily.timer
journalctl -u mnemos-backup-daily.service --since -1h
```

The timer defaults to **03:00 local with up to 30-minute random delay**.
Override per-host to stagger multiple backups:

```bash
sudo systemctl edit mnemos-backup-daily.timer
# drop-in contents:
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:15:00
```

### Dry run

```bash
sudo MNEMOS_BACKUP_CONFIG=/etc/mnemos-backup.conf \
    /usr/local/bin/mnemos-backup-daily.sh
```

---

## Per-host configuration

### Hosts with Postgres (MNEMOS API nodes)

```conf
BACKUP_TARGET=/mnt/argonas/datapool/backups
BACKUP_DIRECTORIES=/home/jasonperlow/.claude:/etc/mnemos:/opt/mnemos
INCLUDE_PG=true
PG_DATABASES=mnemos
```

For **container-hosted Postgres** (e.g. `mnemos-prod-pg` on CERBERUS):

```conf
INCLUDE_PG=true
PG_DATABASES=mnemos
PG_CONTAINER=mnemos-prod-pg
PG_USER=mnemos
```

The container user (`jasonperlow` in the default service unit) needs
to be in the `docker` group so `docker exec` works from the timer.

### Hosts without Postgres (agent / edge nodes)

```conf
BACKUP_TARGET=/mnt/argonas/datapool/backups
BACKUP_DIRECTORIES=/home/jasonperlow/.claude:/home/jasonperlow/src
INCLUDE_PG=false
```

### Read-mostly edge (Pi, Jetson)

```conf
BACKUP_TARGET=/mnt/argonas/datapool/backups
BACKUP_DIRECTORIES=/etc/wpa_supplicant:/boot/extlinux.conf:/home/pi/configs
INCLUDE_PG=false
```

Weekly is usually enough for edge devices:

```bash
sudo systemctl edit mnemos-backup-daily.timer
[Timer]
OnCalendar=
OnCalendar=Sun 04:00
```

---

## Retention (run on the backup target)

Copy `retention.sh` to `/usr/local/bin/mnemos-backup-retention.sh` on
ARGONAS (or whatever host has write access to the backup target) and
schedule it with a systemd timer or cron:

```bash
# Example: nightly at 05:00 UTC
0 5 * * * /usr/local/bin/mnemos-backup-retention.sh >> /var/log/mnemos-retention.log 2>&1
```

Retention defaults:
- Keep all `pg_dump`s for 7 days
- Keep one per ISO week for 4 prior weeks
- Keep one per calendar month for 12 prior months
- Delete older

`fs/` rsync trees are not rotated — they're mirrored in place. Use ZFS
snapshots on the backup dataset if you want historical filesystem
views.

---

## Mac Time Machine (STUDIO, ULTRA)

Macs get **two Time Machine destinations** — local USB (already
configured) plus an SMB network share on ARGONAS:

1. Ensure ARGONAS exports `/mnt/datapool/timemachine` over SMB. TrueNAS:
   Storage → Shares → SMB → confirm `timemachine` share exists with
   "Time Machine" preset enabled.
2. On the Mac: **System Settings → General → Time Machine → + →
   Other ARGONAS shares**. Pick `timemachine`. Enter credentials.
3. Time Machine automatically rotates between both disks. Each disk
   gets its own full retention (24 hourly / 7 daily / 4 weekly).

No scripts needed — this is a native macOS feature.

---

## Off-site replication (ARGONAS itself)

Everything backs up *to* ARGONAS. If ARGONAS fails, you lose
everything. The recommended setup is **restic** for block-level
dedup + client-side encryption, with rclone as the transport to
whatever cloud storage you already pay for (Google Drive, Backblaze
B2, S3, etc.).

**Why restic beats tar+gpg+Cloud-Sync at scale:** encrypted tarballs
have random bytes, so cloud-sync tools can't binary-diff them. Every
nightly run re-uploads the full encrypted tarball. On a 400 GB+
corpus that's hours of daily upload. Restic does block-level content
addressing — typical nightly increment is <1 GB because most of the
corpus is stable.

### Option A — restic over rclone Google Drive (recommended)

Zero marginal cost if you already have a Google One plan (5 TB is
more than enough for the full backup corpus for years).

**One-time setup.**

```bash
# Configure rclone remote (interactive OAuth via SSH port-forward)
rclone config           # choose: n, name=gdrive-mnemos-offsite,
                        # storage=drive, then OAuth flow

# Store the restic passphrase (must be backed up OUT OF BAND — see below)
echo "<strong random passphrase>" > /mnt/datapool/scripts/.mnemos-offsite.passphrase
chmod 600 /mnt/datapool/scripts/.mnemos-offsite.passphrase

# Initialize the restic repository on Drive
export RESTIC_REPOSITORY="rclone:gdrive-mnemos-offsite:mnemos-restic"
export RESTIC_PASSWORD_FILE=/mnt/datapool/scripts/.mnemos-offsite.passphrase
restic init

# Install the nightly script
install -m 0755 tools/backup/mnemos-offsite-restic.sh \
    /mnt/datapool/scripts/mnemos-offsite-restic.sh
```

**TrueNAS config.** If you're already running a TrueNAS Cloud Sync
task for Google Drive, you can reuse its stored OAuth token directly
instead of running `rclone config`. The provider credentials are
queryable via `midclt call cloudsync.query` — extract `client_id`,
`client_secret`, and the stringified `token`, then write
`/root/.config/rclone/rclone.conf`:

```ini
[gdrive-mnemos-offsite]
type = drive
client_id = <from TrueNAS>
client_secret = <from TrueNAS>
scope = drive
token = <stringified OAuth JSON from TrueNAS>
```

rclone auto-refreshes the access token using the refresh token, so
this config stays valid indefinitely.

**Schedule via TrueNAS cron** (System Settings → Advanced → Cron Jobs):

- Description: `MNEMOS nightly off-site (restic + rclone gdrive)`
- Command: `/mnt/datapool/scripts/mnemos-offsite-restic.sh`
- User: `root`
- Schedule: daily 05:30
- Hide Standard Output/Error: unchecked (you want the log)

**Disable or remove any pre-existing Cloud Sync task** for the
off-site — restic talks to Drive directly via rclone and doesn't
need Cloud Sync as a middleman.

**Verify restore path.** Periodically (monthly) prove the backup
works end-to-end:

```bash
# On a host that doesn't share the passphrase fs with the NAS
export RESTIC_REPOSITORY="rclone:gdrive-mnemos-offsite:mnemos-restic"
export RESTIC_PASSWORD="<paste from password manager>"

restic snapshots                              # list what's in the repo
restic restore latest --target /tmp/restore   # restore the latest
ls /tmp/restore/                              # verify contents
rm -rf /tmp/restore/
```

If the passphrase lives only in ARGONAS and ARGONAS dies, the
off-site is unrecoverable. **Put the restic passphrase in a
password manager** (1Password, LastPass) before production.

### Option B — restic to Backblaze B2 or S3 (if not using Google Drive)

Same restic script, different rclone remote:

```bash
rclone config       # add: name=b2-mnemos-offsite, storage=b2
# then change RESTIC_REPOSITORY in mnemos-offsite-restic.sh
export RESTIC_REPOSITORY="rclone:b2-mnemos-offsite:mnemos-restic"
```

Cost at ~200 GB of deduped corpus + history: ~$1.20/month on B2.

### Option C — Second NAS via `zfs send | receive`

If you have a second physical box on the LAN, periodic snapshot
replication is free and often faster than cloud:

```bash
# on the source NAS (ARGONAS)
zfs snapshot -r datapool/backups@$(date -u +%Y%m%d-%H%M%S)
zfs send -i datapool/backups@<prev> datapool/backups@<new> \
    | ssh root@backup-box zfs receive datapool/argonas-backups
```

### Option D — External USB rotated weekly

Cheapest manual fallback. Plug in disk, `zfs send` snapshot, unplug,
take off-site, repeat with a second disk next week.

---

## Recovery drills

A backup you haven't restored from is a backup you don't have. At
minimum, once per quarter:

1. Pick a random `pg_dump` file from ARGONAS.
2. Spin up a throwaway Postgres container (`pgvector/pgvector:pg17`).
3. `pg_restore` into it.
4. Verify row counts match the production DB as of the dump timestamp.
5. Delete the throwaway.

Document the last successful restore drill in the ARGONAS backup
directory's README so the next operator can see when the coverage was
last proven.

---

## Monitoring

Each run writes `$BACKUP_TARGET/<hostname>/last-backup.json` with a
timestamp. A simple monitor:

```bash
# Alert if any host's last-backup.json is stale (>36h old)
find /mnt/argonas/datapool/backups -name last-backup.json -mmin +2160 -print
```

Wire that to your alerting channel of choice.
