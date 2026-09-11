#!/bin/bash
# aws-gather.sh - run ONCE on the OLD AWS box (ubuntu@3.80.44.105) before the move.
#
# Writes every fact the new VPS build needs into ~/aws-facts.md so nothing is copied from memory:
# the live nginx config, the real django_app unit, how the frontend container was started, what
# owns every listening port (the port 8080 mystery from the runbook), the size of media/, the
# exact Python packages in the venv, and whether any migration is unapplied.
#
# PRINTS NO SECRETS. .env is reported as variable NAMES only. Docker Hub login is reported as
# present/absent, never the token. Safe to scp back and keep in the repo.
#
# Consumed by: the migration operator (see deploy/vps/README.md), gate A2 in GATES-vps-migration.md.

set -u
OUT=~/aws-facts.md
exec > "$OUT" 2>&1

echo "# AWS box facts, gathered $(date -u +%Y-%m-%dT%H:%MZ) on $(hostname)"
echo

echo "## versions"
lsb_release -ds 2>/dev/null; uname -r
python3 --version; ~/AFC-B/venv/bin/python --version 2>/dev/null
mysql --version; redis-server --version; nginx -v 2>&1; docker --version 2>/dev/null
node --version 2>/dev/null; which tesseract chromium chromium-browser google-chrome chromedriver 2>/dev/null
echo

echo "## resources"
nproc; free -h; df -h / /home 2>/dev/null
echo

echo "## listening ports (who owns what, incl. 8080)"
sudo ss -tlnp
echo

echo "## systemd units of interest"
systemctl list-units --type=service --all | grep -iE "django|celery|gunicorn|redis|mysql|nginx|docker" || true
echo
for u in django_app celery-worker celery-beat celery-rankings celery-ocr-ml; do
  echo "### /etc/systemd/system/$u.service"
  sudo cat /etc/systemd/system/$u.service 2>/dev/null || echo "(absent)"
  echo
done

echo "## anything still in screen/tmux (double-beat check)"
ps aux | grep -E "celery|gunicorn|screen|tmux" | grep -v grep || true
echo

echo "## nginx"
ls -la /etc/nginx/sites-enabled/
for f in /etc/nginx/sites-enabled/*; do echo "### $f"; sudo cat "$f"; echo; done
echo "### /etc/nginx/nginx.conf (http block essentials)"
sudo grep -nE "client_max_body_size|worker_processes|keepalive|gzip on|include" /etc/nginx/nginx.conf
echo "### conf.d"
ls -la /etc/nginx/conf.d/ 2>/dev/null; sudo cat /etc/nginx/conf.d/*.conf 2>/dev/null
echo

echo "## TLS"
sudo ls -la /etc/letsencrypt/live/ 2>/dev/null || echo "(no letsencrypt dir)"
sudo certbot certificates 2>/dev/null || echo "(certbot absent or no certs)"
systemctl list-timers --all 2>/dev/null | grep -i certbot || true
echo

echo "## docker: frontend container"
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null
echo "### docker inspect (Config.Env, Cmd, HostConfig.PortBindings, RestartPolicy)"
docker inspect africanfreefire-frontend --format '{{json .Config.Env}} {{json .Config.Cmd}} {{json .HostConfig.PortBindings}} {{json .HostConfig.RestartPolicy}}' 2>/dev/null
echo "### docker hub login present?"
test -f ~/.docker/config.json && grep -q '"auths"' ~/.docker/config.json && echo "YES: ~/.docker/config.json has auths" || echo "NO docker login on this box"
echo "### images"
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}' 2>/dev/null
echo

echo "## AFC-B checkout"
cd ~/AFC-B || exit 1
git rev-parse --abbrev-ref HEAD; git rev-parse --short HEAD; git log -1 --format='%ci %s'
git status --porcelain | head -20
echo "### untracked migration files per app"
for d in afc_*/migrations; do printf "%-45s %s\n" "$d" "$(ls $d/*.py 2>/dev/null | grep -v __init__ | wc -l)"; done
echo

echo "## migrations state (code vs files vs DB)"
source venv/bin/activate
python manage.py makemigrations --check --dry-run 2>&1 | tail -20
echo "### unapplied:"
python manage.py showmigrations 2>/dev/null | grep "\[ \]" || echo "(none unapplied)"
echo

echo "## venv packages (exact pins to reproduce)"
venv/bin/pip freeze
echo

echo "## .env variable NAMES only"
grep -oE "^(export )?[A-Za-z_][A-Za-z0-9_]*\s*=" .env | sed 's/export //; s/\s*=//' | tr '\n' ' '; echo
echo

echo "## media"
du -sh media; find media -type f | wc -l
du -sh media/* 2>/dev/null | sort -rh | head -25
echo

echo "## other paths the code expects"
ls -la /home/ubuntu/ipinfo 2>/dev/null || echo "(no /home/ubuntu/ipinfo)"
ls -la /home/ubuntu/geoip 2>/dev/null || true
ls -la ~/AFC-B/celerybeat-schedule* 2>/dev/null || true
ls ~/AFC-B/static ~/AFC-B/staticfiles 2>/dev/null | head || echo "(no collected static dir)"
echo

echo "## crontab"
crontab -l 2>/dev/null || echo "(no user crontab)"
sudo crontab -l 2>/dev/null || echo "(no root crontab)"
echo

echo "## mysql"
sudo mysql -e "SELECT VERSION(); SELECT user,host FROM mysql.user; SELECT COUNT(*) AS tables_ FROM information_schema.TABLES WHERE TABLE_SCHEMA='afc_db'; SELECT COUNT(*) AS users FROM afc_db.afc_auth_user; SELECT table_schema, ROUND(SUM(data_length+index_length)/1024/1024,1) AS mb FROM information_schema.tables WHERE table_schema='afc_db';" 2>&1
sudo grep -rhE "^(bind-address|max_allowed_packet|innodb_buffer_pool_size|sql_mode)" /etc/mysql/ 2>/dev/null
echo

echo "## redis"
redis-cli info server | grep -E "redis_version|tcp_port"; redis-cli -n 0 llen celery; redis-cli -n 0 llen whatsapp; redis-cli -n 0 llen rankings_recalc; redis-cli -n 0 llen ocr_ml
echo

echo "## firewall"
sudo ufw status verbose 2>/dev/null || echo "(ufw absent)"
echo

echo "## DONE"
