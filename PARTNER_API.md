# AFC Partner Data API

**Version:** v1
**Base URL:** `https://api.africanfreefirecommunity.com/api/v1/partner/`
**Web version of this guide:** `https://africanfreefirecommunity.com/partners/api`

This is the document AFC hands to an approved partner. It explains how to authenticate, what
you can read, what every response looks like, and what to do when a call fails. Everything
here is read-only: the API has no write, no delete, and no live match feed.

The same content is published as a page at the URL above, in English, French and Portuguese.
The page and this file must always say the same thing; when one changes, change both.

Internal design notes live in `tasks/partner-api-design.md`; this document is the one that
goes to the partner.

---

## 1. What the API gives you

The tournament data AFC has published to partners, for the events your organisation has been
granted. An AFC admin publishes an event explicitly, normally once its results are final, so
in practice what you read is settled data rather than a match in progress.

| Resource | What it is |
|---|---|
| Events | The event card: name, slug, dates, tier, status, prize pool |
| Stages and groups | The structure of an event, with each stage's groups |
| Matches | Per-match rows: match number, map, MVP, whether the result is in |
| Standings | The final ranked table for an event |
| Teams | Every registered team, with event-wide aggregated stats and rosters |
| Players | Everyone who recorded stats, with their per-event stats |
| Designs | Branded leaderboard templates: background art, logos, brand colours |

**What it never gives you.** Real names, emails, Discord IDs or any other personal data;
room IDs and room passwords; AFC's internal database IDs; events AFC has not published to
partners; site-wide rankings and tier ladders.

Events are always addressed by their **slug** (`deca-cup-season-5`), never by a numeric ID.

---

## 2. Authenticating

Every request carries your key in the `X-API-Key` header. Keys look like
`afcp_<prefix>_<secret>`.

```bash
curl -H "X-API-Key: afcp_3f9a_1a2b3c..." \
  "https://api.africanfreefirecommunity.com/api/v1/partner/events/"
```

Your key is shown to the AFC admin **once**, at the moment it is created, and is stored only
as a hash. Nobody at AFC can retrieve it later. If you lose it, ask for a new one; if you
think it has leaked, tell AFC immediately and they will revoke it.

Send the key over HTTPS only, from your own servers. Never put it in client-side code, a
mobile app, or a public repository.

### Key lifecycle

A key can be **revoked** (disabled, but kept on AFC's records), **deleted** (removed
entirely), or issued with an **expiry date**. An expiry date means the key works through the
**end** of that day, UTC, which is what makes a key for a single tournament weekend
practical. A revoked, deleted or expired key stops working instantly, as does every key
belonging to a partner account AFC has suspended. All of those return `401`.

If you hold the correct key, the `401` body tells you which of those happened
(`Key expired.`, `Partner suspended.`). If the key itself is wrong, every failure reads
`Unknown or revoked key.` on purpose, so that a stranger guessing at keys learns nothing.

---

## 3. Pagination

Every list endpoint is paginated. There is no way to fetch an unbounded list.

| Parameter | Default | Maximum |
|---|---|---|
| `limit` | 25 | 100 |
| `offset` | 0 | - |

Every list response uses the same envelope:

```json
{
  "results": [ ... ],
  "has_more": true,
  "next_offset": 25,
  "total_count": 55
}
```

Page through by following `next_offset` until `has_more` is `false` (at which point
`next_offset` is `null`). A `limit` above 100 is silently capped at 100; a malformed `limit`
or `offset` falls back to the default rather than erroring, and an `offset` past the end
returns an empty `results` array rather than an error.

The event **detail** endpoint returns a single object and is not paginated.

---

## 4. Rate limits

Requests are limited **per key**, in a fixed one-minute window. Your ceiling is set by AFC
(60 requests/minute unless agreed otherwise).

