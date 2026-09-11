#!/bin/bash
# 08-bot.sh - move the Discord bot onto the VPS as part of the backend checkout. Run as ubuntu.
#
# Added 2026-09-11 mid-migration when the owner said "remember we wanted to merge the bot to the
# website also". Facts gathered that day (deploy/vps/bot-facts section of aws-facts): the live bot
# runs on a THIRD box, afc-bot (t2.micro, 52.73.8.218), from the archived standalone repo
# ~/AFC-Bot at c2441c3 (2026-08-18), Python 3.11, 46 packages (deploy/vps/bot-freeze.txt), unit
# afc-bot.service, no control API. The absorbed copy at AFC-B/afcbot/ (bot.py 280 KB vs the
# standalone's 258 KB) is the one with the control API the /a/bot admin page talks to, and it is
# what runs here.
#
# What moves from the bot box: afcbot/.env (10 vars: DISCORD_TOKEN, OPENAI_API_KEY, two fallback
# provider sets) and the runtime state files git ignores on purpose: seen_events.json,
# seen_event_statuses.json, seen_news.json, seen_ban_activities.json, conversation_history.json,
# pending_event_approvals.json, rejected_event_ids.json, plus the freshest knowledge_base.txt.
# Without the seen_* files the bot re-announces every tournament it has ever seen on first start.
#
# The VPS pulls with its own key (~/.ssh/id_ed25519), appended to the bot box's authorized_keys
# by the operator, same as for the backend box.
#
# BOT_CONTROL_TOKEN: generated here once, written to BOTH afcbot/.env and the Django .env (they
# must match or /a/bot 401s against a healthy bot), together with BOT_CONTROL_URL. The token
# never leaves the box.
#
# Proven by gates D7, D8.

set -eu
BOT_SRC=ubuntu@52.73.8.218:AFC-Bot
DST=/home/ubuntu/AFC-B/afcbot
cd /home/ubuntu/AFC-B
test -d afcbot || { echo "afcbot/ missing: the checkout is older than 2026-08-18"; exit 1; }

# 1. secrets + state off the bot box
rsync -az "$BOT_SRC/.env" "$DST/.env"
rsync -az --include='*.json' --include='knowledge_base.txt' --exclude='*' "$BOT_SRC/" "$DST/"
chmod 600 "$DST/.env"
echo "state files: $(ls "$DST"/*.json | wc -l), env vars: $(grep -cE '^[A-Z_]+=' "$DST/.env")"

# 2. its own venv (deliberately separate, see deploy/systemd/afc-bot.service header)
python3 -m venv "$DST/venv"
"$DST/venv/bin/pip" install -q --upgrade pip wheel
if [ -s /home/ubuntu/deploy-vps/bot-freeze.txt ]; then
  "$DST/venv/bin/pip" install -q -r /home/ubuntu/deploy-vps/bot-freeze.txt
else
  "$DST/venv/bin/pip" install -q -r "$DST/requirements.txt"
fi
"$DST/venv/bin/python" -c "import discord, openai, aiohttp, nacl; print('bot imports ok, discord.py', discord.__version__)"

# 3. control API wiring, both sides, idempotent
if ! grep -qE '^BOT_CONTROL_TOKEN=' "$DST/.env"; then
  TOKEN=$(openssl rand -hex 32)
  printf '\nBOT_CONTROL_TOKEN=%s\nBOT_CONTROL_HOST=127.0.0.1\nBOT_CONTROL_PORT=8099\n' "$TOKEN" >> "$DST/.env"
fi
TOKEN=$(grep -E '^BOT_CONTROL_TOKEN=' "$DST/.env" | cut -d= -f2)
sed -i -E '/^\s*BOT_CONTROL_(TOKEN|URL)\s*=/d' .env
printf '\nBOT_CONTROL_URL = http://127.0.0.1:8099\nBOT_CONTROL_TOKEN = %s\n' "$TOKEN" >> .env

# 4. unit from the repo
sudo cp deploy/systemd/afc-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now afc-bot
sudo systemctl restart django_app
sleep 10
echo "BOT_OK"
systemctl is-active afc-bot
sudo journalctl -u afc-bot -n 8 --no-pager -o cat | cut -c1-140
sudo ss -tlnp | grep 8099 || echo "(no 8099 listener yet: check the journal above)"
