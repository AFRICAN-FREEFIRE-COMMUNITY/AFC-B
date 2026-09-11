#!/bin/bash
# afc-restore-test.sh - prove the nightly dump restores. "A backup you have never restored is a
# hope, not a backup" (runbook section 17). Runs as root (cron, monthly, or by hand).
#
# Loads the newest /var/backups/afc/afc_db-*.sql.gz into a scratch database afc_db_restoretest,
# compares the row count of a few busy tables with the live database, prints RESTORE_TEST_OK
# only when every table restored with a non-zero count and the scratch DB's table count matches
# live, then drops the scratch database. Never touches afc_db.
set -euo pipefail
DIR=/var/backups/afc
latest=$(ls -1t "$DIR"/afc_db-*.sql.gz 2>/dev/null | head -1)
[ -n "$latest" ] || { echo "no dump in $DIR"; exit 1; }
echo "restoring $(basename "$latest") ($(du -h "$latest" | cut -f1))"
mysql -e "DROP DATABASE IF EXISTS afc_db_restoretest; CREATE DATABASE afc_db_restoretest CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
gunzip < "$latest" | mysql afc_db_restoretest
ok=1
live_tables=$(mysql -N -e "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='afc_db'")
test_tables=$(mysql -N -e "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='afc_db_restoretest'")
echo "tables: live=$live_tables restored=$test_tables"
[ "$live_tables" = "$test_tables" ] || ok=0
for t in afc_auth_user afc_tournament_and_scrims_event afc_team_team afc_auth_notifications; do
  l=$(mysql -N -e "SELECT COUNT(*) FROM afc_db.$t" 2>/dev/null || echo "?")
  r=$(mysql -N -e "SELECT COUNT(*) FROM afc_db_restoretest.$t" 2>/dev/null || echo "?")
  printf '%-40s live=%-8s restored=%s\n' "$t" "$l" "$r"
  [ "$r" != "?" ] && [ "$r" -gt 0 ] || ok=0
done
mysql -e "DROP DATABASE afc_db_restoretest;"
[ "$ok" = 1 ] && echo "RESTORE_TEST_OK $(date -u +%FT%TZ)" || { echo "RESTORE_TEST_FAILED"; exit 1; }
