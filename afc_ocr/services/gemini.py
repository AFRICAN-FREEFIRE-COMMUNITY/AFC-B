import base64
import json

import requests
from django.conf import settings

GEMINI_MODEL = "gemini-2.5-pro"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
    f"/models/{GEMINI_MODEL}:generateContent?key={{api_key}}"
)


def build_prompt(aliases: list, team_notes: list) -> str:
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
            for a in aliases[:30]
        )
        base += f"\nKnown player name corrections from past matches:\n{alias_block}\n"

    if team_notes:
        note_block = "\n".join(
            f'  - "{n["username"]}" may appear under team "{n["played_for"]}" as a stand-in'
            for n in team_notes[:10]
        )
        base += f"\nKnown stand-in / sub appearances:\n{note_block}\n"

    return base


def call_gemini(image_bytes: bytes, mime_type: str, aliases: list, team_notes: list) -> dict:
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured in settings.")

    url = GEMINI_URL.format(api_key=api_key)
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

    resp = requests.post(url, json=payload, timeout=90)
    resp.raise_for_status()

    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    return json.loads(text)


def get_prompt_context(match) -> tuple:
    """Returns (aliases, team_notes) to inject into the Gemini prompt."""
    from afc_ocr.models import OCRNameAlias, OCRTeamNote
    from afc_ocr.services.matching import get_registered_players
    from django.db import models as dm

    event = _get_event(match)
    if not event:
        return [], []

    event_type = "solo" if event.participant_type == "solo" else "team"
    registered = get_registered_players(match, event, event_type)
    user_ids = [p["user_id"] for p in registered]

    aliases = list(
        OCRNameAlias.objects
        .filter(user_id__in=user_ids)
        .select_related("user")
        .order_by("-match_count")[:30]
        .values("raw_name", username=dm.F("user__username"))
    )

    team_notes = list(
        OCRTeamNote.objects
        .filter(user_id__in=user_ids)
        .select_related("user", "played_for_team")
        .order_by("-created_at")[:10]
        .values(
            username=dm.F("user__username"),
            played_for=dm.F("played_for_team__team_name"),
        )
    )

    return aliases, team_notes


def _get_event(match):
    if match.leaderboard_id:
        return match.leaderboard.event
    if match.group_id:
        return match.group.stage.event
    return None
