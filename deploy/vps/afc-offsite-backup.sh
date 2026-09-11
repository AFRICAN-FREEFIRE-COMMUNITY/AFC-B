#!/bin/bash
# afc-offsite-backup.sh - push the nightly dump and a media snapshot off the box. Runs as root
# from cron at 03:45 UTC, after afc-db-backup.sh (03:15).
#
# Needs ONE thing the owner supplies: an rclone remote called "offsite" in /root/.config/rclone/
# rclone.conf (Backblaze B2 was the runbook's pick, any S3-compatible bucket works). Until that
# file exists the script says so and exits 0, so cron stays quiet and the nightly local dump is
# still made. Set it up with:
#     rclone config          (remote name: offsite, type: b2 or s3, then the bucket's keys)
#     rclone lsd offsite:    (must list the bucket)
# and put the bucket name in /etc/afc-offsite.conf as  BUCKET=<name>
#
# What goes up: every local dump (14 days kept locally; the bucket keeps what its own lifecycle
# rule says), and media/ as an incremental sync (2.3 GB first time, then only changes).
# Restore: rclone copy offsite:$BUCKET/db/<file> . ; rclone sync offsite:$BUCKET/media/ ~/AFC-B/media/
set -euo pipefail
CONF=/etc/afc-offsite.conf
if ! rclone listremotes 2>/dev/null | grep -q '^offsite:' || [ ! -f "$CONF" ]; then
  echo "$(date -u +%FT%TZ) offsite not configured (need rclone remote 'offsite' + $CONF), skipping"
  exit 0
fi
# shellcheck disable=SC1090
. "$CONF"
rclone copy /var/backups/afc "offsite:$BUCKET/db" --include 'afc_db-*.sql.gz' --transfers 4 -q
rclone sync /home/ubuntu/AFC-B/media "offsite:$BUCKET/media" --transfers 8 --fast-list -q
echo "$(date -u +%FT%TZ) offsite ok: db $(rclone size "offsite:$BUCKET/db" --json | grep -oE '"bytes":[0-9]+') media $(rclone size "offsite:$BUCKET/media" --json | grep -oE '"bytes":[0-9]+')"
