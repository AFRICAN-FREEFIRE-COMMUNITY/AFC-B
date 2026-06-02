# Production DB 500 Fix — `(1698, "Access denied for user 'root'@'localhost'")`

Runbook for the intermittent production 500s. The **code** change is already merged
(`afc/settings.py`, commit `91a2b96f`); production stays broken until the **environment**
is set per the steps below. This document is the operational half of that fix.

---

## 1. What was happening

The API intermittently returned `500` with:

```
django.db.utils.OperationalError: (1698, "Access denied for user 'root'@'localhost'")
```

Two separate problems stacked on top of each other:

1. **The app authenticated to MySQL as `root@localhost`.** That was the *hardcoded
   default* in `settings.py`. On a Linux MySQL install `root@localhost` is normally bound to
   the `auth_socket` plugin — it only lets in a process whose **OS user is `root`**. The
   Django app does **not** run as the OS `root` user (on Elastic Beanstalk it runs as
   `webapp`), so every password/TCP login attempt as `root` is rejected with error `1698`.
   It looked "intermittent" because Django opens a fresh DB connection per request/worker —
   any request that hit a worker without a live connection tripped it, cached/keep-alive
   ones did not.

2. **`DEBUG = True` in production.** That is why the raw `OperationalError`, the SQL, and
   settings were visible in the HTTP response at all. A production server must never leak
   tracebacks to clients (security best-practice 21).

## 2. What the code change did

`afc/settings.py` no longer hardcodes either value — both are env-driven, with the **local
dev** values as the fallback defaults so local keeps working with no env set:

```python
DEBUG = os.getenv("DEBUG", "True").strip().lower() == "true"

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME':     os.getenv('DB_NAME', 'afc_db'),
        'USER':     os.getenv('DB_USER', 'root'),
        'PASSWORD': os.getenv('DB_PASSWORD', 'Purewater@12345'),
        'HOST':     os.getenv('DB_HOST', 'localhost'),
        'PORT':     os.getenv('DB_PORT', '3306'),
    }
}
```

So **production now just has to set the environment** to a dedicated app user and
`DEBUG=False`. The defaults are dev-only and must never be what prod runs on.

---

## 3. Create a dedicated MySQL app user (run once, on the DB host)

Do **not** keep using `root`. Make a least-privilege user scoped to the one schema.
Connect to the production MySQL as an admin and run (replace the password with a strong
generated secret — this is an example placeholder, do not ship it verbatim):

```sql
-- '%' lets the app connect over TCP from the EB instance. If the DB lives on the SAME
-- host as the app, you may instead scope to '127.0.0.1' (TCP) for tighter least-privilege.
-- Avoid 'localhost' for the app user — on MySQL 'localhost' means the unix socket, which
-- is exactly the auth path that broke with root.
CREATE USER 'afc_app'@'%' IDENTIFIED BY 'REPLACE_WITH_STRONG_SECRET';

-- Scope to the single application schema. ALL is needed because `manage.py migrate`
-- runs DDL; it is still bounded to this one database (best-practice 15).
GRANT ALL PRIVILEGES ON afc_db.* TO 'afc_app'@'%';

FLUSH PRIVILEGES;
```

Verify the new user can log in over TCP (this is the exact path that failed for `root`):

```bash
mysql -h 127.0.0.1 -P 3306 -u afc_app -p afc_db -e "SELECT 1;"
```

A clean `1` means the auth path the app uses now works.

> If production MySQL is **Amazon RDS** (the commented RDS host in `settings.py` suggests it
> once was), there is no socket/`auth_socket` issue — but the same rule holds: use a
> dedicated, scoped user and point `DB_HOST` at the RDS endpoint, never `localhost`.

---

## 4. Set the Elastic Beanstalk environment properties

These are **secrets** — set them as EB environment properties, **never** commit them to a
tracked `.ebextensions/*.config` file (best-practice 16). Two ways:

**EB Console:** Environment → Configuration → **Software** → *Edit* → **Environment
properties**, add each key/value, **Apply**. This restarts the app with the new env.

**EB CLI** (from the backend dir, with the env selected):

```bash
eb setenv \
  DB_NAME=afc_db \
  DB_USER=afc_app \
  DB_PASSWORD='REPLACE_WITH_STRONG_SECRET' \
  DB_HOST=127.0.0.1 \
  DB_PORT=3306 \
  DEBUG=False
```

- `DB_HOST` = `127.0.0.1` if MySQL is on the EB instance (forces TCP, avoids the socket);
  the **RDS endpoint** if it is managed.
- **`DEBUG=False` is mandatory** and must be identical on **every** instance — a single
  instance left on `True` keeps leaking tracebacks.

### One catch with `DEBUG=False`: `ALLOWED_HOSTS`

With `DEBUG=False`, Django enforces `ALLOWED_HOSTS`. It currently includes `"*"`
(`settings.py:37`), so requests will not be blocked — but `"*"` is itself a security smell
(see §6). If you tighten it, make sure the real API host
(`api.africanfreefirecommunity.com`) and the EB internal health-check host are present, or
health checks start returning `400`.

---

## 5. Verify the fix

1. Redeploy / restart finished, then hit an endpoint that always touches the DB, e.g.
   `GET https://api.africanfreefirecommunity.com/events/get-all-events/`.
2. Repeat ~10× (the bug was per-connection/intermittent) — every response should be `200`,
   zero `1698`.
3. Force an error (hit a deliberately bad route) and confirm the response is a **plain**
   `500`/`404` with **no traceback, SQL, or settings** in the body — proves `DEBUG=False`
   took effect.
4. Tail the app logs (`eb logs`) and confirm no `OperationalError 1698` after the cutover.

Only after all four pass is the incident closed.

---

## 6. Related security debt found in `settings.py` (separate task — not changed here)

These were noticed while fixing the DB issue. They are **not** part of this fix and were
left untouched on purpose (surgical-change rule), but they are real and should be scheduled:

- **`SECRET_KEY` hardcoded** (`settings.py:30`) — move to an env var; rotating it is wise
  since it has been committed to git history.
- **Third-party API keys committed in source**: `OPENAI_API_KEY` (`:43`), `MINTROUTE_*`
  (`:44–46`), and the `GEMINI_API_KEY` default (`:73`). These are live secrets in a tracked
  file — they should be moved to env/secret store **and rotated**, because anything in git
  history is considered exposed.
- **`ALLOWED_HOSTS` contains `"*"`** (`:37`) — acceptable as a stopgap, but should be an
  explicit allow-list of the real hosts.

Flagging only — do not bundle these into the DB hotfix.
