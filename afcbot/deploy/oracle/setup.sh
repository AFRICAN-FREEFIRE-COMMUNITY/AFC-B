#!/usr/bin/env bash
# AFC Discord bot — Oracle Cloud Ubuntu 22.04 (Ampere A1) provision script.
# Idempotent: safe to re-run for redeploys.
#
# Usage:
#   sudo bash setup.sh                # first-time install
#   sudo bash setup.sh --update       # pull latest + restart only

set -euo pipefail

REPO_URL="https://github.com/AFRICANFREEFIRECOMMUNITY/AFC-Bot.git"
APP_USER="ubuntu"
APP_HOME="/home/${APP_USER}"
APP_DIR="${APP_HOME}/AFC-Bot"
VENV_DIR="${APP_DIR}/.venv"
SERVICE_NAME="afc-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="/usr/bin/python3.11"

UPDATE_ONLY=0
if [[ "${1:-}" == "--update" ]]; then
    UPDATE_ONLY=1
fi

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash setup.sh"
    exit 1
fi

if [[ $UPDATE_ONLY -eq 0 ]]; then
    echo "==> Installing system packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y software-properties-common ca-certificates curl git build-essential ffmpeg \
        libffi-dev libssl-dev libnacl-dev libopus-dev pkg-config

    # Python 3.11 via deadsnakes
    if ! command -v python3.11 >/dev/null 2>&1; then
        add-apt-repository -y ppa:deadsnakes/ppa
        apt-get update -y
        apt-get install -y python3.11 python3.11-venv python3.11-dev
    fi

    echo "==> Cloning repo into ${APP_DIR}"
    if [[ ! -d "${APP_DIR}/.git" ]]; then
        sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}"
    fi
fi

echo "==> Syncing to origin/main"
# reset --hard (not pull --ff-only): the running bot leaves the tree dirty
# (knowledge_base.txt is rewritten by auto_scrape_loop), which makes a plain pull
# fail. git is the source of truth here. reset --hard updates tracked files only
# and leaves UNTRACKED files (the gitignored seen_*.json / conversation_history.json)
# in place, so dedup/seen-state survives the deploy — no announcement re-spam.
sudo -u "${APP_USER}" git -C "${APP_DIR}" fetch --all --prune
sudo -u "${APP_USER}" git -C "${APP_DIR}" checkout main
sudo -u "${APP_USER}" git -C "${APP_DIR}" reset --hard origin/main

echo "==> Creating venv + installing requirements"
if [[ ! -d "${VENV_DIR}" ]]; then
    sudo -u "${APP_USER}" "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
sudo -u "${APP_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip wheel
sudo -u "${APP_USER}" "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [[ ! -f "${APP_DIR}/.env" ]]; then
    echo "==> Seeding empty .env (PASTE YOUR SECRETS HERE BEFORE STARTING)"
    sudo -u "${APP_USER}" tee "${APP_DIR}/.env" >/dev/null <<'EOF'
DISCORD_TOKEN=
OPENAI_API_KEY=
EOF
    chmod 600 "${APP_DIR}/.env"
    chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
fi

echo "==> Writing systemd unit"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=AFC Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
# Unbuffered stdout so the bot's print() diagnostics (failover, ⚠️ warnings,
# poll-loop status) reach journald immediately instead of sitting in a buffer.
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

if [[ -s "${APP_DIR}/.env" ]] && grep -q "DISCORD_TOKEN=." "${APP_DIR}/.env"; then
    echo "==> Restarting service"
    RESTART_AT="$(date '+%Y-%m-%d %H:%M:%S')"
    systemctl restart "${SERVICE_NAME}"

    # ── Post-deploy health check ──────────────────────────────────────────────
    # "systemctl restart succeeded" is NOT "the bot is up": a login failure
    # (e.g. PrivilegedIntentsRequired when a privileged intent isn't enabled in
    # the Discord Developer Portal, or a bad token) leaves the service crash-
    # looping while the deploy reports green — exactly what happened 2026-07-02
    # (33-minute silent outage). Wait for the on_ready "online as" line in the
    # journal; fail the deploy loudly if it never appears.
    echo "==> Health check: waiting for Discord login (up to 90s)"
    HEALTH_OK=0
    for _ in $(seq 1 18); do
        sleep 5
        if journalctl -u "${SERVICE_NAME}" --since "${RESTART_AT}" --no-pager 2>/dev/null \
                | grep -q "AFC Bot is online as"; then
            HEALTH_OK=1
            break
        fi
        # Login-fatal errors never self-heal — stop waiting immediately.
        if journalctl -u "${SERVICE_NAME}" --since "${RESTART_AT}" --no-pager 2>/dev/null \
                | grep -qE "PrivilegedIntentsRequired|LoginFailure|Improper token"; then
            break
        fi
    done

    if [[ ${HEALTH_OK} -eq 1 ]]; then
        echo "==> Health check PASSED — bot logged in."
        systemctl status "${SERVICE_NAME}" --no-pager || true
    else
        echo "==> Health check FAILED — bot did not log in after restart. Recent logs:"
        journalctl -u "${SERVICE_NAME}" --since "${RESTART_AT}" --no-pager | tail -30 || true
        exit 1
    fi
else
    echo "==> .env missing secrets. Edit ${APP_DIR}/.env then run: sudo systemctl restart ${SERVICE_NAME}"
fi

echo "==> Done."
echo "Logs: sudo journalctl -u ${SERVICE_NAME} -f"
echo "Update: sudo bash ${APP_DIR}/deploy/oracle/setup.sh --update"
