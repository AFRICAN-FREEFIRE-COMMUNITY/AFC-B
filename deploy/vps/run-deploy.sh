#!/bin/bash
# run-deploy.sh - the ONLY thing the GitHub deploy keys are allowed to run (forced command).
#
# Installed OUTSIDE the repo at /home/ubuntu/deploy-vps/run-deploy.sh and named in
# ~/.ssh/authorized_keys as
#   command="/home/ubuntu/deploy-vps/run-deploy.sh",no-port-forwarding,no-agent-forwarding,no-pty,no-X11-forwarding ssh-ed25519 ... github-deploy-<repo>
# so a leaked key buys an attacker exactly one thing: a deploy of a ref that already exists on
# GitHub, or of an image tag that already exists on Docker Hub. No shell, no arbitrary command,
# no tunnels. deploy-backend.sh re-installs this file from the repo on every run, so the copy on
# the box never drifts from the reviewed one.
#
# The client's requested command arrives in $SSH_ORIGINAL_COMMAND (sshd sets it when a forced
# command is in effect). Two shapes, from the two workflows:
#   backend  <ref>         .github/workflows/deploy-backend.yml (AFC-B): fetch + check out that
#                          branch in /home/ubuntu/AFC-B, then deploy/vps/deploy-backend.sh
#   frontend <image-tag>   .github/workflows/docker-image-live.yml (AFC_Frontend): does NOT touch
#                          git; runs deploy/vps/deploy-frontend.sh from whatever backend checkout
#                          is on the box, with the tag the workflow just built (sha-<commit>, so
#                          the swap deploys exactly what was built, never a racing "latest")
#
# One deploy at a time: flock on a lock file, 10 minute wait, then give up loudly (exit 75).
# GitHub's `concurrency` group serialises the runs too; this is the belt to that brace.

set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
REPO=/home/ubuntu/AFC-B
LOCK=/home/ubuntu/deploy-vps/.deploy.lock
LOG=/home/ubuntu/deploy-vps/deploy.log

read -r target arg _rest <<<"${SSH_ORIGINAL_COMMAND:-}"
case "${target:-}" in
  backend)
    ref="${arg:-main}"
    # A ref is a branch name and nothing else: no spaces, no shell characters, no leading dash.
    [[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$ ]] || { echo "refused: bad ref '$ref'"; exit 64; }
    ;;
  frontend)
    tag="${arg:-}"
    [[ "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ ]] || { echo "refused: bad image tag '$tag'"; exit 64; }
    ;;
  *) echo "usage: backend <ref> | frontend <image-tag>   (got: '${SSH_ORIGINAL_COMMAND:-}')"; exit 64 ;;
esac

exec 9>"$LOCK"
flock -w 600 9 || { echo "another deploy still holds the lock after 10 minutes"; exit 75; }

{
  echo "=== $(date -u +%FT%TZ) $target ${ref:+ref=$ref}${tag:+tag=$tag} ==="
  cd "$REPO"
  if [ "$target" = backend ]; then
    git fetch -q --prune origin
    git rev-parse -q --verify "origin/$ref" >/dev/null || { echo "refused: origin/$ref does not exist"; exit 65; }
    # checkout -f -B: create or move the local branch to the remote tip and OVERWRITE any local
    # edit to a tracked file. Without -f the very second deploy went red (2026-09-11 16:37,
    # "Your local changes ... would be overwritten"): a hand-copied script was one cause, and the
    # bot's 3-hourly knowledge scrape rewriting the tracked afcbot/knowledge_base.txt is the one
    # that would have recurred forever. Ignored files (migrations, .env, media/, venv/) and
    # untracked files not in the way are left alone; a deploy deploys the branch, nothing else.
    git checkout -q -f -B "$ref" "origin/$ref"
    echo "checkout: $(git rev-parse --short HEAD) $(git log -1 --format=%s)"
    exec bash deploy/vps/deploy-backend.sh
  else
    test -f deploy/vps/deploy-frontend.sh || { echo "deploy/vps/deploy-frontend.sh missing on the box: run a backend deploy first"; exit 66; }
    exec bash deploy/vps/deploy-frontend.sh "$tag"
  fi
} 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
