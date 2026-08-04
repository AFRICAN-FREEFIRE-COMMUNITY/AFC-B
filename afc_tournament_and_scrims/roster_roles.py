"""
Single source of truth for "which in-game role did this player hold when this match was played".

WHY THIS MODULE EXISTS
    The per-role player ladders used to filter on afc_team.TeamMembers.in_game_role, which is the
    role the player holds RIGHT NOW. A player who was a sniper in July and is a rusher today was
    listed under rusher in July's sniper table, so the table described the present rather than the
    period it claimed to describe (owner 2026-08-04: "role history is not stored ... fix the above
    so it records properly using data and is stored").

    Storing the role means copying it at two moments, and this module owns the second one:

      1. ROSTER-LOCK. TournamentTeamMember.in_game_role freezes the role when the player is put on
         an EVENT roster. AFC already freezes rosters per event, so that row is the natural place to
         freeze the role with it. Written directly at the roster write sites (views.register_for_event,
         add_teams_to_event, edit_roster, add_player_to_event_roster, event_links promotion/import).

      2. RESULT-RECORDING. TournamentPlayerMatchStats.role_at_match copies that frozen value onto
         the per-match stats row when a result is recorded. This is the precise anchor the ranking
         aggregation reads, because a period's score is built from exactly those rows.

THE RULE THAT KEEPS STEP 2 HONEST, and why the live roster is never consulted here
    Every match-result write path DELETES a match's player-stat rows and re-inserts them on each
    (idempotent) re-upload. If the stamp were read from the live TeamMembers row, re-uploading a July
    match in September would rewrite July's roles with September's, reintroducing the exact bug being
    fixed. Reading the FROZEN per-event value instead makes the stamp reproducible: re-uploading an
    old match reproduces the old role, forever.

    So the resolution has exactly one source and no fallback chain. A missing frozen role yields
    None, and None is written as None. It is never filled in from somewhere more convenient.

WHAT NULL MEANS (all real, none of them a failure)
    * staff - coach, manager and analyst hold no in_game_role at all;
    * a player whose event roster row predates the frozen field (see the backfill command,
      afc_rankings/management/commands/backfill_player_roles.py, which only stamps what it can
      defend and leaves finished events empty on purpose);
    * a roster row copied from a source event that itself had no role recorded;
    * a player the write path could not resolve to a roster row (an OCR ringer, a name match).

CONNECTS TO
    Callers (the stamp): afc_tournament_and_scrims.views - upload_team_match_result, the manual
    result-entry and result-edit endpoints - and afc_ocr.services.commit.commit_review.
    Consumer (the read): afc_rankings.aggregation._collect_player turns a period's role_at_match
    stamps into PlayerMonthlyScore.role / role_breakdown (and the quarterly pair), which is what
    afc_rankings.player_roles.players_by_role filters the public role ladders on.
"""
from afc_team.models import TeamMembers

from .models import TournamentTeamMember


def frozen_roles_for_event(event_id):
    """``{user_id: in_game_role}`` for every player on any roster of ``event_id``.

    One query for the whole event, returned as a dict, because the callers are bulk writers that
    stamp a few hundred player rows at once and must not issue a query per player.

    Only rows with a role recorded are included, so ``.get(user_id)`` naturally yields None for
    everyone else and the caller writes None without a branch. Filtering on
    ``tournament_team__event_id`` rather than the row's own nullable ``event`` FK is deliberate:
    ``TournamentTeamMember.event`` is null on some historical rows, while the parent
    ``TournamentTeam.event`` is always set.

    A player cannot hold two roster rows in one event (TournamentTeam has a unique (event, team)
    constraint and TournamentTeamMember a unique (tournament_team, user)), except in the rare case
    of the same person registered with two different teams in one event, which registration blocks.
    If it ever happened, the last row read would win rather than the query crashing.
    """
    if not event_id:
        return {}
    pairs = (TournamentTeamMember.objects
             .filter(tournament_team__event_id=event_id)
             .exclude(in_game_role=None)
             .exclude(in_game_role="")
             .values_list("user_id", "in_game_role"))
    return {user_id: role for user_id, role in pairs}


def frozen_roles_for_match(match):
    """``{user_id: in_game_role}`` for the event ``match`` belongs to, ``{}`` when it has none.

    The convenience wrapper the result-recording paths actually call, because a match-result endpoint
    has the Match in hand and does not always have the Event. Resolves the event the same way the
    ranking aggregation does (``afc_rankings.aggregation._event_of_match``): group -> stage -> event,
    falling back to leaderboard -> event. Keeping the two resolutions identical matters, because a
    match whose event this cannot find is also a match the aggregation ignores, so an unstamped row
    there costs nothing.
    """
    group = getattr(match, "group", None)
    if group and getattr(group, "stage", None):
        return frozen_roles_for_event(group.stage.event_id)
    leaderboard = getattr(match, "leaderboard", None)
    if leaderboard:
        return frozen_roles_for_event(leaderboard.event_id)
    return {}


def carried_roster(source_event, team):
    """``[(user_id, in_game_role), ...]`` to copy onto a NEW event roster, deduped, order preserved.

    Used by the two roster-CARRY paths in ``event_links``: a qualification promotion (``_promote``)
    and an event merge (``import_competitors``). Both build the target roster from the SOURCE event's
    finishing roster when it has one, and fall back to the team's current club members when it does
    not, so this helper reproduces exactly that rule with the role attached.

    Which role gets carried, and why it differs per branch:
      * from the SOURCE EVENT - the role FROZEN on that event's roster row. A promotion carries the
        squad that just qualified, so it must carry the roles they qualified under, not whatever the
        club roster says today. This is the whole point of freezing the value per event.
      * from the CLUB roster (fallback, source event had no roster rows) - the live
        ``TeamMembers.in_game_role``. That is honest here: with no source roster there is nothing
        historical to carry, and the target event has not been played yet, so the current role is
        the role the player is about to play under.

    A None role is carried as None rather than being filled in from the club roster, because a blank
    on the source roster means the source did not know either.
    """
    src = list(
        TournamentTeamMember.objects
        .filter(tournament_team__event=source_event, tournament_team__team=team)
        .values_list("user_id", "in_game_role")
    ) or list(
        TeamMembers.objects.filter(team=team).values_list("member_id", "in_game_role")
    )
    # dict() dedupes on user_id and keeps insertion order (Python 3.7+), matching the
    # dict.fromkeys() dedupe this replaced. A later row for the same user wins, as it did before.
    return list(dict(src).items())
