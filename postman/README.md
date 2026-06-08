# AFC Backend — Postman Collection

A Postman collection covering **every** backend route, generated from the live Django URL
resolver — so it can never drift out of sync with the code by hand.

## Add new APIs / regenerate (do this whenever you add or change an endpoint)

```bash
# from backend/  (uses the project venv)
.venv/Scripts/python.exe postman/generate.py
```

`postman/generate.py` walks `get_resolver()`, introspects each DRF view's allowed HTTP
methods, fills sample path params, sets the auth header (Bearer `{{token}}` by default,
`X-API-Key {{api_key}}` for the partner API, none for public routes), and rewrites both
collection files below. New endpoints appear automatically — no hand-editing the JSON.
Then re-run the newman smoke (below) to catch any new 500s. Commit the regenerated `*.json`.

> Write-request bodies are emitted as an empty `{}` placeholder — fill the real payload
> before firing a POST/PUT/PATCH. The GET smoke needs no body.

## Files

| File | Commit? | What |
|---|---|---|
| `AFC_Backend_API.postman_collection.json` | ✅ | Full collection — all endpoints, grouped into a folder per Django app. Uses `{{base_url}}` / `{{token}}` / `{{api_key}}` variables (no secrets baked in). |
| `AFC_Backend_API.postman_environment.template.json` | ✅ | Environment template — copy it, paste your own token/key. |
| `AFC_Smoke_GET.postman_collection.json` | ✅ | The read-only (`GET`, non-destructive) subset (149 requests), each with a `status < 500` reachability test. Safe to run end-to-end. |
| `.local.env.json` | ❌ gitignored | A real environment with a live token + key for local runs. Never committed. |

## The team DEV collection (`WEBSITE/AFC.postman_collection.json`) + folder conventions

There are **two** collections, and they are NOT the same file:

| Collection | Path | Foldering |
|---|---|---|
| **AUTO** (source of truth for "what exists") | `backend/postman/AFC_Backend_API.postman_collection.json` | One flat folder **per Django app** — generated, never hand-edited. |
| **DEV** (the one the team imports + drives) | `WEBSITE/AFC.postman_collection.json` (repo root, **not** committed to either git repo) | **Hand-curated by audience/area**, see the rule below. |

Keep DEV current after a code change:

```bash
# from backend/
.venv/Scripts/python.exe postman/generate.py        # 1. regen AUTO from the resolver
.venv/Scripts/python.exe postman/merge_into_dev.py   # 2. APPEND new endpoints into DEV
# 3. if you RETIRED an endpoint, also remove it from DEV by hand (merge only adds).
```

### 🗂️ DEV folder rule (so admin endpoints don't keep landing in the wrong place)

**Group by AUDIENCE, not just by app.** An app can have routes for several audiences, and
they must go to different DEV folders:

- **AFC-staff / platform-admin endpoints → under the top-level `Admin Ap's` folder**, in a
  sub-folder named for the area (`EVENT`, `LEADERBOARD`, `SHOP`, `RANKINGS`, `ORGANIZERS`, …).
  A route is "admin" if it is gated to AFC staff (role admin/moderator/support, the
  `event_admin`/`head_admin` userroles, or `is_platform_org_admin`) — e.g. anything under an
  `.../admin/...` path such as `organizers/admin/*`.
- **End-user / org-member / partner-facing endpoints → their OWN top-level folder**
  (`TEAM(user)`, `EVENTS (user)`, `PLAYER MARKET`, `ORGANIZERS (organizer + public)`,
  `PARTNER API`, …).
- **Public / unauthenticated reads** live with their user-facing folder.

Worked example (the organizers app, fixed 2026-06-08): `afc_organizers` serves three
audiences (`views_admin` = AFC staff, `views_organizer` = org members, `views_public`).
So its 28 routes split into **`Admin Ap's > ORGANIZERS`** (the 11 `organizers/admin/*`
provisioning + oversight + design/report triage routes) and **`ORGANIZERS (organizer +
public)`** (the 17 org-member + public routes). Do the same for any future multi-audience app.

> `merge_into_dev.py` appends a new endpoint next to existing endpoints with the same
> path-prefix, so once an app's admin routes live under `Admin Ap's`, future admin routes
> for that app land there automatically. When you add the FIRST admin route for a brand-new
> app, place it under `Admin Ap's` by hand so the convention holds.

## Auth model (three styles — set per request automatically)

The collection sets the right auth header on each request based on how that view reads credentials:

- **`Authorization: Bearer {{token}}`** — newer endpoints (partner, round-robin, auth) that split `Bearer `.
- **`Authorization: {{token}}`** — many older `afc_*` endpoints that pass the raw header value to `validate_token`.
- **`X-API-Key: {{api_key}}`** — the partner API (`/api/v1/partner/...`).
- no header — public endpoints (signup, login, public reads).

`{{token}}` is a `SessionToken` (get one by calling `POST /auth/login/`, or mint one in a Django
shell). `{{api_key}}` is a partner API key (issued from the admin Partners page).

## Import (Postman GUI)

1. Import `AFC_Backend_API.postman_collection.json`.
2. Import `AFC_Backend_API.postman_environment.template.json`, fill in `token` + `api_key`, select it.
3. Browse by app folder; each request carries an example body for writes.

## Run headless (newman)

```bash
# from backend/  (uses pnpm, never npm)
pnpm dlx newman run postman/AFC_Smoke_GET.postman_collection.json -e postman/.local.env.json \
  --timeout-request 20000 --reporters cli
```

The smoke collection only fires `GET`/non-destructive requests. The full collection contains the
write/delete endpoints too, but do **not** blindly run it against a real DB — those mutate/delete
data. Run individual write requests deliberately.

## Last run (GET smoke, 149 requests)

`200`×112 · `4xx`×27 (reachable — need params/auth, working as designed) · **`500`×10**.

The ten 500s are **pre-existing** endpoints (none from the partner / round-robin / scoring / tooltip
work — those all returned 2xx). Captured exceptions:

| Endpoint | Exception | Likely cause |
|---|---|---|
| `/auth/get-top-winner-player/` | FieldError | bad ORM field reference in the aggregate |
| `/events/get-total-events-count/` | TypeError | |
| `/events/get-most-popular-event-format/` | AttributeError | |
| `/team/view-join-requests/` | MultipleObjectsReturned | `Team.objects.get(...)` matches many rows — needs `.filter().first()` |
| `/team/get-team-with-highest-wins/` | FieldError | bad ORM field reference |
| `/shop/get-coupon-conversion-rate/` | TypeError | view reads a `slug` param the registered URL doesn't provide |
| `/shop/test-denom/` | JSONDecodeError | dev/debug endpoint — calls external Mintroute (no creds in dev) |
| `/shop/test-brands/` | JSONDecodeError | dev/debug endpoint — calls external Mintroute (no creds in dev) |
| `/player-market/view-applications/` | MultipleObjectsReturned | `.get(...)` matches many rows; also registered twice in `urls.py` |

`test-denom` / `test-brands` are dev-only external calls (expected to fail without Mintroute creds).
The rest are genuine code bugs worth fixing separately.
