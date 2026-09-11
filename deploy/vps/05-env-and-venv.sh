#!/bin/bash
# 05-env-and-venv.sh - point .env at the new local database and build the venv. Run on the VPS as ubuntu.
#
# .env arrived via rsync with the AWS values. Only the three database lines change: user afc,
# the password from /home/ubuntu/.afc-db-pass (03-mysql.sh), host localhost. Everything else
# (Discord, Paystack, Stripe, Gemini, WhatsApp, the OIDC RSA key) stays as it was; rotating those
# is a logged follow-up, not a cutover step. sed edits whole lines matched by name, so the
# multi-line RSA key in the same file is untouched.
#
# venv: built from deploy/vps/aws-freeze.txt, the pip freeze of the AWS venv, because that is
# what production actually runs (see the measured note below). requirements.txt is the fallback.
#
# Proven by gates C1, C5, C6.

set -eu
cd /home/ubuntu/AFC-B
test -f .env || { echo ".env missing, run 04-sync-from-aws.sh first"; exit 1; }
PASS=$(cat /home/ubuntu/.afc-db-pass)

cp -n .env .env.aws-original   # keep the pre-edit copy once; never overwritten
# The AWS .env writes keys as "DB_USER = value" (spaces round the =), so match loosely and
# rewrite the whole line in the same style. python-dotenv accepts both forms.
sed -i -E "s|^\s*DB_USER\s*=.*|DB_USER = afc|; s|^\s*DB_PASSWORD\s*=.*|DB_PASSWORD = ${PASS}|; s|^\s*DB_HOST\s*=.*|DB_HOST = localhost|; s|^\s*DB_PORT\s*=.*|DB_PORT = 3306|" .env
grep -qE "^\s*DB_NAME\s*=" .env || echo "DB_NAME = afc_db" >> .env
grep -qE "^\s*DEBUG\s*=" .env || echo "DEBUG = False" >> .env
echo "env: $(grep -cE '^\s*(DB_NAME|DB_USER|DB_PASSWORD|DB_HOST|DB_PORT)\s*=' .env)/5 db lines set, DEBUG=$(grep -E '^\s*DEBUG\s*=' .env | sed 's/.*=\s*//')"

python3 -m venv venv
venv/bin/pip install --upgrade pip wheel setuptools -q
# MEASURED 2026-09-11: the AWS venv holds 59 packages (deploy/vps/aws-freeze.txt), NOT the 181
# lines of requirements.txt. No torch, no selenium, no tesseract; Django 5.2.7 not 5.2.16. That
# list is what production actually runs and it already contains gunicorn and django-redis, so it
# is installed verbatim and nothing is added on top (an extra pin would silently downgrade).
if [ -s /home/ubuntu/deploy-vps/aws-freeze.txt ]; then
  echo "installing from aws-freeze.txt (production pins, 59 packages)"
  venv/bin/pip install -q -r /home/ubuntu/deploy-vps/aws-freeze.txt
else
  echo "aws-freeze.txt missing, falling back to requirements.txt + the two production extras"
  venv/bin/pip install -q -r requirements.txt
  venv/bin/pip install -q gunicorn django-redis
fi

venv/bin/python -c "import django, gunicorn, django_redis, MySQLdb, cv2, PIL; print('imports ok, django', django.get_version())"
venv/bin/python manage.py check --deploy 2>&1 | tail -15 || true
echo "VENV_OK"
