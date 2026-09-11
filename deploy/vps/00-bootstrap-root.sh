#!/bin/bash
# 00-bootstrap-root.sh - the ONE block the owner pastes as root on the brand-new VPS.
#
# Everything after this is driven over SSH from the operator's machine as `ubuntu`. This block
# only does what cannot be done remotely yet: create that user, give it passwordless sudo (so
# non-interactive `sudo -n` works from scripts), and install the operator's public key.
#
# The username is `ubuntu` ON PURPOSE (not `afc` as the runbook first suggested): the code and the
# systemd units in backend/deploy/systemd/ hardcode /home/ubuntu/AFC-B and /home/ubuntu/ipinfo.
# Same name, nothing to edit.
#
# Password login and root login are NOT disabled here. That happens in 01-harden.sh, only after
# key login as ubuntu has been proven from the outside (runbook section 6, "order matters").

set -eu
PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDG/NhZvYMWOU4YSp0KYHcVbDPXPabR4fxZsIA6BOGdV claude-afc-migration-2026-09-11'

id ubuntu >/dev/null 2>&1 || adduser --disabled-password --gecos "AFC service user" ubuntu
usermod -aG sudo ubuntu
echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-ubuntu-nopasswd
chmod 440 /etc/sudoers.d/90-ubuntu-nopasswd

install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
grep -qF "$PUBKEY" /home/ubuntu/.ssh/authorized_keys 2>/dev/null || echo "$PUBKEY" >> /home/ubuntu/.ssh/authorized_keys
chmod 600 /home/ubuntu/.ssh/authorized_keys
chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys

echo "BOOTSTRAP_OK $(hostname) $(hostname -I | awk '{print $1}')"
