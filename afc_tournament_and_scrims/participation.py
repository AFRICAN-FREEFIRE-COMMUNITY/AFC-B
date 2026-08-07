"""
afc_tournament_and_scrims/participation.py
──────────────────────────────────────────
ONE canonical answer to the question "did this competitor actually take part in this
event?", so every surface that counts a player's tournaments/scrims counts the SAME
rows.

WHY this module exists (owner bug 2026-08-07):
"Tournaments played" was computed by counting RegisteredCompetitors / TournamentTeam
rows with NO status check at all. A registration that was disqualified, a roster slot
that was rejected, a team that withdrew, and a competitor who sat on the waitlist and
never got a slot all counted exactly the same as someone who turned up and played. In
the live database that was 1 disqualified registration, 39 rejected + 3 pending roster
slots, 9 non-active teams, 25 waitlisted entries and 8 no-shows all inflating players'
profile numbers.

The rule, stated once here rather than re-guessed at each call site:

  An entry counts when the competitor HELD A REAL SLOT in a REAL event.
    • the entry's own status says it was accepted     (not rejected/pending/disqualified/withdrawn/left)
    • for a squad, the TEAM's status says the same    (a disqualified team's members did not play)
    • the entry was not stuck on the waitlist         (is_waitlisted -> never got a slot)
    • the competitor was not marked absent           (is_no_show -> had a slot, did not turn up)
    • the event itself is real                        (not a draft)

WHO USES THIS (both sides of the same question, previously written twice and differently):
  • afc_auth.views.get_user_profile        -> "Tournaments" / "Scrims" tiles on the owner's
                                              own profile page (frontend ProfileContent.tsx)
  • afc_player.aggregation.compute_player_stats -> the TEAM-record numbers on the public
                                              player profile (frontend PlayerClient.tsx)

Both call counted_event_ids(user) / counted_tournament_team_ids(user) so the two pages
can never disagree again.
"""
from afc_tournament_and_scrims.models import (
    RegisteredCompetitors,
    TournamentTeam,
    TournamentTeamMember,
)


# ── Status allow-lists (allow-list, NOT deny-list: a status added later must be ────
# ── consciously opted IN rather than silently counting as participation.) ──────────

# RegisteredCompetitors.STATUS_CHOICES: registered/disqualified/withdrawn/left/pending/approved/rejected.
# "registered" and "approved" are the two that mean "you are in". "pending" has not been accepted yet.
COUNTED_COMPETITOR_STATUSES = frozenset({"registered", "approved"})

# TournamentTeamMember.TEAM_MEMBER_STATUS: pending/active/rejected/approved.
COUNTED_TEAM_MEMBER_STATUSES = frozenset({"active", "approved"})

# TournamentTeam.TEAM_STATUS: active/disqualified/withdrawn/left.
COUNTED_TEAM_STATUSES = frozenset({"active"})


def counted_solo_registrations(user):
    """
    The RegisteredCompetitors rows (SOLO entry path) that count as `user` having
    taken part. Returns a queryset so callers can .values_list("event_id") without
    pulling rows into memory.
    """
    return (
        RegisteredCompetitors.objects
        .filter(user=user, status__in=COUNTED_COMPETITOR_STATUSES)
        # Waitlisted = queued, never given a slot. No-show = had a slot, did not turn up.
        # Neither played, so neither counts toward a "played" total.
        .filter(is_waitlisted=False, is_no_show=False)
        # A draft event is not a real event; it is an organizer's unpublished sketch.
        .exclude(event__is_draft=True)
    )


def counted_team_memberships(user):
    """
    The TournamentTeamMember rows (SQUAD entry path) that count as `user` having
    taken part. Gates on BOTH the member's own status and the team's: a rejected
    player on an active team did not play, and every member of a disqualified team
    did not play either.
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

    Consumed by afc_player.aggregation.compute_player_stats to build the player's
    TEAM record (team_matches / team_wins / team_win_rate). Using this instead of a
    bare TournamentTeamMember.filter(user=...) is what stops a rejected roster slot
    from lending a player the wins of a team that never fielded them.
    """
    return list(
        counted_team_memberships(user)
        .values_list("tournament_team_id", flat=True)
        .distinct()
    )


def counted_event_ids(user):
    """
    Every Event id `user` genuinely took part in, across BOTH entry paths, deduped.

    Consumed by afc_auth.views.get_user_profile to split into the "Tournaments" and
    "Scrims" counts (Event.competition_type) shown on the owner's own profile.
    """
    solo_event_ids = counted_solo_registrations(user).values_list("event_id", flat=True)
    squad_event_ids = (
        counted_team_memberships(user)
        .values_list("tournament_team__event_id", flat=True)
    )
    return set(solo_event_ids) | set(squad_event_ids)
