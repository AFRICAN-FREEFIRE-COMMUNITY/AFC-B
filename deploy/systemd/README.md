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
