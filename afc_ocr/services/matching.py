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
