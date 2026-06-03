# AFC Partner Data API

A read-only, versioned REST API that gives AFC-approved partners access to
**completed, published** tournament data — events, stages, matches, standings,
teams, and players. Every partner is scoped to a specific set of events (or whole
organizations / all native AFC events) and sees only the resources and fields AFC
has explicitly turned on for them.

This document is the integration reference AFC hands to a partner.

---

## 1. Base URL

```
https://api.africanfreefirecommunity.com/api/v1/partner/
```

The version (`v1`) is baked into the path. A future breaking change ships as
`/api/v2/partner/` and never disturbs your existing integration.

All endpoints are **`GET` only** (the API is strictly read-only). Any other verb
returns `405 Method Not Allowed`.

---

## 2. Authentication — `X-API-Key`

Every request must carry your API key in the **`X-API-Key`** request header:

```
X-API-Key: afcp_3f9a_2b1c…  (the full key AFC issued you)
```

A key looks like `afcp_<prefix>_<secret>`. AFC stores only a hash of your key —
the plaintext is shown to the AFC admin **exactly once** at issue time, so keep it
somewhere safe; it cannot be recovered, only revoked and re-issued.

If the header is missing, malformed, unknown, revoked, expired, or your partner
account is suspended, the API responds **`401 Unauthorized`**:

```http
GET /api/v1/partner/events/
(no X-API-Key header)

HTTP/1.1 401 Unauthorized
Content-Type: application/json

{ "error": "Missing or malformed X-API-Key." }
```

Treat the key as a secret credential: send it only over HTTPS, never embed it in
client-side code or a public repository.

---

## 3. Endpoints

There are **seven** endpoints. Events are always addressed by their human-readable
**`slug`** (never a numeric id — the API never exposes internal database ids).

| # | Method & path | Resource toggle required | Returns |
|---|---|---|---|
| 1 | `GET /events/` | `can_read_events` | Paginated list of events you may read |
| 2 | `GET /events/{slug}/` | `can_read_events` | One event's public card |
| 3 | `GET /events/{slug}/stages/` | `can_read_stages` | Paginated stages (each with its groups nested) |
| 4 | `GET /events/{slug}/matches/` | `can_read_matches` | Paginated matches of the event |
| 5 | `GET /events/{slug}/standings/` | `can_read_standings` | Final ranked standings of the event |
| 6 | `GET /events/{slug}/teams/` | `can_read_teams` | Tournament teams with event-wide aggregated stats |
| 7 | `GET /events/{slug}/players/` | `can_read_players` | Players who recorded stats, with per-event stats |

Each endpoint is independently gated. If the matching resource toggle is **off**
for your partner account, that endpoint returns `403` (see §6) — the rest keep
working.

> **Field availability is also toggled.** Even on an endpoint you can read, stat and
> detail fields (kills, damage, placements, prize, rosters, etc.) appear **only** when
> AFC has enabled the corresponding field toggle for you. See §5.

### 3.1 `GET /events/` — list events

```http
GET /api/v1/partner/events/?limit=25&offset=0
X-API-Key: afcp_3f9a_…
```

```json
{
  "results": [
    {
      "slug": "afc-open-2026",
      "name": "AFC Open 2026",
      "competition_type": "tournament",
      "participant_type": "squad",
      "tier": "tier_1",
      "status": "completed",
      "start_date": "2026-01-01",
      "end_date": "2026-01-02",
      "is_native_afc": true,
      "prize_pool": "$1000"
    }
  ],
  "has_more": false,
  "next_offset": null,
  "total_count": 1
}
```

`prize_pool` appears only if your `include_prize` field toggle is on.
`is_native_afc` is `true` for AFC-run events and `false` for partner-organization
events — it never reveals the underlying organization id.

### 3.2 `GET /events/{slug}/` — event detail

```http
GET /api/v1/partner/events/afc-open-2026/
```

```json
{
  "slug": "afc-open-2026",
  "name": "AFC Open 2026",
  "competition_type": "tournament",
  "participant_type": "squad",
  "tier": "tier_1",
  "status": "completed",
  "start_date": "2026-01-01",
  "end_date": "2026-01-02",
  "is_native_afc": true
}
```

