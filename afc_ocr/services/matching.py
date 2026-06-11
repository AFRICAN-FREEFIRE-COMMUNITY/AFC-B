import uuid
from collections import Counter

CONFIDENCE_AUTO = 0.85
CONFIDENCE_WARN = 0.75


def get_registered_players(match, event, event_type: str) -> list:
    """
    Returns all players registered in the event.
    Format: [{"user_id", "username", "team_id", "team_name"}]
    """
    from afc_tournament_and_scrims.models import TournamentTeam, RegisteredCompetitors

    players = []

    if event_type == "team":
        teams = (
            TournamentTeam.objects
            .filter(event=event, status="active")
            .select_related("team")
            .prefetch_related("members__user")
        )
        for t_team in teams:
            for member in t_team.members.filter(status__in=["active", "approved"]):
                players.append({
                    "user_id":   member.user_id,
                    "username":  member.user.username,
                    "team_id":   t_team.tournament_team_id,
                    "team_name": t_team.team.team_name,
                })
    else:
        competitors = (
            RegisteredCompetitors.objects
            .filter(event=event, status__in=["registered", "approved"])
            .select_related("user")
        )
        for comp in competitors:
            if comp.user:
                players.append({
                    "user_id":   comp.user_id,
                    "username":  comp.user.username,
                    "team_id":   None,
                    "team_name": None,
                })

    return players


# ──────────────────────────────────────────────────────────────────────────────
# P2: platform-wide candidate pools + a team-name matcher (the standalone-leaderboard
# OCR assist). Unlike get_registered_players / match_name (event flow, roster-gated),
# these match against EVERY team / user on the platform, because a standalone leaderboard
# has no event roster to gate against. Consumed by afc_leaderboard.views.ocr_extract:
#   - team-format LB  -> all_platform_teams() + match_team_name()
#   - solo-format LB  -> all_platform_players() + match_name() (reused as-is)
# match_name (above) is reused unchanged for the solo flow.
# ──────────────────────────────────────────────────────────────────────────────


def all_platform_players(limit=None) -> list:
    """Every registered user as a match candidate (NO roster gate), shaped like
    get_registered_players' rows so match_name can consume the list unchanged.

    Format: [{"user_id", "username", "team_id": None, "team_name": None}]. team_id/team_name are
    always None here: a standalone solo leaderboard carries no team context (a real-user solo
    participant resolves by user_id alone). `limit` caps the pool (paginate huge member bases).
    Read by afc_leaderboard.views.ocr_extract for the solo flow."""
    from afc_auth.models import User

    qs = User.objects.all().order_by("user_id").values("user_id", "username")
    if limit is not None:
        qs = qs[:limit]
    return [
        {"user_id": u["user_id"], "username": u["username"], "team_id": None, "team_name": None}
        for u in qs
    ]


def all_platform_teams() -> list:
    """Every real Team as a match candidate for the standalone team flow.

    Format: [{"team_id", "team_name", "team_tag"}]. Read by afc_leaderboard.views.ocr_extract
    (team format) and fed to match_team_name below. No roster gate: a standalone leaderboard can
    feature any team on the platform."""
    from afc_team.models import Team

    return [
        {"team_id": t.team_id, "team_name": t.team_name, "team_tag": t.team_tag}
        for t in Team.objects.all().order_by("team_id")
    ]


def derive_team_tag(player_names: list) -> str:
    """Best-effort TEAM TAG from a placement's player names (owner 2026-06-11: "these team tags can help
    when searching for teams through the tags on the players names").

    Free Fire players almost always prefix their team tag onto their IGN (e.g. "AE.John", "AE乂Mike",
    "ᴀᴇMike"). When the team-name read is blank or weak, the tag shared across a placement's players is a
    strong second signal for which team it is. We take the leading ALPHANUMERIC run of each player name
    and return the longest common prefix when it is a plausible tag (2-5 chars) shared by >=2 players,
    else "". Consumed by afc_leaderboard.ocr.build_team_ocr_rows, which matches the derived tag against
    each team's team_tag via match_team_name (whose scorer already compares the tag).
    """
    import os
    import re

    leads = []
    for n in player_names or []:
        m = re.match(r"\s*([A-Za-z0-9]{1,8})", n or "")
        if m:
            leads.append(m.group(1))
    if len(leads) < 2:
        return ""
    prefix = re.sub(r"[^A-Za-z0-9]", "", os.path.commonprefix(leads))
    return prefix if 2 <= len(prefix) <= 5 else ""


