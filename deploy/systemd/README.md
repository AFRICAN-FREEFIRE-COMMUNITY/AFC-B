# Celery under systemd

Unit files for the production API host (`ubuntu@ip-172-31-19-83`, `/home/ubuntu/AFC-B`).

## Why these exist

Celery was started by hand inside `screen` on 2026-06-12 and never restarted. On 2026-08-05
that produced two separate, live faults:

1. **Two `celery -A afc beat` processes were running at once.** Beat does not coordinate with
   other beats, so each independently enqueued the entire schedule. Every periodic job had been
   firing **twice** for eight weeks: the five-minute event-status sweep, the per-minute
   scheduled-news publish, the nightly payout pass. Duplicate emails and notifications are the
   visible symptom.
2. **The workers were executing 2026-06-12 code.** `django_app` was restarted on every deploy;
   Celery never was. So roughly two months of task fixes had shipped without ever taking
   effect, including the WhatsApp dispatch fix where a queued send *counted* as sent while
   nothing actually left the building.

`screen` caused both: it dies with its terminal, does not return after a reboot, and nothing
stops a second copy being started. systemd restarts on failure, starts on boot, refuses to run
a second instance of a unit, and makes `systemctl restart celery-worker` a normal deploy step
the way `django_app` already is.

## Install

Stop anything still running under `screen` FIRST, or you will have duplicates again:

```bash
pkill -f 'celery -A afc'
sleep 3
ps aux | grep 'celery -A afc' | grep -v grep     # must print nothing
```

Then:

```bash
sudo cp deploy/systemd/celery-worker.service /etc/systemd/system/
sudo cp deploy/systemd/celery-beat.service   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now celery-worker celery-beat
sudo systemctl status celery-worker celery-beat --no-pager
```

`celery-ocr-ml.service` is **optional and deliberately not enabled** - read its header first,
it costs Gemini API calls and has a queue backlog to purge before its first start.

## Deploy step

Add to every backend deploy, after `migrate`:

```bash
sudo systemctl restart django_app celery-worker celery-beat
```

Restarting Celery is not optional. Skipping it is exactly how the workers ended up two months
stale while everything looked deployed.

## Notes

* **No `EnvironmentFile`.** `afc/settings.py` calls `load_dotenv(BASE_DIR/".env")` itself, so
  the process reads `.env` without systemd. Pointing `EnvironmentFile` at that same `.env` is
  the usual reason these units fail to start, because systemd's parser rejects the quoting and
  `export` lines a shell-oriented `.env` may contain.
* **`After=` not `Requires=` on redis.** If the redis unit is named differently on this host,
  `Requires=` would make the service fail outright; `After=` only orders startup, and
  `Restart=always` covers a broker that is slow to come up.
* **Logs:** `journalctl -u celery-worker -f` (or `-u celery-beat`).

## The queues (added 2026-08-05, after the units above were first written)

A worker started as plain `celery -A afc worker` consumes **only the default queue**. This
codebase routes work onto four dedicated queues via `@shared_task(queue=...)`, and none of
them had a consumer. The inline fallback that would have covered it is gated on `*_SYNC`
settings that default to `DEBUG` and are **not set in the production `.env`**, so those tasks
were queued and never ran, silently.

Measured on production, 2026-08-05:

| queue | backlog | consumer now |
|---|---|---|
| `celery` (default) | 0 | `celery-worker` |
| `whatsapp` | 170 | `celery-worker` |
| `sso_webhooks` | 0 | `celery-worker` |
| `rankings_recalc` | 346,520 | `celery-rankings` |
| `ocr_ml` | 126 | `celery-ocr-ml` (enabled 2026-08-05 on the owner's call) |

The `whatsapp` figure is the one that mattered most: every WhatsApp message the platform
believed it had handed off was sitting in Redis unread.

### Purge before first start

Do NOT point a worker at these backlogs and let it rip.

* **`rankings_recalc`** - the tasks are idempotent and per-entity, so the same team appears
  hundreds of times and only the last pass matters. Draining it would hammer MySQL for a very
  long time to reach a state one bulk command reaches in minutes:

      redis-cli -n 0 del rankings_recalc
      python manage.py recalc_rankings --all-months

* **`whatsapp`** - draining it fires every queued message at once: room details for events
  that already finished, order updates days late. Worse, a sudden burst of template messages
  from a recently-approved business number is what gets a number quality-flagged or restricted
  by Meta, which is hard to undo. Inspect what is in there first (prints task contexts and
  counts, no phone numbers):

      redis-cli -n 0 lrange whatsapp 0 -1 | grep -o '"context": *"[^"]*"' | sort | uniq -c | sort -rn

  then `redis-cli -n 0 del whatsapp` unless something in that list is worth delivering late.

* **`ocr_ml`** - stale nightly autolabel jobs. `redis-cli -n 0 del ocr_ml` before enabling
  that unit, or the first start runs every night's job that was ever queued, back to back,
  each one spending Gemini calls.

### Install

    sudo cp deploy/systemd/celery-worker.service   /etc/systemd/system/
    sudo cp deploy/systemd/celery-beat.service     /etc/systemd/system/
    sudo cp deploy/systemd/celery-rankings.service /etc/systemd/system/
    sudo cp deploy/systemd/celery-ocr-ml.service   /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now celery-worker celery-beat celery-rankings celery-ocr-ml

### Deploy step (updated)

    sudo systemctl restart django_app celery-worker celery-beat celery-rankings celery-ocr-ml

### OCR learning loop cost, for the record

Enabled 2026-08-05 on the owner's instruction. It is bounded, which is why it was a small
decision rather than a big one:

* `autolabel_backlog` runs nightly at 02:30 and processes at most `OCR_AUTOLABEL_CAP` images
  (default **50**), at two Gemini reads each - a ceiling of **100 calls per night**. Raise or
  lower it with `OCR_AUTOLABEL_CAP` in the `.env`.
* `check_retrain_trigger` runs Mondays 03:00 and costs nothing: it does NOT train on the box,
  it only writes a `retrain_requested` marker when enough admin-confirmed pairs have built up.
* Both no-op safely if `GEMINI_API_KEY` is unset, so a missing key degrades rather than errors.

To watch a run without waiting for 02:30, execute it inline with a small cap:

    python manage.py shell -c "from afc_ocr.tasks import autolabel_backlog; print(autolabel_backlog(cap=3))"