A single event object (not wrapped in a pagination envelope). Returns `404` if the
event is out of your scope or not published (see §6).

### 3.3 `GET /events/{slug}/stages/` — stages (groups nested)

```json
{
  "results": [
    {
      "stage_name": "Grand Final",
      "order": 1,
      "format": "br - normal",
      "status": "completed",
      "start_date": "2026-01-02",
      "end_date": "2026-01-02",
      "groups": [
        {
          "group_name": "Group A",
          "playing_date": "2026-01-02",
          "maps": ["bermuda"]
        }
      ]
    }
  ],
  "has_more": false,
  "next_offset": null,
  "total_count": 1
}
```

`order` is a stable 1-based sequence number within the event (never the internal
stage id). Each group's `maps` array appears only if your `include_maps` toggle is on.

### 3.4 `GET /events/{slug}/matches/` — matches

```json
{
  "results": [
    {
      "match_number": 1,
      "result_inputted": true,
      "map": "bermuda",
      "mvp": "ProGamer"
    }
  ],
  "has_more": false,
  "next_offset": null,
  "total_count": 1
}
```

Room id / room password / room name and internal scoring settings are **never**
returned. `map` is gated on `include_maps`; `mvp` (the in-game handle, or `null`) is
gated on `include_mvp`.

### 3.5 `GET /events/{slug}/standings/` — final standings

```json
{
  "results": [
    {
      "rank": 1,
      "team": "Team Alpha",
      "placement": 1,
      "kills": 10,
      "damage": 2500,
      "assists": 4
    }
  ],
  "has_more": false,
  "next_offset": null,
  "total_count": 1
}
```

A ranked list, winners first. `rank` is a derived 1-based ordinal. For **solo**
events each row carries `username` + `in_game_id` instead of `team`. The stat fields
(`placement`, `kills`, `damage`, `assists`) appear only for the field toggles you have.

### 3.6 `GET /events/{slug}/teams/` — teams

```json
{
  "results": [
    {
      "team": "Team Alpha",
      "team_tag": "ALP",
      "placement": 1,
      "kills": 10,
      "damage": 2500,
      "assists": 4,
      "roster": [
        { "username": "ProGamer", "in_game_id": "UID0001" }
      ]
    }
  ],
  "has_more": false,
  "next_offset": null,
  "total_count": 1
}
```

Stats are aggregated across the team's matches **in this event**. `placement` is the
team's best (lowest) finish. `roster` (public player handles only) appears only if
`include_rosters` is on; each stat field is gated on its own field toggle.

### 3.7 `GET /events/{slug}/players/` — players

```json
{
  "results": [
    {
      "username": "ProGamer",
      "in_game_id": "UID0001",
      "kills": 10,
      "damage": 2500,
      "assists": 4
    }
  ],
  "has_more": false,
  "next_offset": null,
  "total_count": 1
}
```

Each player's stats are folded **for this event only** (not lifetime totals). Only
the public in-game handle (`username`) and in-game id (`in_game_id`) are ever
returned — never real name, email, or Discord id.

---

## 4. Pagination

All **list** endpoints (events, stages, matches, standings, teams, players) are
paginated with `limit` / `offset` query parameters and return a consistent
envelope. The event **detail** endpoint (§3.2) returns a single object and is not
paginated.

| Query param | Meaning | Default | Max |
|---|---|---|---|
| `limit` | Page size (rows per response) | `25` | `100` |
| `offset` | Number of rows to skip | `0` | — |

A `limit` above 100 is silently capped at 100; malformed values fall back to the
defaults (the API never errors on a bad page parameter).

Every paginated response carries this metadata:

| Field | Meaning |
|---|---|
| `results` | The array of rows for this page |
| `total_count` | Total rows across all pages |
| `has_more` | `true` if more pages remain after this one |
| `next_offset` | The `offset` to pass for the next page, or `null` on the last page |

**Paging loop:** start at `offset=0`; while `has_more` is `true`, request again with
`offset = next_offset`. Stop when `has_more` is `false`.