Every successful response tells you where you stand:

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 52
```

Exceed it and you get `429` with a `Retry-After: 60` header:

```json
{ "error": "rate_limit_exceeded" }
```

Back off for the number of seconds in `Retry-After`. The window is a wall-clock minute, so a
fresh allowance begins when the minute rolls over rather than sixty seconds after your first
call. If AFC issued you more than one key, each key carries its own separate budget.

---

## 5. Errors

Errors raised by the API carry a single `error` field:

```json
{ "error": "not_found" }
```

| Status | Meaning | What to do |
|---|---|---|
| `401` | Missing, malformed, unknown, revoked or expired key; or your account is suspended | Check the header, then contact AFC |
| `403` | `resource_not_enabled` - your account is not entitled to this resource | Ask AFC to enable that resource |
| `404` | `not_found` - the event does not exist, is not published, or is not in your scope | Confirm the slug with AFC |
| `405` | Wrong HTTP verb | Every endpoint is `GET` only |
| `429` | `rate_limit_exceeded` | Honour `Retry-After` |

One exception to the body shape: a `405` is refused by the web framework before the API sees
it, so it carries a `detail` field rather than `error`
(`{"detail": "Method \"POST\" not allowed."}`). A request to a path that does not exist at
all is a plain `404` from the web server and is not JSON, so match on the status code rather
than parsing the body of an unexpected response.

Note the deliberate difference between `403` and `404`. A `403` means the resource type is
switched off for you. A `404` means you cannot see that event, and it is returned whether the
event is out of your scope, unpublished, or simply does not exist. The API will not confirm
that an event exists if you are not allowed to read it, so a `404` is never proof that your
slug is wrong.

---

## 6. What is switched on for you

Your access has three layers, all set by AFC and all closed by default.

**The publish gate** comes first. An AFC admin publishes an event to partners explicitly.
Until they do, no partner can read it however broadly scoped, and it returns `404`.

**Resource toggles** decide which endpoints answer at all. If one is off, that endpoint
returns `403 resource_not_enabled` and the rest keep working.

`events`, `stages`, `matches`, `standings`, `teams`, `players`, `designs`

**Field toggles** decide which fields appear inside a resource you can already read. A field
that is switched off is **absent from the JSON**, not present-and-null.

| Toggle | Adds |
|---|---|
| Placements | `placement` on teams and standings |
| Kills / Damage / Assists | the matching stat on teams, players and standings |
| Rosters | `roster` (the player list) on each team |
| Maps played | `maps` on groups, `map` on matches |
| Prize pool | `prize_pool` on events |
| MVP | `mvp` on matches |
| Images and files | `banner_url`, `rules_file_url`, `logo_url`, `esports_image_url`, and design art |
| Descriptions and rules text | `rules_text` on events, `description` on teams |

A key that is **present and null** means the opposite of an absent one: the field is enabled
for you and the underlying value is genuinely empty, for example a team that never uploaded a
logo. Ask AFC which toggles you have, or call an endpoint and look at the keys you get back.

---

## 7. Media URLs

Every image or file URL is **absolute** and publicly fetchable, so you can download it or
hot-link it directly:

```
https://api.africanfreefirecommunity.com/media/teams_logos/IMG-20260320-WA0061.jpg
```

They are plain URLs with no signature and no expiry, which means they keep working, and also
means anyone you pass one to can fetch it. Treat a media URL as public, because it is.

A missing asset is `null`, never an error. A team with no uploaded logo returns
`"logo_url": null`; the field is only absent if the Images and files toggle is off entirely.

**Cache them.** Download media once and serve it from your own storage rather than
hot-linking on every page view: your pages render faster, and you are unaffected if AFC moves
an asset. Media responses carry `ETag` and `Last-Modified`, so if you would rather revalidate
than re-download, a conditional request with `If-None-Match` gets you a cheap `304`.

---

## 8. Endpoints

All are `GET`. Responses below are real, trimmed for length.

### `GET /events/`

The events you can read, newest first. Paginated.

```json
{
  "results": [
    {
      "slug": "deca-cup-season-5",
      "name": "DECA CUP SEASON 5",
      "competition_type": "tournament",
      "participant_type": "squad",
      "tier": "tier_1",
      "status": "completed",
      "start_date": "2026-06-08",
      "end_date": "2026-06-28",
      "is_native_afc": false
    }
  ],
  "has_more": false,
  "next_offset": null,
  "total_count": 1
}
```

`is_native_afc` tells you whether AFC ran the event itself or an organiser did, without
revealing which organiser.

With Prize pool, Images and files, and Descriptions on, the event also carries:

```json
{
  "prize_pool": "$1000",
  "banner_url": "https://api.africanfreefirecommunity.com/media/event_banner/DECACUP.png",
  "rules_file_url": null,
  "rules_text": "No cheating. Be on time."
}
```

### `GET /events/{event_slug}/`

One event, same shape as a row above. `404` if it is not yours to read.

### `GET /events/{event_slug}/stages/`

Stages in running order, each with its groups nested. `order` is a 1-based sequence number,
not a database ID.

```json
{
  "stage_name": "Grand Final",
  "order": 3,
  "format": "br - normal",
  "status": "completed",
  "start_date": "2026-06-28",
  "end_date": "2026-06-28",
  "groups": [
    { "group_name": "Group A", "playing_date": "2026-06-28", "maps": ["bermuda"] }
  ]
}
```

### `GET /events/{event_slug}/matches/`

```json
{ "match_number": 1, "result_inputted": true, "map": "bermuda", "mvp": "ASN REAPER" }
```

`mvp` is the in-game handle, or `null` when none was recorded.

### `GET /events/{event_slug}/standings/`

The event's final ranked table, `rank` ascending. Squad and duo events are ranked by team;
solo events by player (each row carries `username` and `in_game_id` instead of `team`).

```json
{ "rank": 1, "team": "NO PRESSURE", "placement": 1, "kills": 88 }
```

Ranking uses the same metric as AFC's official standings: placement points plus kill points
plus bonus points minus penalty points, with 1st-place finishes and then total kills breaking
ties. `placement` is the team's **best** finish across the event. Solo events do not record
damage or assists, so those two fields appear on team standings only.

### `GET /events/{event_slug}/teams/`

Every team registered for the event, sorted by name, with its event-wide totals. This list is
the full registration list, not the list of competitors: read `status` to tell them apart.

```json
{
  "team": "ALLSTARS NG",
  "team_tag": "ASN",
  "status": "played",
  "logo_url": "https://api.africanfreefirecommunity.com/media/teams_logos/IMG-0061.jpg",
  "description": "We grind every night.",
  "placement": 1,
  "kills": 44,
  "damage": 0,
  "assists": 0,
  "roster": [
    { "username": "ASN GABBY", "in_game_id": "3098864559", "kills": 3 }
  ]
}
```

#### Team participation status

`status` tells you whether a team actually competed. It is always present, on every plan, and
does not depend on any toggle.

You need it because registering for an AFC event and playing in one are different things. On
AFC's current data, fewer than half of all registrations had played a match. The rest return
zeroed stats and an empty roster, which is easy to mistake for a team that played badly. If
you are drawing a bracket, a standings card or a team count, filter on `status` first.

| Value | Meaning |
|---|---|
| `played` | Turned up and played at least one match. These are the competitors. |
| `registered` | Accepted into the event, has not played a match. |
| `waitlisted` | Signed up but holding a waitlist slot, not a playing slot. |
| `pending` | Registration submitted and awaiting approval. Sponsored events only. |
| `no_show` | Was expected to play and the organizer marked it absent. |
| `withdrawn` | Pulled out of the event. |
| `left` | Left the event. |
| `disqualified` | Removed by the organizer for a rules breach. |

Two notes on how to read it:

- Only one value is returned per team. Where more than one could apply, the more specific one
  wins, in this order: `disqualified`, `withdrawn`, `left`, `pending`, `waitlisted`, `no_show`,
  `played`, `registered`. So a team that played two maps and then withdrew comes back as
  `withdrawn`, not `played`. Its stats are still populated, but they are a partial record of an
  event it did not finish.
- A team can hold match records and still not be `played`. AFC seeds teams into a map before it
  is played, and a team that does not turn up keeps that row with no result. `played` counts
  only matches the team actually contested.

This field was added in a backwards compatible way: it is a new key on an existing response, and
the set of teams returned did not change. If you were already filtering this list yourself, your
filter still behaves exactly as before. The value set above is fixed, so you can switch on it
safely; if we ever need a new state we will add a value rather than change the meaning of one of
these.

### `GET /events/{event_slug}/players/`

Everyone who recorded stats, with stats scoped to **this event** rather than career totals.

```json
{
  "username": "ASN REAPER",
  "in_game_id": "1848789033",
  "esports_image_url": "https://api.africanfreefirecommunity.com/media/esports_pictures/a4b4.jpg",
  "kills": 7
}
```

`esports_image_url` is the player's posed roster photo, for lower-thirds and versus cards. It
is `null` for players who have not uploaded one. Only the public in-game handle (`username`)
and in-game id (`in_game_id`) are ever returned, never a real name, email, or Discord id.

### `GET /events/{event_slug}/designs/`

The branded leaderboard templates behind this event's graphics, so you can produce on-brand
standings cards yourself. Requires the Designs resource toggle; the artwork additionally
requires Images and files.

```json
{
  "name": "DYNASTY CUP",
  "design_type": "leaderboard",
  "text_color": "#FFFFFF",
  "accent_color": "#34d27b",
  "transparent_background": false,
  "max_rows": 18,
  "is_default": true,
  "background_instagram_url": "https://api.africanfreefirecommunity.com/media/org_leaderboard_designs/DYNASTY_IG.png",
  "background_youtube_url": "https://api.africanfreefirecommunity.com/media/org_leaderboard_designs/DYNASTY_YT.png",
  "logos": [
    { "image_url": "https://api.africanfreefirecommunity.com/media/org_leaderboard_logos/sponsor.png",
      "x_pct": 12.5, "y_pct": 8.0, "size": "medium" }
  ]
}
```

The two canvases are Instagram portrait (1080x1350) and YouTube landscape (1920x1080). Logo
positions are a **percentage of the canvas, anchored at the logo's centre**, so the same pair
of numbers places the logo correctly on both sizes. `max_rows` is how many standings rows the
template is designed to hold. Colours and flags are always returned, so you can colour-match
even without the Images and files toggle.

You only ever receive designs belonging to the event's owner: the organiser's library for an
organiser-run event, or AFC's own library for a native AFC event.

---

## 9. Working with the API

**Poll, do not hammer.** Data only changes when AFC publishes or corrects a result. Once a day
during a season, or once an hour around a final, is plenty. There are no webhooks.

**Cache the media.** Banners, logos and design art change rarely. Download them once and serve
them from your own storage rather than hot-linking on every page view.

**Handle absent fields.** A field you are not entitled to is missing from the JSON, so read
defensively rather than assuming a key exists.

**Expect a `404` to be lasting.** If an event you were reading starts returning `404`, AFC has
either un-published it or changed your scope. Retrying will not help; ask AFC.

**Slugs are the identifiers.** Store the event slug, not the position of an event in a list,
because a new event changes every position.

---

## 10. Getting help

Contact your AFC partner manager for a new or rotated key, to change which events or resources
you can read, or to report anything the API returns that looks wrong. Include the endpoint, the
timestamp, and your key's prefix (the `afcp_3f9a` part, never the full key).
