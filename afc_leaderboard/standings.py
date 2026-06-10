"""
afc_leaderboard.standings — compute a standalone leaderboard's standings ON READ.

PURPOSE
    Given a StandaloneLeaderboard, aggregate every participant's per-map results into a sorted
    standings table. Nothing is stored: standings are derived fresh from ParticipantMatchResult
    rows each time, so editing results (or adding a map) re-renders the table with no recalc job.

HOW IT CONNECTS
    - Consumed by afc_leaderboard.views.leaderboard_detail (GET leaderboards/standalone/<id>/) and
      surfaced on the FE view page (/leaderboards/standalone/<id>) + the wizard's Review step.
    - Reads ParticipantMatchResult columns that were COMPUTED on save by the views via
      afc_tournament_and_scrims.scoring.compute_team_points / compute_solo_points. This helper does
      NOT re-score — it only sums the stored point columns.
    - The sort key is the SAME chain the event standings builder uses
      (afc_tournament_and_scrims.views.get_all_leaderboard_details_for_event):
          (-effective_total, -booyahs, -total_kills, last_match_placement, name)
      where effective_total = placement_points + kill_points + bonus_points - penalty_points.

RETURN SHAPE
    [
      {
        "rank": 1,
        "participant": {"id", "name", "is_ghost", "kind"},
        "played_count": <maps with a played result>,
        "total_points": <effective total>,
        "kills": <summed kills>,
        "booyahs": <count of placement==1>,
        "per_match": [{"match_number", "placement", "kills", "total_points"}, ...]
      },
      ...
    ]
"""
from .models import LeaderboardParticipant


def standalone_standings(lb):
    """
    Build the sorted standings list for StandaloneLeaderboard `lb`.

    For each participant, sum placement_points / kill_points / bonus_points / penalty_points /
    total_points / kills across its ParticipantMatchResult rows, count booyahs (placement == 1),
    and record the last-match placement (for the tiebreak). Sort by the event-standings chain and
    assign 1-based ranks. Participants with zero results still appear (all-zero row), matching the
    event builder which lists every competitor.
    """
    # One query for participants, prefetch their results + the match each result belongs to so we
    # can read match_number without an extra query per row (avoids the N+1 the event Players page hit).
    participants = (
        LeaderboardParticipant.objects
        .filter(leaderboard=lb)
        .select_related("team", "ghost_team", "user", "ghost_player")
        .prefetch_related("results__match")
    )

    rows = []
    for p in participants:
        placement_sum = kill_sum = bonus_sum = penalty_sum = 0
        total_sum = kills_sum = booyahs = played_count = 0
        per_match = []
        # last_match_placement: placement in the highest match_number this participant played.
        # Default 999 so a participant with no results sorts LAST on this tiebreak (mirrors the
        # event builder's Coalesce(..., 999)).
        last_match_number = -1
        last_match_placement = 999

        for r in p.results.all():
            placement_sum += r.placement_points
            kill_sum += r.kill_points
            bonus_sum += r.bonus_points
            penalty_sum += r.penalty_points
            total_sum += r.total_points
            kills_sum += r.kills
            if r.placement == 1:
                booyahs += 1
            if r.played:
                played_count += 1
            mnum = r.match.match_number
            per_match.append({
                "match_number": mnum,
                "placement": r.placement,
                "kills": r.kills,
                "total_points": r.total_points,
            })
            if mnum > last_match_number:
                last_match_number = mnum
                last_match_placement = r.placement

        # effective_total mirrors the event builder: placement + kill + bonus - penalty.
        effective_total = placement_sum + kill_sum + bonus_sum - penalty_sum
        # Keep per_match in match order for a stable, readable breakdown.
        per_match.sort(key=lambda m: m["match_number"])

        rows.append({
            "participant": {
                "id": p.id,
                "name": p.display_name,
                "is_ghost": p.is_ghost,
                "kind": p.kind,
            },
            "played_count": played_count,
            "total_points": effective_total,
            "kills": kills_sum,
            "booyahs": booyahs,
            "per_match": per_match,
            # private sort fields (not emitted) ──
            "_last_placement": last_match_placement,
            "_name": p.display_name or "",
        })

    # Sort by the exact event-standings chain: highest effective total, then most booyahs, then most
    # kills, then best (lowest) last-match placement, then name (stable, alphabetical).
    rows.sort(key=lambda r: (
        -r["total_points"],
        -r["booyahs"],
        -r["kills"],
        r["_last_placement"],
        r["_name"],
    ))

    # Assign 1-based ranks and drop the private sort fields.
    standings = []
    for i, r in enumerate(rows, start=1):
        standings.append({
            "rank": i,
            "participant": r["participant"],
            "played_count": r["played_count"],
            "total_points": r["total_points"],
            "kills": r["kills"],
            "booyahs": r["booyahs"],
            "per_match": r["per_match"],
        })
    return standings
