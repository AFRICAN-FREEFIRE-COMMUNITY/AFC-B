# deploy/vps: the kit that moved AFC from AWS EC2 to the InterServer VPS (2026-09-11)

The spec is `docs/aws-to-vps-migration-runbook.md`. This folder is the runbook turned into files
that run, in the order below, plus the decisions taken on the day (chat, 2026-09-11):

- user `ubuntu`, path `/home/ubuntu/AFC-B`: same as AWS, so `GEOIP_PATH`, the systemd units and
  every hardcoded path work unchanged
- nginx + certbot with the Cloudflare DNS plugin, not Caddy: cert BEFORE the DNS flip, copies the
  proven AWS config, the maintenance kit is nginx
- copy, do not rebuild: rsync the whole checkout (migrations, .env, media), dump + load the DB,
  `docker pull` the frontend image
- only the DB password rotates today; the rest is a logged follow-up
- site email stays on M365 SMTP today

Acceptance is `GATES-vps-migration.md` at the repo root. Nothing here is "done" until its gate
holds evidence.

## Order

| # | file | runs on | as | what |
|---|---|---|---|---|
| 0 | `aws-gather.sh` | AWS box | ubuntu | writes `~/aws-facts.md` (kept OUTSIDE this public repo, in the owner's WEBSITE/deploy/vps/: it lists developer home IPs from the firewall table). No secrets |
| 1 | `00-bootstrap-root.sh` | VPS | root (owner pastes once) | ubuntu user, NOPASSWD sudo, operator key |
| 2 | `01-harden.sh` | VPS | ubuntu | updates, sshd key-only, ufw 22/80/443, fail2ban, unattended-upgrades, UTC |
| 3 | `02-install.sh` | VPS | ubuntu | Python 3.12 + headers, MySQL 8 (localhost, utf8mb4, 3G pool), Redis, nginx, certbot + cloudflare plugin, Docker, tesseract |
| 4 | `03-mysql.sh` | VPS | ubuntu | `afc_db` + user `afc` with a NEW password in `~/.afc-db-pass` |
| 5 | `04-sync-from-aws.sh` | VPS | ubuntu | rsync `AFC-B/` (minus venv) + `ipinfo/` from AWS. Re-run at cutover |
| 6 | DB dump on AWS, load on VPS | both | | see "Database" below. Re-run at cutover |
| 7 | `05-env-and-venv.sh` | VPS | ubuntu | DB lines in `.env`, venv from `aws-freeze.txt` (or requirements.txt) + gunicorn + django-redis, CPU torch |
| 8 | `06-services.sh` | VPS | ubuntu | django_app + celery-worker/beat/rankings enabled, ocr-ml disabled, frontend container on 127.0.0.1:3000 |
| 8b | `08-bot.sh` | VPS | ubuntu | Discord bot: `.env` + 7 state files off the afc-bot box, own venv from `bot-freeze.txt`, shared `BOT_CONTROL_TOKEN` in both env files, `afc-bot.service`. STOP the old bot on 52.73.8.218 first (gate D8) |
| 9 | `07-nginx-cert.sh` | VPS | ubuntu | LE cert via Cloudflare DNS-01 (needs `/etc/letsencrypt/cloudflare.ini`), `nginx-afc.conf`, maintenance page |
| 10 | hosts-file rehearsal | operator machine | | runbook section 13, Chrome, desktop + 390x844, gates E1-E8 |
| 11 | cutover | both + Cloudflare | | maintenance on AWS, final dump + rsync, A records, MX untouched. Gates F1-F5 |
| 12 | `afc-db-backup.sh` | VPS | root cron 03:15 UTC | nightly dump, 14 days. Gate G1 |
| 13 | `afc-offsite-backup.sh` | VPS | root cron 03:45 UTC | dumps + media to a bucket once the owner supplies one (`OWNER-RUNBOOK.md` section 3); silent no-op until then |
| 14 | `afc-restore-test.sh` | VPS | root cron, 1st of the month 04:30 UTC | restores the newest dump into a scratch DB, compares counts, drops it. RESTORE_TEST_OK on 2026-09-11 |
| 15 | `.github/workflows/uptime.yml` | GitHub | every 5 min | site 200 + `X-AFC-Host: vps`, api 400; a red run is the alert email |

Steps that need YOUR dashboards (key rotation, mail provider, the bucket, retiring AWS):
`OWNER-RUNBOOK.md`.

## Three source boxes, not one

The runbook assumed one EC2. Measured on the day: `afc-test-1` (backend account 211125329565,
3.80.44.105) runs the API + MySQL + Redis + Celery; `afc-frontend` (frontend account
835857361469, 34.230.23.212) runs the Next.js container behind its own nginx + certbot; `afc-bot`
(same frontend account, 52.73.8.218, t2.micro) runs the Discord bot from the archived standalone
repo. All three fold into the one VPS.

## Database

On AWS (site stays up, `--single-transaction` does not lock):

    mysqldump --single-transaction --routines --triggers --events --quick afc_db | gzip > ~/afc_db.sql.gz

On the VPS (pulled by rsync or scp, then):

    gunzip < ~/afc_db.sql.gz | mysql -u afc -p"$(cat ~/.afc-db-pass)" afc_db
    mysql -u afc -p"$(cat ~/.afc-db-pass)" afc_db -e "SELECT COUNT(*) FROM afc_auth_user; SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='afc_db';"

Then, because migration files are gitignored and generated on the server (runbook trap 3), check
the files that rsync carried agree with the loaded schema, and apply anything the last releases
left owed (v7.1.77's invitation-note widening was still owed on 2026-09-01):

    cd ~/AFC-B && venv/bin/python manage.py makemigrations --check --dry-run
    venv/bin/python manage.py showmigrations | grep '\[ \]'
    venv/bin/python manage.py migrate

NEVER `migrate` a fresh empty database on the VPS. Load the dump first, always.

## Cutover (runbook section 16, compressed)

1. AWS: `sudo cp /etc/nginx/sites-enabled/<site> /root/site.bak` then switch both server blocks to
   return the maintenance page (`return 503` + the `@maintenance` location), `nginx -t`, reload.
   Writes stop here.
2. AWS: final `mysqldump` (command above). VPS: `04-sync-from-aws.sh` again (seconds), then load
   the dump over the rehearsal copy: `mysql -e "DROP DATABASE afc_db; CREATE DATABASE afc_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"` then the load. Recheck gates C2, C3.
3. VPS: `sudo systemctl restart django_app celery-worker celery-beat celery-rankings`.
4. Cloudflare DNS: A `@` and A `api` (and `www` if present) to NEW_IP, proxied as before. MX rows:
   do not touch. `nslookup -type=MX africanfreefirecommunity.com 1.1.1.1` must match the pre-change screenshot.
5. Probe: `curl -sI https://api.africanfreefirecommunity.com/ | grep -i x-afc-host` prints `vps`.
6. Walk the real domain in Chrome, desktop + mobile (gate F5).
7. AWS stays RUNNING, untouched apart from the maintenance page, for two weeks. Rollback = A
   records back to `3.80.44.105`.

## Follow-ups this move deliberately did not do

Rotate Django secret, Discord, Paystack, Stripe, Gemini keys (runbook trap 8). Move site mail to
SES (runbook decision 4). Offsite backups to Backblaze B2 and a tested restore (section 17).
UptimeRobot. Resolve or document whatever owned port 8080 on AWS (trap 10; `aws-facts.md` says).
