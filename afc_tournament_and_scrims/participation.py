"""
afc_tournament_and_scrims/participation.py
──────────────────────────────────────────
ONE canonical answer to the question "did this player actually PLAY in this event?",
so every surface that counts a player's tournaments/scrims counts the SAME rows.

WHY this module exists (owner bug 2026-08-07):
"Tournaments played" was computed by counting RegisteredCompetitors / TournamentTeam
rows with NO status check at all. A registration that was disqualified, a roster slot
that was rejected, a team that withdrew, and a competitor who sat on the waitlist and
never got a slot all counted exactly the same as someone who turned up and played.

WHAT CHANGED (owner ruling 2026-08-08), and it is a change of MEANING, not a bug fix:
  "It should count events played. Matches they participated in where a score was
   assigned to them."
So the first version of this module - which counted a REAL SLOT (accepted registration,
active team, not waitlisted, not marked absent) - was still counting the wrong thing. A
slot is permission to play. It is not play. The number now counts EVIDENCE OF PLAY:

  An event counts for a player when that player has at least one MATCH LINE in it that
  a score was written to:
    • squad path: a TournamentPlayerMatchStats row  (player was on the sheet for a match)
    • solo path:  a SoloPlayerMatchStats row        (via their RegisteredCompetitors row)
    • the row must be marked played=True            (see the `played` note below)
    • the event itself must be real                 (not an organizer's unpublished draft)

THE THREE QUESTIONS THIS RULE HAD TO ANSWER, answered here once so no call site re-guesses:

  1. A player fielded in a match that was later voided, or whose team was disqualified:
     COUNTS. They played, and a score was assigned to them. A disqualification is a
     sanction on where a team FINISHES; it does not un-play the matches. This is the one
     place the new rule is deliberately LOOSER than the old slot rule: a fielded player on
     a team that was later disqualified, withdrew, or was marked a no-show now keeps the
     event on their "played" total, because they were there. Their standings, points and
     prize money are governed elsewhere and are untouched by this number.

  2. A scored line with 0 kills and 0 points: COUNTS. Zero is a real score, and in the
     live database it is the MAJORITY shape of a quiet match (1003 of 2982 squad lines and
     360 of 1019 solo lines carry zero kills). The gate below is therefore the `played`
     BOOLEAN and nothing else. There is NO filter on kills, damage, assists, placement or
     total_points anywhere in this module, and none must ever be added: a truthiness test
     on a score would silently delete a third of the platform's real play. (This repo has
     shipped three separate falsy-zero bugs, one of them in exactly this area.)

  3. Solo events, whose shape differs: same rule, different table. A solo competitor has
     no team and no roster, so their match line is a SoloPlayerMatchStats row hanging off
     their RegisteredCompetitors row, which is what carries the event. Both paths are
     unioned into one set of event ids, so a player who entered the same event both ways
     is counted once.

`played` is the flag the result-write path already uses to mean "this competitor was on
the sheet for this match": result_writes.write_team_result forces played=False for a
player the organizer marked as not fielded, and views.create_leaderboard pre-seeds a
played=False placeholder row for EVERY rostered member of every registered team so the
manual score-entry grid has something to type into. Those placeholders are precisely the
"rostered but never fielded" case the owner is excluding, so filtering on played=True is
load-bearing even though today's database happens to hold none of them.

WHO USES THIS (both sides of the same question, previously written twice and differently):
  • afc_auth.views.get_user_profile              -> "Tournaments" / "Scrims" on the owner's
                                                    own profile (frontend ProfileContent.tsx)
  • afc_player.aggregation.compute_player_stats  -> tournaments_played / scrims_played in the
                                                    shared stat block, served by BOTH
                                                    afc_player.views.get_public_player_stats
                                                    (public player page, PlayerClient.tsx) and
                                                    get_player_details (admin player detail)

Both go through played_event_counts(user), so the two pages cannot disagree again.

NOT the same question, and deliberately still slot-shaped: counted_tournament_team_ids
below. See its section header.
"""
from afc_tournament_and_scrims.models import (
    Event,
    RegisteredCompetitors,
    SoloPlayerMatchStats,
    TournamentPlayerMatchStats,
    TournamentTeamMember,
)


# ══════════════════════════════════════════════════════════════════════════════════════
# §1  PLAYED - the owner's rule. Evidence of play, not permission to play.
# ══════════════════════════════════════════════════════════════════════════════════════

def scored_squad_match_lines(user):
    """
    The TournamentPlayerMatchStats rows (SQUAD path) that prove `user` played.

    Returns a queryset so callers can .values_list(...) without pulling rows into memory.
    """
    return (
        TournamentPlayerMatchStats.objects
        # played=True is the ONLY score-side gate. Never add a kills/points filter here:
        # a zero-kill line is a played line (see question 2 in the module docstring).
        .filter(player=user, played=True)
        # A draft event is not a real event; it is an organizer's unpublished sketch.
        .exclude(team_stats__tournament_team__event__is_draft=True)
    )


def scored_solo_match_lines(user):
    """
    The SoloPlayerMatchStats rows (SOLO path) that prove `user` played.

    A solo competitor has no TournamentTeam, so the row reaches its event through the
    RegisteredCompetitors row it belongs to. RegisteredCompetitors.user is nullable
    (sponsor-imported entries can be user-less), and filtering on `competitor__user=user`
    naturally skips those.
    """
    return (
        SoloPlayerMatchStats.objects
        .filter(competitor__user=user, played=True)
        .exclude(competitor__event__is_draft=True)
    )


