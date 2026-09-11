#!/bin/bash
# 02-install.sh - runbook section 7, run as ubuntu (sudo) over SSH.
#
# Installs everything the AFC stack needs on one Ubuntu 24.04 box:
#   Python 3.12 + the native headers mysqlclient, opencv and pillow compile against
#   MySQL 8 (localhost only) and Redis
#   nginx + certbot with the Cloudflare DNS plugin (cert BEFORE the DNS flip, renewals that do not
#     depend on the orange cloud; see the chat decision 2 on 2026-09-11)
#   Docker (the frontend image from Docker Hub is how the frontend has always shipped)
#   libglib2.0-0 for opencv-python-headless (the only opencv in the production venv)
#
# NEVER npm on this project. Node is not installed on the host at all: the frontend runs inside its
# container and the image is built by GitHub Actions.
#
# Proven by gate B4.

set -eu
export DEBIAN_FRONTEND=noninteractive

sudo apt-get update -q
sudo apt-get install -y -q \
  python3 python3-venv python3-dev python3-pip \
  build-essential pkg-config default-libmysqlclient-dev \
  mysql-server redis-server \
  nginx certbot python3-certbot-nginx python3-certbot-dns-cloudflare \
  git rsync curl unzip htop ca-certificates gnupg \
  tesseract-ocr libgl1 libglib2.0-0

# Docker from the official script (what the runbook specifies). ubuntu joins the docker group so
# the frontend container can be managed without sudo; takes effect on the next login.
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker ubuntu
sudo systemctl enable --now docker

# MySQL: local only, utf8mb4 everywhere, a buffer pool sized for a 16 GB box that also runs the app.
sudo tee /etc/mysql/mysql.conf.d/99-afc.cnf >/dev/null <<'EOF'
[mysqld]
bind-address            = 127.0.0.1
mysqlx-bind-address     = 127.0.0.1
character-set-server    = utf8mb4
collation-server        = utf8mb4_unicode_ci
innodb_buffer_pool_size = 3G
max_allowed_packet      = 64M
max_connections         = 200
EOF
sudo systemctl enable --now mysql redis-server
sudo systemctl restart mysql

# Redis: local only is the Ubuntu default (bind 127.0.0.1 ::1). Assert it rather than assume it.
sudo grep -qE "^bind 127.0.0.1" /etc/redis/redis.conf || { echo "REDIS NOT BOUND TO LOCALHOST, FIX BEFORE CONTINUING"; exit 1; }

echo "INSTALL_OK"
python3 --version; mysql --version; redis-server --version | cut -d' ' -f1-3; nginx -v; certbot --version; docker --version
