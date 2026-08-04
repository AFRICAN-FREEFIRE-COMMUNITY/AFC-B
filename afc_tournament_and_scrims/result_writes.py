"""
THE one place a team's per-map result becomes stats rows.

WHY THIS MODULE EXISTS
    Before it, three write paths each built TournamentTeamMatchStats and
    TournamentPlayerMatchStats by hand: the manual entry endpoint
    (views.enter_team_match_result_manual), the match-log file upload
    (views.upload_team_match_result) and the OCR commit (afc_ocr.services.commit). They
    agreed on the scoring formula, because that already lives in scoring.compute_team_points,
    but each repeated the surrounding write: which rows to clear first, how to sum a team's
    kills from its played players, and what to stamp on the player rows.

    Team result submissions (owner 2026-08-04, backlog item 6: "teams should be able to
    upload their own per-map results, and the organizer approves them") would have been a
    FOURTH copy. A fourth copy is how standings start disagreeing with themselves: an
    approved submission has to produce exactly the rows an organizer's own entry produces,
    or the same map scores differently depending on who typed it.

    Four doors, one write. Nothing about a bonus, a penalty, a zero-kill row or an unnamed
    player slot is decided in four places any more, so the standings cannot come to depend
    on which door a result arrived through. Each door proves it with a test that scores the
    same map twice and compares every stored column: tests_team_submissions
    .test_approved_result_matches_manual_entry, tests_log_attribution
    .test_log_upload_matches_manual_entry, and afc_ocr.tests.test_commit_matches_manual.

    WHAT EACH DOOR STILL OWNS is everything ABOVE the write, because that part genuinely
    differs: the log upload decides which in-game block is which registered team and which
    UIDs are ringers, the OCR commit decides the same from names, and manual entry is simply
    told. They hand this function a team, a placement and a list of players, and it decides
    the rest.

WHY THE UNIT IS ONE TEAM AND NOT ONE MAP
    Manual entry knows the whole lobby at once and clears the match before rewriting it.
    An approval knows exactly ONE team's row and must leave the other teams alone, because
    the organizer approves each team's submission as it arrives. A per-team writer serves
    both: the manual path clears the match first and then calls this once per team, the
    approval path calls it once and touches nothing else.

THE ROLE STAMP
    Every player row carries role_at_match, copied from the FROZEN per-event roster row via
    roster_roles. Never from the live club roster: this function deletes and re-inserts a
    team's rows on every re-write, so a live read would let a September correction rewrite
    July's roles. See afc_tournament_and_scrims/roster_roles.py, which explains the rule in
    full, and TournamentPlayerMatchStats.role_at_match.

CALLERS
    * views.enter_team_match_result_manual  - organizer or admin types the whole lobby.
    * views_team_submissions.approve_team_map_submission - organizer approves one team's
      own submission of its own row.
    * views.upload_team_match_result - a match-log file, one call per resolved team block.
      The flagged kills that currently count arrive as an entry with no user_id, because a
      flagged kill is part of the team's score with nobody on this roster to attribute it to.
    * afc_ocr.services.commit.commit_team_result - a committed screenshot review, one call
      per placement group, with its ringers folded into the same unnamed entry.

    NOT A CALLER, on purpose: views.upload_match_result_image, the legacy screenshot endpoint.
    It attaches a player's row to the team the player is REGISTERED to rather than the block
    they were read from, which a per-team writer cannot reproduce. The full reason is written
    at its write site.
"""
from . import scoring as scoring_lib
from .models import TournamentPlayerMatchStats, TournamentTeamMatchStats


def scoring_context(match):
    """Pull the four scoring settings off a match, normalised, or raise ValueError.

    Every caller needs the same four values in the same shapes, and the placement table
    arrives as a JSON object with string keys that has to become int -> int before
    compute_team_points can index it by placement. Doing that once here keeps a caller from
    quietly getting it wrong (a string key silently scores every placement as zero).

    Raises ValueError with a message fit to return to an API caller when the stored settings
    are malformed, which is the same 400 the manual endpoint already produced.
    """
    scoring = match.scoring_settings or {}
    try:
        placement_points = {
            int(k): int(v) for k, v in (scoring.get("placement_points") or {}).items()
        }
    except Exception:
        raise ValueError("Invalid match scoring placement_points.")

    return {
        "placement_points": placement_points,
        "kill_point": float(scoring.get("kill_point", 1)),
        "points_per_assist": float(scoring.get("points_per_assist", 0)),
        "points_per_1000_damage": float(scoring.get("points_per_1000_damage", 0)),
    }