def match_team_name(raw_name: str, teams: list) -> dict:
    """The team-format mirror of match_name: fuzzy-match a raw OCR-read team name against the
    platform team pool (from all_platform_teams), returning the best match + top-3 candidates.

    Scoring: rapidfuzz WRatio (same scorer/cutoff/limit as match_name) over BOTH the team_name
    and the team_tag of each team; a team's score is the better of its name-score and tag-score,
    so a screenshot showing only the short tag ("ALP") still resolves. Cutoff 40, top-3.

    Returns {row_id, raw_name, matched_team_id, matched_team_name, confidence,
             top_candidates:[{team_id, team_name, confidence}]}. No match -> matched_team_id None,
             matched_team_name None, confidence 0.0, top_candidates []. Consumed by ocr_extract;
             the FE review table renders top_candidates as the per-row dropdown."""
    row_id = str(uuid.uuid4())

    empty = {
        "row_id":            row_id,
        "raw_name":          raw_name,
        "matched_team_id":   None,
        "matched_team_name": None,
        "confidence":        0.0,
        "top_candidates":    [],
    }
    if not teams:
        return empty

    try:
        from rapidfuzz import fuzz
    except ImportError:
        return empty

    # Score every team by the better of its name-score and tag-score, then keep the top-5 above the
    # cutoff. We score in Python (not process.extract) because each team has TWO strings (name + tag)
    # to compare and we want the max of the two as that team's confidence. Cutoff 30 + top-5 (owner
    # 2026-06-11: "if there are similar names on the platform, it lists them") — a deliberately LOOSE
    # net so the admin always sees the closest names to pick from, even on a rough read. The best
    # match still drives auto-resolve via the confidence ladder on the FE; the extra candidates are
    # just there to pick.
    scored = []
    for t in teams:
        name_score = fuzz.WRatio(raw_name, t["team_name"]) if t.get("team_name") else 0.0
        tag_score = fuzz.WRatio(raw_name, t["team_tag"]) if t.get("team_tag") else 0.0
        score = max(name_score, tag_score)
        if score >= 30:
            scored.append((score, t))

    if not scored:
        return empty

    scored.sort(key=lambda s: s[0], reverse=True)
    top_candidates = [
        {"team_id": t["team_id"], "team_name": t["team_name"], "confidence": round(score / 100, 3)}
        for score, t in scored[:5]
    ]

    best = top_candidates[0]
    return {
        "row_id":            row_id,
        "raw_name":          raw_name,
        "matched_team_id":   best["team_id"],
        "matched_team_name": best["team_name"],
        "confidence":        best["confidence"],
        "top_candidates":    top_candidates,
    }


def match_name(raw_name: str, registered: list) -> dict:
    """
    1. Check OCRNameAlias for an exact match (confidence = 1.0).
    2. Fall back to rapidfuzz against registered usernames.
    3. Return top 3 candidates plus the best match row.
    """
    from afc_ocr.models import OCRNameAlias

    row_id = str(uuid.uuid4())

    # Step 1: exact alias lookup
    alias = (
        OCRNameAlias.objects
        .filter(raw_name__iexact=raw_name)
        .select_related("user")
        .first()
    )
    if alias and alias.user:
        reg = next((p for p in registered if p["user_id"] == alias.user_id), None)
        return {
            "row_id":            row_id,
            "raw_name":          raw_name,
            "matched_user_id":   alias.user_id,
            "matched_username":  alias.user.username,
            "confidence":        1.0,
            "matched_team_id":   reg["team_id"]   if reg else None,
            "matched_team_name": reg["team_name"] if reg else None,
            "top_candidates":    [],
        }

    # Step 2: rapidfuzz
    try:
        from rapidfuzz import process, fuzz

        usernames = [p["username"] for p in registered]
        results = process.extract(
            raw_name, usernames,
            scorer=fuzz.WRatio,
            limit=3,
            score_cutoff=40,
        )
    except ImportError:
        results = []

    if not results:
        return {
            "row_id":            row_id,
            "raw_name":          raw_name,
            "matched_user_id":   None,
            "matched_username":  None,
            "confidence":        0.0,
            "matched_team_id":   None,
            "matched_team_name": None,
            "top_candidates":    [],
        }

    top_candidates = []
    for username, score, _ in results:
        player = next((p for p in registered if p["username"] == username), None)
        if player:
            top_candidates.append({
                "user_id":    player["user_id"],
                "username":   username,
                "confidence": round(score / 100, 3),
            })

    if not top_candidates:
        return {
            "row_id":            row_id,
            "raw_name":          raw_name,
            "matched_user_id":   None,
            "matched_username":  None,
            "confidence":        0.0,
            "matched_team_id":   None,
            "matched_team_name": None,
            "top_candidates":    [],
        }

    best = top_candidates[0]
    player = next((p for p in registered if p["user_id"] == best["user_id"]), {})

    return {
        "row_id":            row_id,
        "raw_name":          raw_name,
        "matched_user_id":   best["user_id"],
        "matched_username":  best["username"],
        "confidence":        best["confidence"],
        "matched_team_id":   player.get("team_id"),
        "matched_team_name": player.get("team_name"),
        "top_candidates":    top_candidates,
    }


def detect_team_mismatches(draft_rows: list) -> list:
    """
    For each placement group, the players should all be on the same
    registered tournament team. If not, flag team_mismatch = True.

    Logic:
    - Group rows by placement number.
    - For each group, find the most common matched_team_id (majority vote).
    - Any player whose matched_team_id differs is flagged.
    """
    groups: dict = {}
    for row in draft_rows:
        p = row.get("placement", 0)
        groups.setdefault(p, []).append(row)

    result = []
    for placement, rows in groups.items():
        team_ids = [r.get("matched_team_id") for r in rows if r.get("matched_team_id")]

        if not team_ids:
            for row in rows:
                row["team_mismatch"]       = True
                row["admin_confirmed_sub"] = False
                row["expected_team_id"]    = None
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
