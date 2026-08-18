# Deploying the AFC bot on AWS (EC2 / Lightsail) + auto-deploy on push

The bot is a long-running worker (persistent Discord gateway connection, needs
`ffmpeg`/`libopus` for stage transcription, writes local state files). That means
a small **VM** — EC2 or Lightsail — not Lambda/App Runner/Fargate.

The provisioning script (`deploy/oracle/setup.sh`) is **cloud-agnostic** — plain
Ubuntu 22.04 — so it runs identically on AWS. It lives under `deploy/oracle/` for
historical reasons; there is nothing Oracle-specific in it.

---

## 1. Launch the instance

EC2 Console → **Launch instance**:

- **AMI:** Ubuntu Server **22.04 LTS** (NOT Amazon Linux — the script uses `apt`,
  the deadsnakes PPA, and the default user `ubuntu`).
- **Type:** `t3.micro` (free-tier eligible for 12 months). Bump to `t3.small`
  (2 GB) only if stage voice-recording runs out of memory.
- **Key pair:** create one and download the `.pem` (for your own admin SSH).
- **Storage:** default 8 GB is plenty.
- **Security group (inbound):**
  - `SSH (22)` from **My IP** — for you to administer.
  - `SSH (22)` reachable by GitHub Actions runners — required for the auto-deploy
    (see [§4 security note](#ssh-exposure-tradeoff)).
  - Nothing else. The bot only makes **outbound** connections (Discord, OpenAI,
    the AFC API), so no other inbound ports are needed.
- **(Recommended) Elastic IP:** allocate one and associate it with the instance.
  Without it, the public IP changes every stop/start and breaks the `EC2_HOST`
  secret. An Elastic IP is free while attached to a running instance.

## 2. Provision

**Option A — automatic (User data).** At launch, expand **Advanced details →
User data** and paste the entire contents of [`cloud-init.yaml`](cloud-init.yaml)
(including the `#cloud-config` first line). The box self-provisions on first boot.

**Option B — manual.** SSH in and run the shared provisioner (the repo is public,
so the raw URL works):

```bash
ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>
curl -fsSL https://raw.githubusercontent.com/AFRICANFREEFIRECOMMUNITY/AFC-Bot/main/deploy/oracle/setup.sh -o /tmp/setup.sh
sudo bash /tmp/setup.sh
```

Either way it installs Python 3.11 + `ffmpeg`/`libopus`/`libnacl`, clones the repo
to `/home/ubuntu/AFC-Bot`, creates a venv, installs `requirements.txt`, and writes
+ enables the hardened `afc-bot` systemd unit.

## 3. Fill secrets and start

```bash
nano /home/ubuntu/AFC-Bot/.env        # set DISCORD_TOKEN and OPENAI_API_KEY
sudo systemctl restart afc-bot
sudo systemctl status afc-bot --no-pager
sudo journalctl -u afc-bot -f         # confirm on_ready fired; watch for ⚠️ loop errors
```

Also enable the **Message Content Intent** in the Discord Developer Portal
(Bot → Privileged Gateway Intents), or the bot receives empty message bodies.

---

## 4. Auto-deploy on push (GitHub Actions over SSH)

[`.github/workflows/deploy-aws.yml`](../../.github/workflows/deploy-aws.yml) runs on
every push to `main` (except knowledge-only commits) and SSHes into the box to run
`setup.sh --update`, which does `git fetch` + `git reset --hard origin/main` +
reinstall + `systemctl restart afc-bot`.

> **Why `reset --hard`, not `pull`:** the running bot leaves the working tree dirty
> (it rewrites `knowledge_base.txt`), so a plain `git pull` fails. `reset --hard`
> makes git the source of truth. Runtime state (`conversation_history.json`,
> `seen_*.json`) is gitignored and **untracked**, so `reset --hard` leaves it in
> place — dedup/seen-state survives the deploy and the bot does not re-spam old
> announcements.

### One-time setup

**a. Create a dedicated deploy key** (on your local machine — do not reuse your
personal `.pem`):

```bash
ssh-keygen -t ed25519 -f afc-deploy-key -N "" -C "github-actions-deploy"
```

This makes `afc-deploy-key` (private) and `afc-deploy-key.pub` (public).

**b. Authorize the public key on the box:**

```bash
# print the public key, copy it...
cat afc-deploy-key.pub
# ...then on the EC2 box, append it:
echo "<paste afc-deploy-key.pub here>" >> /home/ubuntu/.ssh/authorized_keys
```

**c. Add three repository secrets** (GitHub repo → Settings → Secrets and
variables → Actions → New repository secret):

| Secret | Value |
|---|---|
| `EC2_HOST` | the instance's Elastic IP (or public DNS) |
| `EC2_USER` | `ubuntu` |
| `EC2_SSH_KEY` | the full contents of the **private** key file `afc-deploy-key` (include the `-----BEGIN/END-----` lines) |

**d. Confirm passwordless sudo.** The workflow runs `sudo bash …setup.sh`. On the
default AWS Ubuntu AMI the `ubuntu` user already has NOPASSWD sudo (via
`/etc/sudoers.d/90-cloud-init-users`), so this works out of the box.

Push to `main` → watch the run under the repo's **Actions** tab.

### <a name="ssh-exposure-tradeoff"></a>Security note — SSH exposure

GitHub-hosted runners connect from a large, rotating set of public IPs, so the box's
port 22 must be reachable by them. Pick your comfort level:

- **Quick (common for a small bot):** allow `22` from `0.0.0.0/0` but enforce
  **key-only** auth on the box — set `PasswordAuthentication no` in
  `/etc/ssh/sshd_config`, `sudo systemctl restart ssh`. The ed25519 deploy key is
  the only way in. Optionally add `fail2ban`.
- **Tighter:** restrict `22` to GitHub Actions' published egress ranges. Fetch the
  current list and add them to the security group (they change ~monthly, so
  re-check periodically):
  ```bash
  curl -s https://api.github.com/meta | jq -r '.actions[]'
  ```
- **Most secure (no open SSH at all):** switch the trigger to **AWS SSM Run
  Command** with GitHub OIDC — no inbound SSH, no stored private key. More one-time
  AWS setup (IAM OIDC provider + role + instance profile). Ask and this can be
  swapped in.

---

## Operating notes

- **Run exactly one instance.** Two copies double-post every announcement —
  `seen_*.json` dedup is per-process, not shared. Shut Railway down once EC2 is
  confirmed healthy.
- **No data migration.** State regenerates; on first boot the seen-sets seed from
  the current snapshot, so the bot won't re-announce existing events/news/bans.
  Conversation history just starts fresh (24h TTL anyway).
- **Knowledge stays current automatically:** `auto_scrape_loop` rewrites
  `knowledge_base.txt` every 6h on the box, and the `update_knowledge.yml` Action
  keeps the repo copy fresh; `--update` pulls the latter.
- **Cost:** free on `t3.micro` for 12 months; ~$5–8/mo after. For fixed-price
  simplicity, **Lightsail** ($5/mo, 1 GB) runs the same Ubuntu + same `setup.sh`.
- **Private repo later?** The clone uses an unauthenticated HTTPS URL (works because
  the repo is public). If you make it private, add a PAT or deploy key on the box
  for the clone/fetch.

## Troubleshooting

| Symptom | Check |
|---|---|
| Actions deploy fails at SSH | `EC2_HOST` matches the current IP (use an Elastic IP); port 22 reachable by runners; public deploy key is in `~/.ssh/authorized_keys` |
| `Permission denied (publickey)` | `EC2_SSH_KEY` is the **private** key, complete with header/footer lines; `EC2_USER` is `ubuntu` |
| `sudo: a password is required` | NOPASSWD sudo missing for `ubuntu` — add `ubuntu ALL=(ALL) NOPASSWD:ALL` via `sudo visudo -f /etc/sudoers.d/afc-deploy` |
| Bot not replying after deploy | `sudo journalctl -u afc-bot -n 100 --no-pager`; confirm `.env` has both secrets and Message Content Intent is on |
