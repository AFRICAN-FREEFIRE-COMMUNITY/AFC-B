#!/bin/bash
# 06-services.sh - install and start the API, the three Celery units and the frontend container.
# Run on the VPS as ubuntu after 05-env-and-venv.sh and after the database has been loaded.
#
# Units come from the repo, not from memory: django_app.service sits next to this script, the
# Celery units are backend/deploy/systemd/ (the ones that fixed the double-beat and the four
# unconsumed queues on 2026-08-05). celery-ocr-ml is copied but NOT enabled, per the README in that
# folder: on the free Gemini tier it takes live OCR down with it.
#
# The frontend is the Docker Hub image GitHub Actions built for the last tag; same domains, so no
# rebuild (NEXT_PUBLIC_* values are baked at build time, runbook trap 7). The run command is the
# one every deploy has used (reference-how-to-ship-afc), with the container's port bound to
# loopback so only nginx can reach it.
#
# Proven by gates D1, D2, D3, D4.

set -eu
REPO=/home/ubuntu/AFC-B
KIT=/home/ubuntu/deploy-vps

sudo cp "$KIT/django_app.service" /etc/systemd/system/
sudo cp "$REPO/deploy/systemd/celery-worker.service"   /etc/systemd/system/
sudo cp "$REPO/deploy/systemd/celery-beat.service"     /etc/systemd/system/
sudo cp "$REPO/deploy/systemd/celery-rankings.service" /etc/systemd/system/
sudo cp "$REPO/deploy/systemd/celery-ocr-ml.service"   /etc/systemd/system/
sudo systemctl daemon-reload
# REHEARSAL=1 (the default until cutover): only the API starts. Beat and the workers send real
# emails, WhatsApp and Discord messages from whatever database they see, and the rehearsal copy is
# the same data AWS is still serving, so running both would notify every player twice. They are
# enabled here (start on boot) but started only by the cutover step, once AWS's are stopped.
sudo systemctl enable --now django_app
sudo systemctl enable celery-worker celery-beat celery-rankings
if [ "${REHEARSAL:-1}" = "0" ]; then
  sudo systemctl start celery-worker celery-beat celery-rankings
fi
sudo systemctl disable --now celery-ocr-ml 2>/dev/null || true

# frontend
if [ ! -f /home/ubuntu/.docker/config.json ]; then
  echo "NOTE: no Docker Hub login on this box. If the pull below fails with 'pull access denied', run: docker login"
fi
docker pull afctech/afc-frontend:latest
docker rm -f africanfreefire-frontend 2>/dev/null || true
docker run -d --name africanfreefire-frontend \
  -p 127.0.0.1:3000:3000 \
  --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  afctech/afc-frontend:latest

sleep 8
echo "SERVICES:"
systemctl is-active django_app celery-worker celery-beat celery-rankings || true
echo "beat processes (0 in rehearsal, 1 after cutover): $(pgrep -fc 'celery -A afc beat' || true)"
echo "api local probe (expect 400): $(curl -s -o /dev/null -w '%{http_code}' -H 'Host: api.africanfreefirecommunity.com' http://127.0.0.1:8000/auth/connections/)"
echo "frontend local probe: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/)"
docker inspect -f 'container {{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}' africanfreefire-frontend