---

## 5. Resource and field toggles

Your access is described entirely by per-partner toggles, all defaulting **off**
(least privilege). AFC turns on exactly what you are entitled to.

### Resource toggles — which endpoints respond

| Toggle | Unlocks |
|---|---|
| `can_read_events` | `GET /events/` and `GET /events/{slug}/` |
| `can_read_stages` | `GET /events/{slug}/stages/` |
| `can_read_matches` | `GET /events/{slug}/matches/` |
| `can_read_standings` | `GET /events/{slug}/standings/` |
| `can_read_teams` | `GET /events/{slug}/teams/` |
| `can_read_players` | `GET /events/{slug}/players/` |

A request to an endpoint whose resource toggle is off returns `403` (see §6).

### Field toggles — which fields appear

Even on an endpoint you can read, stat/detail fields are emitted **only** when the
matching field toggle is on. If a toggle is off, the field is simply **absent** from
the response (not `null`).

| Toggle | Controls |
|---|---|
| `include_placements` | `placement` on standings / teams |
| `include_kills` | `kills` on standings / teams / players |
| `include_damage` | `damage` on standings / teams / players |
| `include_assists` | `assists` on standings / teams / players |
| `include_rosters` | `roster` (player list) on teams |
| `include_maps` | `maps` on groups, `map` on matches |
| `include_prize` | `prize_pool` on events |
| `include_mvp` | `mvp` on matches |

> **What is never returned, under any toggle:** internal database ids, room id /
> password / name, scoring settings, and all PII (real names, emails, Discord ids).
> The API exposes only public handles, slugs, dates, statuses, and the toggled-on
> aggregated stats.

---

## 6. Rate limiting

Each API key has a **per-minute** request budget (default **60 requests/minute**;
AFC can set a different limit on your key). The window is a fixed wall-clock minute.

Every **successful** (`2xx`) response advertises your budget so you can self-throttle
**before** you ever get blocked:

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | Your per-minute ceiling |
| `X-RateLimit-Remaining` | Requests left in the current minute window |

```http
HTTP/1.1 200 OK
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 58
```

When you exceed the limit, the API responds **`429 Too Many Requests`** with a
`Retry-After` header telling you how many seconds to wait before retrying:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 60
Content-Type: application/json

{ "error": "rate_limit_exceeded" }
```

Watch `X-RateLimit-Remaining` and pause when it nears `0`, or honor `Retry-After`
after a `429`.

---

## 7. Error model

All errors return a JSON body of the shape `{ "error": "<message>" }` with the
appropriate HTTP status code:

| Status | When | Example body |
|---|---|---|
| `401 Unauthorized` | Missing, malformed, unknown, revoked, or expired key; suspended partner | `{ "error": "Missing or malformed X-API-Key." }` |
| `403 Forbidden` | Authenticated, but the endpoint's resource toggle is off for you | `{ "error": "resource_not_enabled" }` |
| `404 Not Found` | The event slug is unknown, **or** out of your scope, **or** not published | `{ "error": "not_found" }` |
| `429 Too Many Requests` | Over your per-minute rate limit (carries `Retry-After`) | `{ "error": "rate_limit_exceeded" }` |

> **Why `404` and not `403` for out-of-scope events:** the API never confirms the
> existence of an event you are not allowed to see. An event outside your scope, or
> one AFC has not published to partners, is indistinguishable from a typo'd slug —
> both return `404`. This is deliberate, to avoid leaking the existence of private or
> unpublished events.

---

## 8. Quick start

```bash
# List the events you can read
curl -s https://api.africanfreefirecommunity.com/api/v1/partner/events/ \
  -H "X-API-Key: afcp_3f9a_your_full_key_here"

# Fetch one event's final standings
curl -s https://api.africanfreefirecommunity.com/api/v1/partner/events/afc-open-2026/standings/ \
  -H "X-API-Key: afcp_3f9a_your_full_key_here"
```

If you need a resource or field that is currently absent from your responses, contact
AFC to have the corresponding toggle enabled for your partner account.
