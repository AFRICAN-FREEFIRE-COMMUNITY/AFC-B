# How a merge becomes a deploy (push-to-deploy, 2026-09-11)

Owner's ask, 2026-09-11: "once you push to git, it automatically updates, also users should never
know that an update is ongoing, they should still be able to use the site while a deploy is
ongoing and then the updates just show up on the next refresh."

That is what this folder does now. Nothing here needs a human on the server.

## The one-line version

- Merge to **AFC-B `main`** -> `.github/workflows/deploy-backend.yml` -> ssh into the VPS -> new
  code live in about 40 s, gunicorn swaps workers one by one, no request dropped.
- Merge to **AFC_Frontend `master`** -> `.github/workflows/docker-image-live.yml` -> image built
  and pushed to Docker Hub (~3 min) -> ssh into the VPS -> new container started beside the old
  one, health-checked, nginx pointed at it, old one retired. No maintenance page, no gap.

A red run means the site is still on the previous code. A green run means users have the new
one. There is no third state.

## The pieces, and why each exists

| Piece | Lives | Job |
|---|---|---|
| deploy key (one per repo) | `~/.ssh/authorized_keys` on the VPS, private half in the repo's GitHub secret `VPS_SSH_KEY` | lets Actions ssh in. **Forced command**: the key can run `run-deploy.sh` and nothing else. No shell, no port forwarding, no pty. A leaked key can redeploy a branch that already exists; it cannot read `.env` or open a shell |
| `run-deploy.sh` | `/home/ubuntu/deploy-vps/run-deploy.sh` (outside the repo; re-installed from the repo on every backend deploy) | the trampoline. Parses `backend <ref>` or `frontend <tag>` from the ssh command, refuses anything else, takes a lock so deploys never overlap, fetches + checks out the branch (backend), then hands over to the script below |
| `deploy-backend.sh` | this folder | pip from `requirements-prod.txt`, `makemigrations` + `migrate`, `manage.py check`, **`systemctl reload django_app`** (graceful), restart celery + bot, health probe |
| `deploy-frontend.sh` | this folder | blue/green swap between ports 3000 and 3001 behind the nginx upstream include |
| `/etc/nginx/afc-frontend-upstream.conf` | VPS | one line, `upstream afc_frontend { server 127.0.0.1:PORT; }`. The only file a frontend deploy edits |
| `probe-during.sh` | this folder | fires 10 requests/s through nginx during a reload or swap and counts failures. This is how "zero downtime" was proven rather than claimed |
| GitHub secrets `VPS_HOST`, `VPS_SSH_KEY` | both repos | the box and the key. The VPS host key is pinned in the workflow file itself, so a DNS or MITM trick cannot redirect a deploy |

## What "zero downtime" means here, measured

Gates P5 and P6 in `WEBSITE/GATES-push-to-deploy.md` hold the numbers from 2026-09-11: a request
loop at 10/s through nginx across a gunicorn reload and across a full container swap saw zero
non-expected responses. Repeat any time with `bash deploy/vps/probe-during.sh backend|frontend`.

What a user actually experiences: an open tab keeps working through the deploy, and keeps
working after it. Every build's `/_next/static` files are kept on the host for 30 days
(`/var/www/afc-next`, served by nginx ahead of the container), so a tab still on the previous
build can open a lazy-loaded modal or tab and find its chunks; nothing forces a reload. On the
next navigation the tab gets the new build. `deploy/vps/probe-old-chunk.sh` proves it: it swaps
builds and fetches the retired build's manifest, which must answer 200 with `X-AFC-Static: disk`.

Two measured traps, both closed on 2026-09-11:
- nginx reload is graceful, so OLD workers keep serving browsers on keep-alive connections with
  the OLD upstream. Retiring the old container straight after the reload refused real requests
  for a few seconds (seen in the error log at 16:14:19). `deploy-frontend.sh` now waits until no
  worker older than the reload remains (`worker_shutdown_timeout 30s` in nginx.conf caps it),
  then retires the port. `probe-during.sh` runs a keep-alive probe (one curl, 400 URLs, like a
  browser) beside the fresh-connection one: 400/400 after the fix.
- `curl -o /dev/null` with many URLs only silences the FIRST body; every URL needs its own `-o`.

## Migrations

This repo does not commit migration files; they are generated on the server (team convention,
2026-06-08). `deploy-backend.sh` therefore runs `makemigrations --noinput` then `migrate --noinput`
on the box, which is exactly what a human did by hand before. A model change that needs a human
decision (a new non-null field without a default) makes `makemigrations` exit non-zero, the run
goes red **before** anything is reloaded, and the site stays on the old code. Fix: give the field
a default (or `null=True`), push again.

## Rollback

- **Frontend:** every build is also tagged `sha-<commit>`. On the box:
  `bash ~/AFC-B/deploy/vps/deploy-frontend.sh sha-<previous commit>`. Same swap, other direction,
  about 30 s. Or re-run the previous green workflow from the Actions tab.
- **Backend:** `git revert` the merge on `main` and push; the workflow deploys the revert. A
  migration that must be undone is a manual `migrate <app> <previous>` first, as always.

## Failure modes you will actually meet

| You see | It means | Do |
|---|---|---|
| run red at "Deploy": `refused: origin/<ref> does not exist` | branch not pushed / typo in a manual dispatch | push, re-run |
| run red at "Deploy": `makemigrations` traceback about a default | model change needs a decision | add default, push |
| run red at "Deploy": `API health probe failed` + gunicorn log | new code crashes on import or startup; old workers were retired by reload only after new ones came up, so if this fires the site may be degraded: check `systemctl status django_app` | fix or revert, push |
| run red at "Swap in": `new container never answered 200` | frontend image starts but does not serve (bad env, crash) | old container still live; read the container log in the run output |
| run red: `another deploy still holds the lock after 10 minutes` | a stuck earlier deploy | on the box: `ps -ef | grep deploy-`, kill it, `rm /home/ubuntu/deploy-vps/.deploy.lock`, re-run |
| run red at "Prepare ssh" / `Host key verification failed` | the VPS was rebuilt and has a new host key | update the pinned key line in BOTH workflow files |

## Deploying by hand, if GitHub is down

    ssh afcvps
    cd ~/AFC-B && git pull && bash deploy/vps/deploy-backend.sh
    bash deploy/vps/deploy-frontend.sh latest      # or a sha-<commit> tag

## What deliberately is NOT here

No staging environment, no approval step, no Slack/Discord notification, no automatic rollback.
Each is a small addition on top of this; none was asked for on 2026-09-11.
