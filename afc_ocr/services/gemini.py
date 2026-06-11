import base64
import json

import requests
from django.conf import settings

# Default Gemini model id + the label other modules import (training_capture / tasks teacher_model).
# The EFFECTIVE model is settings.GEMINI_MODEL (env GEMINI_MODEL), resolved at call time by
# effective_model() so ops can switch Flash<->Pro without a code change. Flash is the default because
# the synchronous request was timing out on the slower Pro model (see settings.GEMINI_MODEL note).
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
)


def effective_model() -> str:
    """The model id actually used per call: settings.GEMINI_MODEL when set, else the GEMINI_MODEL
    default. Read by call_gemini (URL) and by extract.extract_rows (the returned engine label, so the
    FE 'which engine' badge + the training corpus record the real model)."""
    from django.conf import settings
    return getattr(settings, "GEMINI_MODEL", None) or GEMINI_MODEL


def build_prompt(aliases: list, team_notes: list, prompt_kind=None) -> str:
    # prompt_kind selects the prompt variant:
    #   None / "solo"      -> the existing event prompt (UNCHANGED, byte-for-byte).
    #   "team_standings"   -> additionally ask Gemini for a team_name + summed team kills per
    #                         placement (the standalone team-leaderboard flow, where we match the
    #                         TEAM name against the platform team pool, not individual players).
    # Read by call_gemini above (threaded from services.extract.extract_rows) when the standalone
    # team OCR endpoint runs; the event flow never passes "team_standings", so it is unaffected.
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

    # ── team_standings variant: ask for a team_name + summed team kills per placement ──
    # Appended AFTER the base block so the default prompt above is never mutated. We keep the same
    # placements[] shape (so the existing draft-row build still works) and ADD a "team_name" key
    # plus a "kills" total at the placement level. The standalone team flow reads team_name to
    # match against the platform team pool; players[] stays available for context.
    if prompt_kind == "team_standings":
        base += """
ADDITIONAL TEAM-STANDINGS INSTRUCTIONS:
This is a TEAM-STANDINGS read. For EACH placement, also report the team it belongs to:
- "team_name": the team's displayed name or tag (copy it EXACTLY), or omit it if no team name
  is visible for that placement.
- "kills": at the PLACEMENT level, the team's total eliminations (the sum of its players' kills).

Return the SAME structure with the two extra fields per placement:
{
  "match_type": "team",
  "placements": [
    {
      "placement": 1,
      "team_name": "TeamName",
      "kills": 4,
      "players": [
        {"name": "PlayerName", "kills": 3},
        {"name": "OtherPlayer", "kills": 1}
      ]
    }
  ]
}
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


def call_gemini(image_bytes: bytes, mime_type: str, aliases: list, team_notes: list, prompt_kind=None) -> dict:
    # prompt_kind selects the prompt variant (None/"solo" = the existing player prompt;
    # "team_standings" = additionally read a team_name per placement). It is threaded down from
    # services.extract.extract_rows so the standalone-leaderboard team flow can ask Gemini for a
    # team name; the event flow passes the default (None) and is unchanged.
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured in settings.")

    url = GEMINI_URL_TMPL.format(model=effective_model(), api_key=api_key)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": build_prompt(aliases, team_notes, prompt_kind=prompt_kind)},
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
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        # NEVER let the raw HTTPError propagate: requests embeds the full request URL in the
        # message, and ours carries ?key=<GEMINI_API_KEY>. That message gets persisted on OCR
        # job rows (afc_leaderboard.ocr.process_job stores str(exc) into job.error) and is then
        # rendered verbatim in the admin/organizer review dialog, so a raw raise would leak the
        # key into the UI. Re-raise a key-free message, keeping Gemini's own error detail.
        detail = ""
        try:
            detail = ((resp.json().get("error") or {}).get("message") or "")[:300]
        except Exception:
            pass
        raise RuntimeError(
            f"Gemini request failed with HTTP {resp.status_code}"
            + (f": {detail}" if detail else "")
        ) from None

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
