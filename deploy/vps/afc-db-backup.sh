#!/bin/bash
# afc-db-backup.sh - nightly consistent dump of afc_db, 14 days kept locally. Runs from root's
# crontab at 03:15 UTC (installed by 08-backup-cron.sh). Day-one safety only: a copy on the same
# disk survives a bad delete or a broken migration, not a dead server. Offsite (Backblaze B2,
# runbook section 17) is the logged follow-up.
#
# --single-transaction: consistent snapshot without locking the site.
# --routines --triggers --events: NOT included by default, and leaving them out silently drops
# database logic (runbook section 9). Root's socket auth means no password on the command line.
#
# Proven by gate G1.

set -eu
DIR=/var/backups/afc
mkdir -p "$DIR"
chmod 700 "$DIR"
STAMP=$(date -u +%Y-%m-%d-%H%M)
OUT="$DIR/afc_db-$STAMP.sql.gz"

mysqldump --single-transaction --routines --triggers --events --quick afc_db | gzip -6 > "$OUT.part"
gzip -t "$OUT.part"
mv "$OUT.part" "$OUT"
find "$DIR" -name 'afc_db-*.sql.gz' -mtime +14 -delete

echo "$(date -u +%FT%TZ) wrote $OUT ($(du -h "$OUT" | cut -f1))"
