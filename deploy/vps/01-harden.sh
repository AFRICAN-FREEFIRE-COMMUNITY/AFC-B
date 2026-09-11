#!/bin/bash
# 01-harden.sh - runbook section 6, run as ubuntu (sudo) over SSH, AFTER key login is proven.
#
# Order matters and is the reason this is a separate file from 00-bootstrap-root.sh: password
# login is switched off here, so it must only run once `ssh ubuntu@NEW_IP` works with the key.
# Keep the provider's browser console open in another tab as the way back in.
#
# What it leaves behind: updated OS, sshd key-only + no root, ufw with 22/80/443 only (3306 is
# deliberately NOT open; the AWS box has MySQL exposed to the world and that is not inherited),
# fail2ban on sshd, unattended security upgrades.
#
# Proven by gates B1, B2, B3 in GATES-vps-migration.md.

set -eu
export DEBIAN_FRONTEND=noninteractive

sudo apt-get update -q
sudo apt-get upgrade -y -q

# sshd: key only, no root. Drop-in file so a package upgrade never overwrites it.
sudo install -d -m 755 /etc/ssh/sshd_config.d
sudo tee /etc/ssh/sshd_config.d/90-afc-hardening.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
# Ubuntu 24.04 cloud images sometimes ship a 50-cloud-init.conf that forces PasswordAuthentication yes.
# A later-sorted drop-in does NOT win in sshd (first match wins), so neutralise it explicitly.
if [ -f /etc/ssh/sshd_config.d/50-cloud-init.conf ]; then
  sudo sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config.d/50-cloud-init.conf
fi
sudo sshd -t
sudo systemctl restart ssh

# firewall
sudo apt-get install -y -q ufw
sudo ufw --force reset >/dev/null
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

# brute-force protection + automatic security updates
sudo apt-get install -y -q fail2ban unattended-upgrades
sudo tee /etc/fail2ban/jail.d/sshd.local >/dev/null <<'EOF'
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
EOF
sudo systemctl enable --now fail2ban
sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
sudo systemctl enable --now unattended-upgrades

# timezone + hostname: the API stores UTC; keep the box on UTC so logs and cron agree with the DB
sudo timedatectl set-timezone UTC
sudo hostnamectl set-hostname afc-prod-1

echo "HARDEN_OK"
sudo sshd -T | grep -E "^(permitrootlogin|passwordauthentication) "
sudo ufw status | sed -n '1,8p'
systemctl is-active fail2ban unattended-upgrades
