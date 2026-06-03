# AFC Backend — Postman Collection

A Postman collection covering **every** backend route (382 endpoints / 396 requests across
12 apps), auto-generated from the live Django URL resolver — so it can never drift out of
sync with the code by hand.

## Files

| File | Commit? | What |
|---|---|---|
| `AFC_Backend_API.postman_collection.json` | ✅ | Full collection — all endpoints, grouped into a folder per Django app. Uses `{{base_url}}` / `{{token}}` / `{{api_key}}` variables (no secrets baked in). |
| `AFC_Backend_API.postman_environment.template.json` | ✅ | Environment template — copy it, paste your own token/key. |
| `AFC_Smoke_GET.postman_collection.json` | ✅ | The read-only (`GET`, non-destructive) subset (149 requests), each with a `status < 500` reachability test. Safe to run end-to-end. |
| `.local.env.json` | ❌ gitignored | A real environment with a live token + key for local runs. Never committed. |

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
