# AFC Free Fire OCR Leaderboard — Full Specification & Developer Guide

**Branch:** `feature/ocr-leaderboard` (both `frontend/` and `backend/`)
**Never merge to `main` / `master` without explicit owner approval.**

---

## Table of Contents

1. [Overview](#overview)
2. [Admin User Flow](#admin-user-flow)
3. [Architecture Overview](#architecture-overview)
4. [Backend — Django App `afc_ocr`](#backend--django-app-afc_ocr)
   - [Models](#models)
   - [API Endpoints](#api-endpoints)
   - [Gemini Integration](#gemini-integration)
   - [DB Fuzzy Matching](#db-fuzzy-matching)
   - [Wrong Team Detection](#wrong-team-detection)
   - [Correction & Learning System](#correction--learning-system)
   - [Points Attribution](#points-attribution)
5. [Frontend](#frontend)
   - [Files to Create / Modify](#files-to-create--modify)
   - [Map Selection Step](#map-selection-step)
   - [ImageUploadStep UI](#imageuploadstep-ui)
   - [OCRReviewTable Component](#ocrreviewtable-component)
6. [Environment Variables](#environment-variables)
7. [Integration with Existing System](#integration-with-existing-system)
8. [Local Test Tools](#local-test-tools)
   - [CLI Script (`ocr_test.py`)](#cli-script-ocr_testpy)
   - [Browser App (`ocr_local_app.py`)](#browser-app-ocr_local_apppy)
9. [Key Files Reference](#key-files-reference)
10. [Out of Scope](#out-of-scope)

---

## Overview

Admins upload Free Fire match result screenshots per map. Gemini 2.5 Pro extracts player names and kill counts from the image. Extracted names are fuzzy-matched against players registered in the specific match/event in the database. The system flags low-confidence matches and wrong-team appearances for admin review. Once the admin confirms or corrects the results, the data is committed to the leaderboard using the existing manual entry scoring engine.

Over time the system learns from corrections — so repeat players auto-match at full confidence without any admin intervention.

---

## Admin User Flow

```
Admin opens the leaderboard wizard for a match
        |
        v
Selects "Image Upload" as the entry method
        |
        v
[MAP SELECTION STEP]
Admin chooses which map this screenshot belongs to
  - Dropdown shows all maps/rounds in this match
    e.g. "Map 1 – Bermuda", "Map 2 – Kalahari", "Map 3 – Purgatory"
  - One screenshot per map (admin repeats the flow for each map)
        |
        v
Admin uploads the screenshot for the selected map
  - Drag-and-drop or file picker (PNG / JPG / WEBP)
        |
        v
Backend receives image + match_id + map_index
  → Calls Gemini 2.5 Pro with the image
  → Gemini returns raw extraction:
       { placements: [{ placement: N, players: [{ name, kills }] }] }
        |
        v
Backend runs DB matching:
  → Loads all players registered in this match's event
  → Checks OCRNameAlias table first (instant, confidence = 1.0)
  → Falls back to rapidfuzz against registered usernames
  → Each row gets: matched_user_id, matched_username, confidence
        |
        v
Backend runs Wrong Team Detection:
  → Each placement group = one team finishing at that rank
  → Checks: do all matched players in this placement group
    belong to the same registered tournament team?
  → If not → flags the row with team_mismatch = true
        |
        v
Returns OCRSession draft to frontend
        |
        v
[REVIEW TABLE]
Admin sees a table with one row per extracted player:
  | Raw Name (from screenshot) | Matched Player (dropdown) | Kills | Placement | Confidence | Team | Flags |

  Confidence badge colours:
    Green  ≥ 0.85  → auto-matched, likely correct
    Yellow 0.75–0.84 → possible match, admin should verify
    Red    < 0.75   → needs admin correction before confirm

  Team mismatch flag:
    Orange warning icon → player's matched team ≠ the placement group's team
    Admin can either:
      (a) Reassign to correct player (name OCR was wrong)
      (b) Confirm sub — player genuinely played for a different team this match
        |
        v
Admin corrects any red/orange rows:
  - Changes matched player via searchable dropdown
  - Adjusts kills if misread
  - Removes rows that don't belong (OCR noise)
  - Marks confirmed subs (team mismatch acknowledged)
        |
        v
Admin clicks "Confirm & Submit"
        |
        v
Backend:
  → Saves all admin corrections to OCRNameAlias
     (raw_name → corrected user, for future auto-matching)
  → Saves confirmed team subs to OCRTeamNote
     (user played for different team — noted for future prompt hints)
  → Calls existing manual entry endpoint with the corrected data:
       POST /events/enter-solo-match-result-manual/  (solo events)
       POST /events/enter-team-match-result-manual/  (team events)
  → OCRSession marked as committed
        |
        v
Leaderboard scores updated — same as if entered manually
```

---

## Architecture Overview

```
Screenshot
    |
    v
Gemini 2.5 Pro (Vision)
    |  raw { name, kills, placement }
    v
OCR Service (afc_ocr)
    |
    ├─ OCRNameAlias lookup  (exact → confidence 1.0)
    ├─ rapidfuzz match      (fuzzy → confidence 0.0–1.0)
    ├─ Wrong team check     (placement group vs registered roster)
    |
    v
OCRSession (draft, stored server-side)
    |
    v
Admin Review UI (frontend)
    |  corrections
    v
OCR Service
    ├─ Save corrections → OCRNameAlias
    ├─ Save sub notes   → OCRTeamNote
    v
Existing manual entry endpoints
    v
Leaderboard scores
```

---

## Backend — Django App `afc_ocr`

Create as a new standalone Django app: `backend/afc_ocr/`

Register in `afc/settings.py`:
```python
INSTALLED_APPS = [
    ...
    "afc_ocr",
]
```

Register URLs in `afc/urls.py`:
```python
path("events/", include("afc_ocr.urls")),
```

---

### Models

```python
# afc_ocr/models.py

import uuid
from django.db import models


class OCRSession(models.Model):
    """
    Server-side draft for one map's OCR result.
    Stays alive until the admin commits or discards it.
    The admin can close the tab and return — session persists.
    """

    STATUS_CHOICES = [
        ("pending_review", "Pending Review"),
        ("committed",      "Committed"),
        ("discarded",      "Discarded"),
    ]

    EVENT_TYPE_CHOICES = [
        ("solo", "Solo"),
        ("team", "Team"),
    ]

    session_id  = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    match       = models.ForeignKey(
        "afc_tournament_and_scrims.Match",
        on_delete=models.CASCADE,
        related_name="ocr_sessions",
    )
    map_index   = models.PositiveSmallIntegerField(
        help_text="Which map in the match this screenshot covers (1-indexed)."
    )
    created_by  = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.CASCADE,
        related_name="ocr_sessions",
    )
    status      = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending_review")
    event_type  = models.CharField(max_length=10, choices=EVENT_TYPE_CHOICES)

    # Raw Gemini output — stored for debugging / re-processing
    raw_output  = models.JSONField()

    # Matched & annotated rows ready for the review table
    # Schema per row — see Draft Rows Schema below
    draft_rows  = models.JSONField()

    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"OCRSession {self.session_id} | Match {self.match_id} | Map {self.map_index}"
```

**Draft rows schema (one object per extracted player):**

```json
{
  "row_id": "uuid-string",
  "raw_name": "BADメSTAN.AE",
  "matched_user_id": 42,
  "matched_username": "BADSTAN_AE",
  "confidence": 0.88,
  "kills": 5,
  "placement": 1,
  "matched_team_id": 7,
  "matched_team_name": "AE Squad",
  "expected_team_id": 7,
  "team_mismatch": false,
  "admin_confirmed_sub": false,
  "top_candidates": [
    {"user_id": 42, "username": "BADSTAN_AE",   "confidence": 0.88},
    {"user_id": 19, "username": "BADSTAR_AFC",  "confidence": 0.61},
    {"user_id": 88, "username": "BAD_STANZA",   "confidence": 0.54}
  ]
}
```

---

```python
class OCRNameAlias(models.Model):
    """
    Maps a raw OCR name (as Gemini reads it from the screenshot)
    to the correct registered user.

    Built passively from admin corrections. After 2-3 tournaments,
    frequent players match at confidence 1.0 with no admin input.
    """

    raw_name    = models.CharField(max_length=100, db_index=True, unique=True)
    user        = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="ocr_aliases",
    )
    match_count = models.PositiveIntegerField(default=1)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "OCR Name Alias"
        verbose_name_plural = "OCR Name Aliases"

    def __str__(self):
        return f'"{self.raw_name}" → {self.user}'
```

---

```python
class OCRTeamNote(models.Model):
    """
    Records when a player was confirmed to have played for a team
    other than their registered team (e.g. a sub / stand-in).

    Used to inject context hints into future Gemini prompts so the
    model knows certain players routinely appear under different teams.
    """

    user            = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.CASCADE,
        related_name="team_notes",
    )
    registered_team = models.ForeignKey(
        "afc_team.Team",
        on_delete=models.CASCADE,
        related_name="sub_out_notes",
        null=True, blank=True,
    )
    played_for_team = models.ForeignKey(
        "afc_team.Team",
        on_delete=models.CASCADE,
        related_name="sub_in_notes",
    )
    match           = models.ForeignKey(
        "afc_tournament_and_scrims.Match",
        on_delete=models.CASCADE,
    )
    confirmed_by    = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="confirmed_team_notes",
    )
    created_at      = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} subbed for {self.played_for_team} in match {self.match_id}"
```

---

### API Endpoints

Register in `afc_ocr/urls.py`:

```python
from django.urls import path
from . import views

urlpatterns = [
    path("ocr-match-result/",               views.upload_ocr_session,    name="ocr_upload"),
    path("ocr-session/<uuid:session_id>/",  views.ocr_session_detail,    name="ocr_session_detail"),
    path("ocr-session/<uuid:session_id>/commit/", views.commit_ocr_session, name="ocr_session_commit"),
]
```

| Method | URL | Auth | Purpose |
|--------|-----|------|---------|
| `POST` | `/events/ocr-match-result/` | Admin | Upload screenshot → run Gemini → return session draft |
| `GET` | `/events/ocr-session/<id>/` | Admin | Fetch existing draft (tab-restore) |
| `PATCH` | `/events/ocr-session/<id>/` | Admin | Admin updates individual rows (correct a match, adjust kills, mark sub) |
| `DELETE` | `/events/ocr-session/<id>/` | Admin | Discard session |
| `POST` | `/events/ocr-session/<id>/commit/` | Admin | Commit → save aliases → call manual entry → mark committed |

**POST `/events/ocr-match-result/` request:**
```json
{
  "match_id": 123,
  "map_index": 1
}
```
Plus a `multipart/form-data` image file field: `screenshot`.

**POST `/events/ocr-match-result/` response:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending_review",
  "event_type": "team",
  "draft_rows": [ ...see draft_rows schema above... ]
}
```

**PATCH `/events/ocr-session/<id>/` request** (update one row):
```json
{
  "row_id": "uuid-string",
  "matched_user_id": 99,
  "kills": 6,
  "admin_confirmed_sub": true
}
```

**POST `/events/ocr-session/<id>/commit/` request:**
```json
{
  "final_rows": [
    {
      "row_id": "uuid-string",
      "matched_user_id": 42,
      "kills": 5,
      "placement": 1,
      "matched_team_id": 7,
      "admin_confirmed_sub": false
    },
    ...
  ]
}
```

---

### Gemini Integration

**Model:** `gemini-2.5-pro`

**API call (REST, no SDK required):**

```python
import base64
import json
import requests

GEMINI_API_KEY = "your-key-here"
GEMINI_MODEL   = "gemini-2.5-pro"
GEMINI_URL     = (
    f"https://generativelanguage.googleapis.com/v1beta"
    f"/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)


def build_prompt(aliases: list[dict], team_notes: list[dict]) -> str:
    """
    Builds the Gemini prompt, injecting known corrections from past matches.

    aliases    — list of {"raw_name": "...", "username": "..."}
    team_notes — list of {"username": "...", "played_for": "TeamName"}
    """
    base = """You are analyzing a Free Fire battle royale match result screen.

This is a TEAM match. The screen shows placements with teams/players listed.
The layout may be split into two columns (e.g. placements 1-5 on the left, 6-11 on the right).
Each placement row contains players from the same team (usually 2-4 players).
"Eliminations" next to a player = their kill count.

Return a JSON object with this exact structure:
{
  "match_type": "team",
  "placements": [
    {
      "placement": 1,
      "players": [
        {"name": "PlayerName", "kills": 3},
        {"name": "OtherPlayer", "kills": 1}
      ]
    }
  ]
}

Critical rules:
- Include ALL placements visible (both columns)
- Copy player names EXACTLY — including underscores, dots, symbols, unicode
- "kills" is always an integer (0 if not shown)
- Return ONLY raw JSON — no markdown fences, no explanation
"""

    if aliases:
        alias_block = "\n".join(
            f'  - "{a["raw_name"]}" is the player "{a["username"]}"'
            for a in aliases[:30]           # cap at 30 to keep prompt lean
        )
        base += f"\nKnown player name corrections from past matches:\n{alias_block}\n"

    if team_notes:
        note_block = "\n".join(
            f'  - "{n["username"]}" may appear under team "{n["played_for"]}" as a stand-in'
            for n in team_notes[:10]
        )
        base += f"\nKnown stand-in / sub appearances:\n{note_block}\n"

    return base


def call_gemini(image_bytes: bytes, mime_type: str,
                aliases: list, team_notes: list) -> dict:
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": build_prompt(aliases, team_notes)},
                    {"inline_data": {"mime_type": mime_type, "data": b64}},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    resp = requests.post(GEMINI_URL, json=payload, timeout=90)
    resp.raise_for_status()

    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    return json.loads(text)
```

---

### DB Fuzzy Matching

After Gemini returns raw names, match each name against players registered in the event:

```python
from rapidfuzz import process, fuzz
from .models import OCRNameAlias


CONFIDENCE_AUTO   = 0.85   # >= this: auto-matched (green)
CONFIDENCE_WARN   = 0.75   # >= this: flag for review (yellow)
                            # < 0.75:  must correct before confirm (red)


def get_registered_players(match) -> list[dict]:
    """
    Returns all players registered in the match's event.
    Format: [{"user_id": int, "username": str, "team_id": int, "team_name": str}]
    """
    from afc_tournament_and_scrims.models import RegisteredCompetitors, TournamentTeam

    event = match.stage.event  # adjust traversal to your FK chain

    # Team events: pull tournament team rosters
    teams = TournamentTeam.objects.filter(event=event).prefetch_related("members")
    players = []
    for team in teams:
        for member in team.members.all():
            players.append({
                "user_id":   member.player_id,
                "username":  member.username,
                "team_id":   team.tournament_team_id,
                "team_name": team.team_name,
            })
    return players


def match_name(raw_name: str, registered: list[dict]) -> dict:
    """
    1. Check OCRNameAlias for an exact match (confidence = 1.0).
    2. Fall back to rapidfuzz against registered usernames.
    3. Return top 3 candidates.
    """
    import uuid

    # Step 1: exact alias lookup
    alias = OCRNameAlias.objects.filter(raw_name__iexact=raw_name).select_related("user").first()
    if alias and alias.user:
        reg = next((p for p in registered if p["user_id"] == alias.user_id), None)
        return {
            "row_id":           str(uuid.uuid4()),
            "raw_name":         raw_name,
            "matched_user_id":  alias.user_id,
            "matched_username": alias.user.username,
            "confidence":       1.0,
            "matched_team_id":  reg["team_id"]   if reg else None,
            "matched_team_name": reg["team_name"] if reg else None,
            "top_candidates":   [],
        }

    # Step 2: rapidfuzz
    usernames = [p["username"] for p in registered]
    results   = process.extract(
        raw_name, usernames,
        scorer=fuzz.WRatio,
        limit=3,
        score_cutoff=40,
    )

    if not results:
        return {
            "row_id":           str(uuid.uuid4()),
            "raw_name":         raw_name,
            "matched_user_id":  None,
            "matched_username": None,
            "confidence":       0.0,
            "matched_team_id":  None,
            "matched_team_name": None,
            "top_candidates":   [],
        }

    top_candidates = []
    for username, score, _ in results:
        player = next(p for p in registered if p["username"] == username)
        top_candidates.append({
            "user_id":    player["user_id"],
            "username":   username,
            "confidence": round(score / 100, 3),
        })

    best = top_candidates[0]
    player = next(p for p in registered if p["user_id"] == best["user_id"])

    return {
        "row_id":            str(uuid.uuid4()),
        "raw_name":          raw_name,
        "matched_user_id":   best["user_id"],
        "matched_username":  best["username"],
        "confidence":        best["confidence"],
        "matched_team_id":   player["team_id"],
        "matched_team_name": player["team_name"],
        "top_candidates":    top_candidates,
    }
```

---

### Wrong Team Detection

After all names in a placement group are matched, check if they all belong to the same registered tournament team:

```python
def detect_team_mismatches(draft_rows: list[dict]) -> list[dict]:
    """
    For each placement group, the players should all be on the same
    registered tournament team. If not, flag team_mismatch = True.

    Logic:
    - Group rows by placement number.
    - For each group, find the most common matched_team_id (majority vote).
    - Any player whose matched_team_id differs from the majority is flagged.
    """
    from collections import Counter

    # Group by placement
    groups: dict[int, list] = {}
    for row in draft_rows:
        p = row.get("placement", 0)
        groups.setdefault(p, []).append(row)

    result = []
    for placement, rows in groups.items():
        team_ids = [r.get("matched_team_id") for r in rows if r.get("matched_team_id")]
        if not team_ids:
            # No matches at all — flag everything in this group
            for row in rows:
                row["team_mismatch"]        = True
                row["admin_confirmed_sub"]  = False
                row["expected_team_id"]     = None
            result.extend(rows)
            continue

        majority_team = Counter(team_ids).most_common(1)[0][0]

        for row in rows:
            row["expected_team_id"]    = majority_team
            row["team_mismatch"]       = (
                row.get("matched_team_id") is not None
                and row["matched_team_id"] != majority_team
            )
            row["admin_confirmed_sub"] = False
            result.append(row)

    return result
```

**What the admin sees for a team mismatch:**

```
#3  | ঔPB BISHOP.%  | → PB_Bishop  (88%) ⚠ Team mismatch
                          Registered: AE Squad
                          This placement group: KN Clan
    [ Change player ▾ ]  [ Confirm sub — played for KN Clan ]
```

The admin has two choices:
- **Change player** — the name was matched to the wrong person. Use the dropdown to pick the correct player.
- **Confirm sub** — the player genuinely played as a stand-in for a different team this match. Click to acknowledge. This saves an `OCRTeamNote`.

---

### Correction & Learning System

Every time an admin corrects or confirms something, the system learns:

#### 1. Name Alias Learning (`OCRNameAlias`)

```python
def save_name_corrections(final_rows: list[dict], original_rows: list[dict]):
    """
    Called during commit. For every row where the admin changed the
    matched player, save the correction to OCRNameAlias.
    """
    original_map = {r["row_id"]: r for r in original_rows}

    for row in final_rows:
        orig = original_map.get(row["row_id"])
        if not orig:
            continue

        # Admin changed the matched player
        if row["matched_user_id"] != orig.get("matched_user_id"):
            alias, created = OCRNameAlias.objects.get_or_create(
                raw_name=row["raw_name"],
                defaults={"user_id": row["matched_user_id"]},
            )
            if not created:
                # Update to the corrected user and increment seen count
                alias.user_id     = row["matched_user_id"]
                alias.match_count = models.F("match_count") + 1
                alias.save()
```

#### 2. Team Sub Learning (`OCRTeamNote`)

```python
def save_team_notes(final_rows: list[dict], match, confirmed_by_user):
    """
    For every row where the admin confirmed a sub (team mismatch acknowledged),
    save an OCRTeamNote so future prompts mention it.
    """
    for row in final_rows:
        if row.get("admin_confirmed_sub") and row.get("team_mismatch"):
            OCRTeamNote.objects.create(
                user_id            = row["matched_user_id"],
                registered_team_id = row.get("expected_team_id"),
                played_for_team_id = row["matched_team_id"],
                match              = match,
                confirmed_by       = confirmed_by_user,
            )
```

#### 3. Injecting Learning into Future Prompts

Before calling Gemini for any new session, load recent aliases and team notes:

```python
def get_prompt_context(match) -> tuple[list, list]:
    """Returns (aliases, team_notes) to inject into the Gemini prompt."""
    event = match.stage.event

    # Get aliases for players registered in this event
    registered_ids = get_registered_players(match)
    user_ids = [p["user_id"] for p in registered_ids]

    aliases = list(
        OCRNameAlias.objects
        .filter(user_id__in=user_ids)
        .select_related("user")
        .order_by("-match_count")[:30]
        .values("raw_name", username=models.F("user__username"))
    )

    # Get sub notes for players in this event
    team_notes = list(
        OCRTeamNote.objects
        .filter(user_id__in=user_ids)
        .select_related("user", "played_for_team")
        .order_by("-created_at")[:10]
        .values(
            username   = models.F("user__username"),
            played_for = models.F("played_for_team__team_name"),
        )
    )

    return aliases, team_notes
```

After enough tournaments:
- Players with clean usernames match at **confidence 1.0** via alias (no fuzzy matching needed)
- Repeat subs are pre-flagged in the Gemini prompt so the model already knows who might appear where
- Admin workload during review approaches zero for established players

---

### Points Attribution

The OCR commit step does **not** implement any scoring logic. It feeds data into the existing manual entry endpoints which already handle all point calculations.

**Solo events:**
```
POST /events/enter-solo-match-result-manual/
{
  "match_id": 123,
  "results": [
    { "competitor_id": 42, "placement": 1, "kills": 7, "played": true,
      "bonus_points": 0, "penalty_points": 0 },
    ...
  ]
}
```

**Team events:**
```
POST /events/enter-team-match-result-manual/
{
  "match_id": 123,
  "results": [
    {
      "tournament_team_id": 7,
      "placement": 1,
      "played": true,
      "players": [
        { "user_id": 42, "kills": 5, "damage": 0, "assists": 0, "played": true },
        ...
      ]
    },
    ...
  ]
}
```

The existing `Leaderboard.leaderboard_method = 'image_upload'` is already a valid choice in the schema — no migration needed for this field.

---

## Frontend

### Files to Create / Modify

| File | Action |
|------|--------|
| `app/(a)/a/leaderboards/_components/MapSelectionStep.tsx` | **New** — map picker before upload |
| `app/(a)/a/leaderboards/_components/ImageUploadStep.tsx` | **Replace** placeholder with real upload + Gemini polling |
| `app/(a)/a/leaderboards/_components/OCRReviewTable.tsx` | **New** — review table with confidence badges, team warnings, correction dropdowns |
| `lib/api/ocr.ts` | **New** — typed API functions for all OCR endpoints |

---

### Map Selection Step

Before the upload, the admin picks which map the screenshot belongs to. Each map in the match is listed by name and round number.

```tsx
// MapSelectionStep.tsx
interface Map {
  map_index: number;
  map_name: string;      // e.g. "Bermuda", "Kalahari"
  is_completed: boolean; // whether a result already exists for this map
}

interface Props {
  matchId: number;
  maps: Map[];
  onSelect: (mapIndex: number) => void;
  onBack: () => void;
}
```

The admin selects a map → proceeds to ImageUploadStep with `mapIndex` set.

---

### ImageUploadStep UI

1. **Upload zone** — drag-and-drop or click, accepts PNG / JPG / WEBP, single file per map
2. **Processing state** — spinner with "Analysing screenshot..." while Gemini runs (typically 5–15s)
3. **On success** — automatically advances to the review table
4. **On failure** — shows error with a retry button; keeps the image loaded

```tsx
interface Props {
  matchId: number;
  mapIndex: number;
  onSessionReady: (sessionId: string, draftRows: DraftRow[]) => void;
  onBack: () => void;
}
```

---

### OCRReviewTable Component

The core review UI. One row per extracted player.

```tsx
interface DraftRow {
  row_id: string;
  raw_name: string;
  matched_user_id: number | null;
  matched_username: string | null;
  confidence: number;
  kills: number;
  placement: number;
  matched_team_id: number | null;
  matched_team_name: string | null;
  expected_team_id: number | null;
  team_mismatch: boolean;
  admin_confirmed_sub: boolean;
  top_candidates: { user_id: number; username: string; confidence: number }[];
}

interface Props {
  sessionId: string;
  draftRows: DraftRow[];
  registeredPlayers: { user_id: number; username: string; team_name: string }[];
  onCommit: () => void;
  onBack: () => void;
}
```

**Confidence badge logic:**

```tsx
function ConfidenceBadge({ value }: { value: number }) {
  if (value >= 0.85) return <Badge className="border-green-500  text-green-400">Auto</Badge>;
  if (value >= 0.75) return <Badge className="border-yellow-500 text-yellow-400">{Math.round(value * 100)}%</Badge>;
  return                     <Badge className="border-red-500   text-red-400">Review</Badge>;
}
```

**Confirm button gate:**
- Disabled if any row has `confidence < 0.75` AND `matched_user_id === null`
- Disabled if any row has `team_mismatch === true` AND `admin_confirmed_sub === false`
- Enabled once every red row has been resolved and every orange warning has been acknowledged

**Team mismatch warning UI:**

```tsx
{row.team_mismatch && !row.admin_confirmed_sub && (
  <div className="flex items-center gap-2 text-orange-400 text-xs mt-1">
    <IconAlertTriangle size={14} />
    <span>
      Matched to <strong>{row.matched_team_name}</strong> but this placement group
      belongs to <strong>{expectedTeamName}</strong>.
    </span>
    <Button size="sm" variant="outline" onClick={() => confirmSub(row.row_id)}>
      Confirm Sub
    </Button>
  </div>
)}
```

---

## Environment Variables

```bash
# backend/.env
GEMINI_API_KEY=your-gemini-api-key-here

# Already required — no change needed
NEXT_PUBLIC_BACKEND_API_URL=https://api.africanfreefirecommunity.com
```

Add to `afc/settings.py`:
```python
import os
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
```

---

## Integration with Existing System

The OCR feature is a **pre-fill layer** on top of the existing manual entry system. The scoring engine, leaderboard calculation, and all downstream logic are untouched.

- Commit calls `enter-solo-match-result-manual` or `enter-team-match-result-manual` — same as `ManualMatchResultStep.tsx`
- `Leaderboard.leaderboard_method = 'image_upload'` is already valid in the schema
- No new migration needed for the leaderboard field
- Migrations needed only for: `OCRSession`, `OCRNameAlias`, `OCRTeamNote`

---

## Local Test Tools

Two tools for testing OCR extraction locally without connecting to the Django backend.

### CLI Script (`ocr_test.py`)

Processes a folder of screenshots and prints extracted player data to the console. Saves full JSON output.

**Location:** `WEBSITE/ocr_test.py`

**Usage:**
```bash
cd WEBSITE

# Process all screenshots in the configured folder
python ocr_test.py

# Process only screenshots #1, #3, and #5 (1-indexed)
python ocr_test.py 1 3 5

# List available screenshots without processing
python ocr_test.py --list
```

**Setup:**
1. Edit `SCREENSHOTS_DIR` at the top of the file to point to your folder
2. Edit `API_KEY` with your Gemini API key
3. Run — no extra dependencies beyond `requests` (already installed)

**Full source:**

```python
#!/usr/bin/env python3
"""
AFC Free Fire OCR Test Script
Sends match result screenshots to Gemini Vision and extracts structured player data.

Usage:
  python ocr_test.py              # process all screenshots
  python ocr_test.py 1 3 5        # process only screenshots #1, #3, #5 (1-indexed)
  python ocr_test.py --list       # list available screenshots without processing
"""

import base64
import io
import json
import sys
from pathlib import Path

import requests

# Force UTF-8 output on Windows console
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Config ─────────────────────────────────────────────────────────────────────

API_KEY = "your-gemini-api-key-here"
MODEL   = "gemini-2.5-pro"  # or "gemini-2.5-flash" for faster/cheaper

SCREENSHOTS_DIR = Path(r"C:\path\to\your\screenshots")

# ── Prompt ─────────────────────────────────────────────────────────────────────

PROMPT = """You are analyzing a Free Fire battle royale match result screen.

This is a TEAM match. The screen shows placements with teams/players listed.
The layout may be split into two columns (e.g. placements 1-5 on the left, 6-11 on the right).
Each placement row contains players from the same team (usually 2 or 4 players).
"Eliminations" next to a player = their kill count.

Return a JSON object with this exact structure:
{
  "match_type": "team",
  "placements": [
    {
      "placement": 1,
      "players": [
        {"name": "PlayerName", "kills": 3},
        {"name": "OtherPlayer", "kills": 1}
      ]
    }
  ]
}

Critical rules:
- Include ALL placements visible in the image (both left and right columns)
- Player names may have underscores, dots, numbers, symbols — copy EXACTLY as shown
- If a character is unclear, copy your best guess (do not skip players)
- "kills" is always an integer (0 if no kills shown)
- Return ONLY the raw JSON object — no markdown, no triple backticks, no explanation
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def encode_image(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else \
           "image/png"  if suffix == ".png" else "image/webp"
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8"), mime


def call_gemini(image_path: Path) -> dict:
    b64, mime = encode_image(image_path)

    payload = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    url  = (f"https://generativelanguage.googleapis.com/v1beta"
            f"/models/{MODEL}:generateContent?key={API_KEY}")
    resp = requests.post(url, json=payload, timeout=90)
    resp.raise_for_status()

    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text  = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    return json.loads(text)


def print_result(filename: str, result: dict):
    sep = "-" * 60
    print(f"\n{sep}\n  {filename}\n{sep}")
    for entry in sorted(result.get("placements", []), key=lambda x: x.get("placement", 99)):
        place   = entry.get("placement", "?")
        players = entry.get("players", [])
        if players:
            print(f"  #{place:<4}  {players[0].get('name','?'):<28}  {players[0].get('kills',0)} kill(s)")
            for p in players[1:]:
                print(f"         {p.get('name','?'):<28}  {p.get('kills',0)} kill(s)")
        else:
            print(f"  #{place:<4}  (no players)")


def list_screenshots() -> list[Path]:
    return sorted(
        list(SCREENSHOTS_DIR.glob("*.jpeg")) + list(SCREENSHOTS_DIR.glob("*.jpg")) +
        list(SCREENSHOTS_DIR.glob("*.png"))  + list(SCREENSHOTS_DIR.glob("*.webp")),
        key=lambda p: p.name,
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not SCREENSHOTS_DIR.exists():
        print(f"ERROR: Directory not found: {SCREENSHOTS_DIR}"); sys.exit(1)

    screenshots = list_screenshots()
    if not screenshots:
        print(f"No screenshots found in: {SCREENSHOTS_DIR}"); sys.exit(1)

    if "--list" in sys.argv:
        print(f"Found {len(screenshots)} screenshot(s):\n")
        for i, p in enumerate(screenshots, 1):
            print(f"  [{i:>2}] {p.name}")
        return

    index_args = [a for a in sys.argv[1:] if a.isdigit()]
    if index_args:
        screenshots = [screenshots[int(i)-1] for i in index_args
                       if int(i)-1 < len(screenshots)]

    print(f"\nModel : {MODEL}\nImages: {len(screenshots)}\nDir   : {SCREENSHOTS_DIR}\n")
    all_results = []

    for i, path in enumerate(screenshots, 1):
        print(f"[{i}/{len(screenshots)}] {path.name}")
        try:
            result = call_gemini(path)
            all_results.append({"file": path.name, "result": result, "error": None})
            print_result(path.name, result)
        except Exception as e:
            print(f"  ERROR: {e}")
            all_results.append({"file": path.name, "result": None, "error": str(e)})

    out = Path(__file__).parent / "ocr_test_output.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    ok = sum(1 for r in all_results if r["error"] is None)
    print(f"\n{'-'*60}\nDone. {ok}/{len(all_results)} succeeded.\nFull JSON -> {out}")


if __name__ == "__main__":
    main()
```

---

### Browser App (`ocr_local_app.py`)

A local single-file Flask web app with a dark UI. Drag-and-drop images, click "Run OCR", see results with placements, player names, kill counts, and a confidence badge. No database — pure Gemini extraction, useful for quickly testing new screenshots.

**Location:** `WEBSITE/ocr_local_app.py`

**Install dependency (one-time):**
```bash
python -m pip install flask
```

**Run:**
```bash
cd WEBSITE
python ocr_local_app.py
```

Then open **http://localhost:5050** in your browser.

**What it does:**
- Upload one or more screenshots via drag-and-drop or file picker
- Processes each image sequentially (shows per-image status dots)
- Displays placement table with player chips and kill counts
- Colour-coded placement badges: gold #1, silver #2, bronze #3
- Expandable raw JSON section per image for debugging
- Does **not** hit the Django database — extraction only

**Screenshot of the flow:**
```
[Drop zone]
     |
  Drop PNG/JPG/WEBP files
     |
  Thumbnails appear with pending dots
     |
  Click "Run OCR"
     |
  Each dot: grey → amber (processing) → green (done) / red (error)
     |
  Results card appears per image:
  ┌─────────────────────────────────────────────┐
  │ [thumb]  filename.jpg           [Done ✓]    │
  ├─────────────────────────────────────────────┤
  │ #1  AE.MI6          4k                      │
  │     AE.AKAZA        1k                      │
  │     ZORO            3k                      │
  │     BADメSTAN.AE    5k                      │
  │ #2  PUNKY CVS       2k   UG〆NINJA CVS  8k  │
  │ ...                                         │
  │ ▶ Raw JSON                                  │
  └─────────────────────────────────────────────┘
```

**Full source:** see `WEBSITE/ocr_local_app.py` — the entire app is a single Python file (~330 lines) with the HTML/CSS/JS embedded as a string. Copy the file as-is.

---

## Key Files Reference

### Backend

| File | Purpose |
|------|---------|
| `afc_ocr/models.py` | `OCRSession`, `OCRNameAlias`, `OCRTeamNote` |
| `afc_ocr/views.py` | All 5 OCR endpoints |
| `afc_ocr/services/gemini.py` | Gemini API call + prompt builder |
| `afc_ocr/services/matching.py` | Fuzzy matching + wrong team detection |
| `afc_ocr/services/commit.py` | Save corrections, call manual entry endpoints |
| `afc_ocr/urls.py` | URL routing for OCR endpoints |
| `afc_tournament_and_scrims/views.py` | `enter_solo_match_result_manual`, `enter_team_match_result_manual` |

### Frontend

| File | Purpose |
|------|---------|
| `app/(a)/a/leaderboards/_components/MapSelectionStep.tsx` | Map picker (new) |
| `app/(a)/a/leaderboards/_components/ImageUploadStep.tsx` | Upload zone + Gemini processing state |
| `app/(a)/a/leaderboards/_components/OCRReviewTable.tsx` | Review table with correction UI (new) |
| `lib/api/ocr.ts` | Typed API client for all OCR endpoints |

### Local Test Tools

| File | Purpose |
|------|---------|
| `WEBSITE/ocr_test.py` | CLI batch processor — run against a folder of screenshots |
| `WEBSITE/ocr_local_app.py` | Browser UI for interactive testing (Flask, port 5050) |
| `WEBSITE/ocr_test_output.json` | Raw JSON output from the last CLI run |

---

## Out of Scope

- Automatic bulk-processing without admin review
- OCR of non-Free Fire game screenshots
- Historical session browsing (sessions discarded after commit)
- Solo event support in the local test tools (tools are team-mode only for now)
- Bulk alias import/export UI (managed via Django admin)
