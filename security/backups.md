# Database backups (owner rule R82)

One row per database. Schedule and retention are what the provider is set to; "restore rehearsed on"
is the date somebody restored a backup into a scratch database and read a row back. Re-rehearse
every 90 days. `check-security --rule R82` reads this file.

Provider notes: Supabase (Database > Backups: daily on Pro, PITR add-on); Heroku Postgres
(`heroku pg:backups:schedule DATABASE_URL --at "02:00 Africa/Lagos"`, `heroku pg:backups:restore`);
Neon (branch snapshots, `pg_dump` to S3 by cron); PlanetScale (automatic daily, restore = branch);
MongoDB Atlas (Cloud Backup, point-in-time on M10+); self-hosted (`pg_dump` / `mysqldump` nightly
to object storage with a lifecycle rule).

| Database | Provider | Schedule | Retention | Backups go to | Restore rehearsed on | By |
|---|---|---|---|---|---|---|
| mysql (afc_db, the production database on the VPS) | self-hosted MySQL on the InterServer VPS (162.35.123.2) | nightly 03:15 UTC `afc-db-backup.sh` (mysqldump --single-transaction --routines --triggers --events, root cron); 03:45 UTC `afc-offsite-backup.sh` copies every dump and syncs media/ | 14 nightly dumps on the box (/var/backups/afc); the Backblaze bucket keeps what its lifecycle rule says | Backblaze B2 bucket `afc-offsite-backups` (db/ + media/), via the rclone remote `offsite` in root's rclone.conf | 2026-09-11 | Claude, `afc-restore-test.sh` on the box: restored afc_db-2026-09-11-1542.sql.gz into afc_db_restoretest, 233 tables both sides, users 8776/8773, events 147/145, teams 848/847 -> RESTORE_TEST_OK; cron re-runs it on the 1st of every month 04:30 UTC (/var/log/afc-restore-test.log) |
| redis (Celery broker on db 0, Django cache on db 1) | self-hosted on the same VPS | not backed up on purpose: it holds queued tasks and cache entries only, both rebuilt on start (beat re-issues its schedule, the cache refills); nothing lives here that a restore could bring back | none (ephemeral) | nowhere | 2026-09-11 | Claude: the VPS migration brought the site up on an EMPTY redis (HANDOVER-2026-09-11.md), which is the rehearsal this row needs |

Where the scripts live: `deploy/vps/afc-db-backup.sh`, `afc-offsite-backup.sh`, `afc-restore-test.sh`
(installed to /usr/local/sbin by the deploy kit). The last runs are in /var/log/afc-db-backup.log and
/var/log/afc-offsite-backup.log on the box; on 2026-09-17 the offsite copy reported db 54.8 MB and
media 2.44 GB. The AWS boxes (frozen 2026-09-11) hold the pre-migration copy until the owner
terminates them.
