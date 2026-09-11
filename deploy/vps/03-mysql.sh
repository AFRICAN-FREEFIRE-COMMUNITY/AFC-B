#!/bin/bash
# 03-mysql.sh - create afc_db and the afc user with a NEW password (runbook section 7 + trap 8).
#
# The database password is the one secret rotated during the move: the AWS box has 3306 open to
# the world and a plaintext password in a commented settings block, so the old value is treated as
# burned. The new one is generated here, stored ONLY in /home/ubuntu/.afc-db-pass (mode 600) and
# written into .env by 05-env.sh. It is never printed.
#
# Idempotent: re-running keeps the existing password file if there is one.
#
# Proven by gate B5.

set -eu
PASSFILE=/home/ubuntu/.afc-db-pass
if [ ! -s "$PASSFILE" ]; then
  umask 077
  openssl rand -base64 30 | tr -d '/+=' | cut -c1-32 > "$PASSFILE"
fi
PASS=$(cat "$PASSFILE")

sudo mysql <<EOF
CREATE DATABASE IF NOT EXISTS afc_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'afc'@'localhost' IDENTIFIED BY '${PASS}';
ALTER USER 'afc'@'localhost' IDENTIFIED BY '${PASS}';
GRANT ALL PRIVILEGES ON afc_db.* TO 'afc'@'localhost';
-- Django's test runner and a future restore both want to create scratch databases; keep that
-- possible without handing out global rights on anything that already exists.
GRANT CREATE ON *.* TO 'afc'@'localhost';
FLUSH PRIVILEGES;
EOF

echo "MYSQL_OK"
sudo mysql -e "SELECT SCHEMA_NAME, DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='afc_db'; SELECT user,host FROM mysql.user WHERE user='afc';"
mysql -u afc -p"$PASS" -e "SELECT 'afc user can connect' AS check_;" afc_db
