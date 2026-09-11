# Owner runbook: the steps only the account holder can do (written 2026-09-11)

Everything an agent could do on the VPS has been done and tested. These four things need a
login to a dashboard that belongs to you, so they are click-lists. Each ends with the same
restart on the box:

    ssh afcvps
    nano ~/AFC-B/.env            # or ~/AFC-B/afcbot/.env for the bot's own keys
    sudo systemctl restart django_app celery-worker celery-beat celery-rankings afc-bot

Nothing else on the box needs touching; the values are read from `.env` at start.

---

## 1. Rotate the third-party keys

Why: the migration runbook (trap 8) flagged that the old AWS box had its database port open to
the world and a Gemini key once sat in git history; the Django secret and the DB password were
rotated on 2026-09-11, the rest live in your provider dashboards. Do them in any order, one at a
time, and restart after each so a typo is caught immediately (the site keeps working on the old
value until the restart).

| `.env` line | Where to get a new one | Also affects |
|---|---|---|
| `DISCORD_CLIENT_SECRET` | discord.com/developers → your AFC application → **OAuth2** → Reset Secret | Discord sign-in and the connect-Discord flow. Redirect URLs stay as they are |
| `DISCORD_TOKEN` (Django) | same portal → **Bot** → Reset Token | role assignment. If the same bot user is the chat bot, put the same value in `afcbot/.env` `DISCORD_TOKEN` |
| `DISCORD_TOKEN` (`afcbot/.env`) | the chat bot's application → Bot → Reset Token | the Discord bot goes offline until restarted with the new token |
| `PAYSTACK_SECRET_KEY`, `PAYSTACK_PUBLIC_KEY` | dashboard.paystack.com → Settings → **API Keys & Webhooks** → Generate new keys | shop checkout. Webhook URL unchanged |
| `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY` | dashboard.stripe.com → Developers → **API keys** → Roll key (keep the old one valid for an hour while you swap) | shop + event payments |
| `STRIPE_WEBHOOK_SECRET` | Developers → **Webhooks** → each endpoint (`/shop/stripe-webhook/`, `/events/stripe-webhook/`) → Roll secret | both endpoints read the same env line; roll both to the same value or split the env line first |
| `GEMINI_API_KEY` | aistudio.google.com → Get API key → create, then delete the old | OCR result reading |
| `OPENAI_API_KEY` (both env files) | platform.openai.com → API keys | bot replies (and any Django use) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | console.cloud.google.com → APIs & Services → Credentials → the OAuth client → Reset secret | Google sign-in. The client ID does not change |
| `DEEPL_API_KEY`, `KAPSO_API_KEY`, `MINTROUTE_*`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET` | each provider's own console | translation, WhatsApp, diamond shop |
| `EMAIL_PASSWORD` | Microsoft 365 admin → the info@ mailbox → reset password (or an app password) | every email the site sends; test with a password reset to yourself |

Leave `AFC_OIDC_RSA_PRIVATE_KEY` alone: partners (v-ent.co) verify tokens against its public half,
so rotating it is a coordinated change with them, not a dashboard click.

Also part of rotation, because of the AWS exit: the backend box had an `~/.aws` credentials
folder (now in the archive on the VPS). In the AWS console of account 211125329565: IAM → Users
→ each user → Security credentials → **deactivate** those access keys.

## 2. Site email: leave M365, or switch

Today the site sends through `smtp.office365.com:587` as `info@africanfreefirecommunity.com`.
Microsoft caps that at **30 messages a minute** and **1,000 a day to people you have never
mailed**; a registration rush or a broadcast hits that and password resets stop arriving.

Since 2026-09-11 the provider is `.env`, not code. To switch, sign up with a transactional
provider (the runbook compared Amazon SES, Brevo, Postmark, Resend; Resend's free tier covers
3,000 a month), verify the domain there (they give you DNS records to add in Cloudflare, grey
cloud), then:

    EMAIL_HOST = smtp.<provider>
    EMAIL_PORT = 587
    EMAIL_HOST_USER = <the SMTP username they give you>
    EMAIL_PASSWORD = <the SMTP password they give you>
    EMAIL_FROM = info@africanfreefirecommunity.com

restart, and test with a password reset to your own address. Until you do this, nothing changes.

## 3. Offsite backups: one bucket

The box keeps 14 nightly dumps in `/var/backups/afc` and a restore is tested monthly
(`afc-restore-test.sh`, first run 2026-09-11: RESTORE_TEST_OK). That survives a bad migration,
not a dead server. For that, `afc-offsite-backup.sh` already runs nightly at 03:45 and does
nothing until it finds a bucket:

1. Create a bucket at Backblaze B2 (runbook pick, about $1 a month for this size) or any
   S3-compatible store. Note the bucket name and the key pair it gives you.
2. On the box, as root: `rclone config` → new remote, name **offsite**, type `b2` (or `s3`),
   paste the key pair. `rclone lsd offsite:` must list the bucket.
3. `echo 'BUCKET=<bucket name>' | sudo tee /etc/afc-offsite.conf`
4. `sudo /usr/local/sbin/afc-offsite-backup.sh` once by hand; the first media sync is 2.3 GB.

From then on: every dump plus media, off the box, nightly.

## 4. Retire AWS

The frontend/bot account (835857361469) is scheduled for **suspension on 12 September 2026** for
non-payment. Everything on its two boxes is copied: the bot's checkout, env and state
(`~/aws-archive/afc-bot-home-2026-09-11.tar.gz` on the VPS), the frontend box held nothing
but the maintenance-page installer (already in this repo) and the container that Docker Hub
still has. No S3 buckets, no snapshots, no elastic IPs in that account. **You can let it be
suspended**; nothing is lost. If you would rather close it cleanly: pay the balance, then
terminate `afc-frontend` and `afc-bot` and close the account.

The backend account (211125329565) was inventoried on 2026-09-11 evening. Besides `afc-test-1`
it holds four things the site does not use, all still billing:

| Resource | What it is | Do |
|---|---|---|
| RDS `database-1` + `database-2` (us-east-1, db.t4g.micro, 20 GiB each, created 2025-10-19) | the pre-June database instances; 0 connections, unreachable even from the live box | RDS → each → Actions → Take snapshot (optional, a few dollars a month) → Delete |
| Elastic Beanstalk environment `afc-env` (eu-west-2 London, t3.micro 13.41.70.82) | created Oct 2025, never deployed to: `/var/app/current` empty, nothing listening, 46 weeks up | Elastic Beanstalk → Environments → `afc-env` → Actions → Terminate environment |
| S3 `elasticbeanstalk-eu-west-2-211125329565` | Beanstalk bundles + `backup.sql` of 2026-06-08 (already on the box and in `aws-archive`) | Empty, then Delete, after the environment is gone |
| EBS snapshot `before-resize` (8 GiB, 2025-11-06) | pre-resize copy of the box's original disk | Delete after `afc-test-1` is retired |

`afc-test-1` itself is fully archived to the VPS
(`~/aws-archive/afc-backend-home-2026-09-11.tar.gz`, 2.5 GB incl. a second media copy, plus the
final dump and the nginx/systemd/mysql configs). The rule from the migration runbook:

1. Keep it running until the site has survived one tournament night on the VPS and not before
   2026-09-25. Rollback is documented in `cloudflare-dns-after-2026-09-11.txt`.
2. Then: EC2 → Instances → `afc-test-1` → Instance state → **Stop** (compute billing stops;
   the disk stays). Wait a week. Nothing broke? **Terminate**.
3. IAM: deactivate the access keys (section 1). Delete the security group and the key pair.
4. Close the account, or leave it empty.

Copy the two archives somewhere that is not the VPS before terminating (the offsite bucket from
section 3 is the natural place: `rclone copy ~/aws-archive offsite:<bucket>/aws-archive`).