def counted_event_ids(user):
    """
    Every Event id `user` genuinely PLAYED IN, across BOTH entry paths, deduped.

    The event of a squad line is taken from team_stats.tournament_team.event, NOT from
    match.leaderboard.event: Match.leaderboard is nullable, and in the live database 230
    scored lines (one whole event) hang off matches with no leaderboard, so the
    leaderboard path silently loses them. tournament_team.event is a non-null FK and the
    two never disagree where both exist (checked: 0 mismatching rows).

    Returns a SET, so entering one event both solo and as a squad counts once.
    """
    squad_event_ids = scored_squad_match_lines(user).values_list(
        "team_stats__tournament_team__event_id", flat=True
    )
    solo_event_ids = scored_solo_match_lines(user).values_list(
        "competitor__event_id", flat=True
    )
    return set(squad_event_ids) | set(solo_event_ids)


def played_event_counts(user):
    """
    (tournaments_played, scrims_played) for `user` - the two numbers every surface shows.

    Splits counted_event_ids by Event.competition_type ("tournament" / "scrims"). This is
    THE function the call sites use; counted_event_ids is exported beside it only because
    the tests and any future per-event surface want the ids themselves.

    Consumed by afc_auth.views.get_user_profile (own profile) and
    afc_player.aggregation.compute_player_stats (public player page + admin player
    detail), which is what keeps those surfaces reporting the same number for the same
    human being.
    """
    competition_types = (
        Event.objects
        .filter(event_id__in=counted_event_ids(user))
        .values_list("competition_type", flat=True)
    )
    tournaments = sum(1 for kind in competition_types if kind == "tournament")
    scrims = sum(1 for kind in competition_types if kind == "scrims")
    return tournaments, scrims


# ══════════════════════════════════════════════════════════════════════════════════════
# §2  ROSTERED - a DIFFERENT question, kept slot-shaped on purpose.
#
# "Which tournament-teams' records legitimately belong to this player?" is not "which
# events did this player play". The TEAM record on the player profile (team_matches /
# team_wins / team_win_rate) is explicitly the record of the TEAMS a player was part of,
# every match, played or not - so it is answered from the ROSTER, gated on the roster's
# own statuses. Re-pointing it at scored match lines would collapse it into the player's
# personal record, which is the number sitting right beside it.
#
# What this still throws out: a REJECTED or pending roster slot, and a slot on a team that
# was disqualified, withdrew, left, sat on the waitlist, or was marked absent. None of
# those teams' results were ever this player's to inherit.
# ══════════════════════════════════════════════════════════════════════════════════════

# Status allow-lists (allow-list, NOT deny-list: a status added later must be consciously
# opted IN rather than silently counting).

# RegisteredCompetitors.STATUS_CHOICES: registered/disqualified/withdrawn/left/pending/approved/rejected.
# "registered" and "approved" are the two that mean "you are in". "pending" has not been accepted yet.
COUNTED_COMPETITOR_STATUSES = frozenset({"registered", "approved"})

# TournamentTeamMember.TEAM_MEMBER_STATUS: pending/active/rejected/approved.
COUNTED_TEAM_MEMBER_STATUSES = frozenset({"active", "approved"})

# TournamentTeam.TEAM_STATUS: active/disqualified/withdrawn/left.
COUNTED_TEAM_STATUSES = frozenset({"active"})


def counted_solo_registrations(user):
    """
    The RegisteredCompetitors rows (SOLO entry path) where `user` held a real slot.

    Kept as the slot-shaped counterpart of scored_solo_match_lines above, and used by the
    tests that pin the roster rule. Not part of the tournaments-played count any more.
    """
    return (
        RegisteredCompetitors.objects
        .filter(user=user, status__in=COUNTED_COMPETITOR_STATUSES)
        # Waitlisted = queued, never given a slot. No-show = had a slot, did not turn up.
        .filter(is_waitlisted=False, is_no_show=False)
        .exclude(event__is_draft=True)
    )


def counted_team_memberships(user):
    """
    The TournamentTeamMember rows (SQUAD entry path) where `user` held a real slot. Gates
    on BOTH the member's own status and the team's: a rejected player on an active team
    did not hold a slot, and neither did any member of a disqualified team.
    """
    return (
        TournamentTeamMember.objects
        .filter(user=user, status__in=COUNTED_TEAM_MEMBER_STATUSES)
        .filter(tournament_team__status__in=COUNTED_TEAM_STATUSES)
        .filter(tournament_team__is_waitlisted=False, tournament_team__is_no_show=False)
        .exclude(tournament_team__event__is_draft=True)
    )


def counted_tournament_team_ids(user):
    """
    The TournamentTeam ids whose record legitimately belongs to `user`.

    Consumed by afc_player.aggregation.compute_player_stats to build the player's TEAM
    record (team_matches / team_wins / team_win_rate). Using this instead of a bare
    TournamentTeamMember.filter(user=...) is what stops a rejected roster slot from
    lending a player the wins of a team that never fielded them.
    """
    return list(
        counted_team_memberships(user)
        .values_list("tournament_team_id", flat=True)
        .distinct()
    )