def normalise_players(players, team_played):
    """Clean one team's player list into the rows this module will store.

    Accepts the shape every caller already speaks: a list of
    {user_id, kills, damage, assists, played}.

    AN ENTRY WITH NO user_id IS KEPT, with user_id None, and that is load bearing. The manual
    entry form lets an organizer type a team's kills against an unnamed slot, so those kills
    are part of the TEAM total even though there is no player to attach a per-player row to.
    Dropping them here silently scored those teams at zero (caught by
    tests_scoring.test_manual_team_entry_stores_canonical_total). The team sum below counts
    every entry; only the per-player rows are limited to entries that name a player.

    A team marked not played forces every player to played=False, which is what zeroes their
    contribution. That rule lived in the manual endpoint and moves here so the approval path
    cannot forget it.
    """
    rows = []
    for player in players or []:
        user_id = player.get("user_id")
        played = bool(player.get("played", True)) and team_played
        rows.append({
            "user_id": int(user_id) if user_id else None,
            "played": played,
            # A player who did not play contributes nothing, whatever the form sent. Zeroing
            # here rather than at read time keeps the stored row honest on its own.
            "kills": int(player.get("kills") or 0) if played else 0,
            "damage": int(player.get("damage") or 0) if played else 0,
            "assists": int(player.get("assists") or 0) if played else 0,
        })
    return rows


def write_team_result_row(*, match, tournament_team_id, row, ctx, frozen_roles):
    """Write ONE team's result for ONE map. Replaces whatever that team had for this match.

    Args:
        match:              the Match being scored.
        tournament_team_id: the TournamentTeam this row belongs to.
        row:                {placement, played, bonus_points, penalty_points, players[]},
                            the same shape the manual entry form posts per team.
        ctx:                the dict from scoring_context(match).
        frozen_roles:       {user_id: in_game_role} from
                            roster_roles.frozen_roles_for_match(match), passed in rather
                            than fetched here so a caller writing twenty teams issues one
                            query instead of twenty.

    Returns the created TournamentTeamMatchStats.

    IDEMPOTENT: the team's existing stats row for this match is deleted first, which cascades
    to its player rows. Calling this twice with the same input leaves the same single row,
    which is what makes a re-approval or a corrected re-entry safe.
    """
    team_played = bool(row.get("played", True))
    placement = int(row.get("placement") or 0) if team_played else 0
    bonus = int(row.get("bonus_points") or 0)
    penalty = int(row.get("penalty_points") or 0)

    players = normalise_players(row.get("players"), team_played)
    played_players = [p for p in players if p["played"]]

    total_kills = sum(p["kills"] for p in played_players)
    total_damage = sum(p["damage"] for p in played_players)
    total_assists = sum(p["assists"] for p in played_players)

    points = scoring_lib.compute_team_points(
        placement_points=ctx["placement_points"],
        kill_point=ctx["kill_point"],
        points_per_assist=ctx["points_per_assist"],
        points_per_1000_damage=ctx["points_per_1000_damage"],
        placement=placement,
        kills=total_kills,
        damage=total_damage,
        assists=total_assists,
        bonus=bonus,
        penalty=penalty,
        played=team_played,
    )

    # Clear this team's row for this match before writing the new one. The unique constraint
    # (match, tournament_team) would otherwise reject the insert, and the delete cascades to
    # the player rows so no orphan survives a correction that dropped a player.
    TournamentTeamMatchStats.objects.filter(
        match=match, tournament_team_id=tournament_team_id).delete()

    team_stats = TournamentTeamMatchStats.objects.create(
        match=match,
        tournament_team_id=tournament_team_id,
        placement=placement,
        kills=total_kills,
        damage=total_damage,
        assists=total_assists,
        placement_points=points["placement_points"],
        kill_points=points["kill_points"],
        bonus_points=bonus,
        penalty_points=penalty,
        total_points=points["total_points"],
        played=team_played,
    )

    # Only entries that name a player become per-player rows. An unnamed slot has already
    # contributed its kills to the team total above; there is nobody to attribute them to.
    TournamentPlayerMatchStats.objects.bulk_create([
        TournamentPlayerMatchStats(
            team_stats=team_stats,
            player_id=p["user_id"],
            kills=p["kills"],
            damage=p["damage"],
            assists=p["assists"],
            played=p["played"],
            # None when this event's roster has no role for them (staff, or a roster row
            # written before the field existed). Left None rather than guessed.
            role_at_match=frozen_roles.get(p["user_id"]),
        )
        for p in players if p["user_id"] is not None
    ], batch_size=500)

    return team_stats
