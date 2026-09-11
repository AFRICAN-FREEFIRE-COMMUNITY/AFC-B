#!/bin/bash
# deploy-backend.sh - put the checked-out AFC-B commit live on the VPS with no dropped request.
#
# Called by run-deploy.sh (the forced command behind the GitHub deploy key) after it has fetched
# and checked out the requested branch in /home/ubuntu/AFC-B. Also fine to run by hand:
#     cd ~/AFC-B && git pull && bash deploy/vps/deploy-backend.sh
#
# Why each step is what it is:
#   pip install -r requirements-prod.txt   the 59 production pins; a no-op in ~2 s when nothing
#                                          changed, so it runs every time rather than "when needed"
#   makemigrations --noinput               this project generates migration files ON THE SERVER
#                                          (gitignored, team convention since 2026-06-08). A model
#                                          change that needs a human answer (a new non-null field
#                                          with no default) makes makemigrations exit non-zero
#                                          here, which fails the run BEFORE anything is reloaded.
#   migrate --noinput                      schema first, code second: additive migrations are
#                                          safe for the old workers still serving during reload
#   manage.py check                        import-level smoke test of the new code
#   systemctl RELOAD django_app            SIGHUP to the gunicorn master: it starts new workers
#                                          on the new code and retires old ones after they finish
#                                          their current request. No 502, no maintenance page.
#   restart celery-* and afc-bot           workers stop after the task in flight (TimeoutStopSec
#                                          300); queued tasks wait in Redis; the bot reconnects to
#                                          Discord in a couple of seconds. Nothing user-visible.
#   health probe                           an auth-required endpoint must answer 400 (Django is
#                                          up and routing) within 30 s or the run goes red
#
# The trampoline is re-installed from this checkout at the end so the reviewed copy in git is
# always the one the deploy key runs.

set -euo pipefail
cd /home/ubuntu/AFC-B
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
t0=$(date +%s)
echo "deploying $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

venv/bin/pip install -q -r requirements-prod.txt
venv/bin/python manage.py makemigrations --noinput
venv/bin/python manage.py migrate --noinput
venv/bin/python manage.py check

sudo systemctl reload django_app
sudo systemctl restart celery-worker celery-beat celery-rankings afc-bot

code=""
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' -H 'Host: api.africanfreefirecommunity.com' http://127.0.0.1:8000/auth/connections/ || true)
  [ "$code" = "400" ] && break
  sleep 1
done
if [ "$code" != "400" ]; then
  echo "API health probe failed after 30 s (got '$code'); last gunicorn log lines:"
  sudo journalctl -u django_app -n 20 --no-pager -o cat
  exit 1
fi
for u in django_app celery-worker celery-beat celery-rankings afc-bot; do
  systemctl is-active --quiet "$u" || { echo "$u is not active after deploy"; sudo journalctl -u "$u" -n 20 --no-pager -o cat; exit 1; }
done

# keep the forced-command trampoline in sync with the repo
install -m 755 deploy/vps/run-deploy.sh /home/ubuntu/deploy-vps/run-deploy.sh

echo "DEPLOYED backend $(git rev-parse --short HEAD) in $(( $(date +%s) - t0 )) s"
