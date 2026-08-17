"""
afc_fantasy.roster - who can be picked in a league, and which team they count as.

WHY THIS IS ITS OWN FILE
    "Who is playing in this event" sounds like one query and is not. AFC records it in two shapes
    depending on the event: a SQUAD event has TournamentTeam rows with TournamentTeamMember rosters,
    while a SOLO event has RegisteredCompetitors rows pointing straight at users with no team at all.
    Pricing, the squad builder and the max-per-team rule all need the same answer, so they must all
    ask the same function or they will disagree - and the way they would disagree is that a player
    is pickable but unpriceable, which reads to a fan as the site being broken.

WHO IS EXCLUDED, AND WHY
    Only ACTIVE competitors. A disqualified, withdrawn or departed team is not going to play, so
    offering their players would sell a fan a pick that can never score. Pending and rejected
    registrations are excluded for the same reason: they are not in the event yet, and a league that
    opened before registration closed would otherwise list people who never turn up.

    A no-show is NOT excluded here. It is marked on the day, long after picks lock, so filtering on
    it would silently change the pool between the squad builder and the price list.

THE TEAM MATTERS AS MUCH AS THE PLAYER
    max_per_team is the rule that stops every fan entering the same squad, so the team a player
    counts as has to be the team they play for IN THIS EVENT, not whatever club their profile says
    today. Rosters change; a squad built in week one must still be explainable in week three. That
    is also why PlayerPrice stores the team rather than looking it up later.

HOW IT CONNECTS
    Reads afc_tournament_and_scrims (TournamentTeam, TournamentTeamMember, RegisteredCompetitors)
    and afc_team.Team. Called by afc_fantasy.pricing.apply_prices (to build the price list) and by
    afc_fantasy.views (to serve the squad builder its pool).
"""
from afc_tournament_and_scrims.models import (
    RegisteredCompetitors,
    TournamentTeamMember,
)

# Competitors who will actually take part. Anything else is a pick that cannot score.
ACTIVE_TEAM_STATUSES = ("active",)
ACTIVE_MEMBER_STATUSES = ("active", "approved")
ACTIVE_REGISTRATION_STATUSES = ("registered", "approved")


def eligible_players(event):
    """[(player_id, team_or_None), ...] - everyone pickable in this event, deduplicated.

    Squad events resolve through the event roster; solo events through the registration list, where
    there is no team and `None` is the honest answer rather than an invented one. A player who
    somehow appears in both (a squad member who also registered solo) is returned ONCE, keeping
    their team, because two rows would let one fan pick the same person twice and double every
    point they score.
    """
    seen = {}

    # ── squad events: the frozen per-event roster ─────────────────────────────────────────────
    # Filtered on the TEAM's status as well as the member's: a disqualified team's roster rows stay
    # "active", so without this a fan could still pick players from a team that is out of the event.
    members = (
        TournamentTeamMember.objects
        .filter(tournament_team__event=event,
                tournament_team__status__in=ACTIVE_TEAM_STATUSES,
                status__in=ACTIVE_MEMBER_STATUSES)
        .values_list("user_id", "tournament_team__team_id")
    )
    for user_id, team_id in members:
        seen.setdefault(user_id, team_id)

    # ── solo events: the registration list, no teams involved ─────────────────────────────────
    solo = (
        RegisteredCompetitors.objects
        .filter(event=event, status__in=ACTIVE_REGISTRATION_STATUSES,
                user_id__isnull=False, is_waitlisted=False)
        .values_list("user_id", "team_id")
    )
    for user_id, team_id in solo:
        seen.setdefault(user_id, team_id)

    return list(seen.items())


def eligible_with_teams(event):
    """[(player_id, Team|None), ...] - the same list with Team objects resolved in ONE query.

    Pricing needs the Team itself, not its id, because the team premium reads its tier. Fetching
    them together avoids a query per player, which on a 20-team event would be 100 round trips just
    to open a league.
    """
    from afc_team.models import Team

    pairs = eligible_players(event)
    team_ids = {tid for _, tid in pairs if tid}
    teams = {t.pk: t for t in Team.objects.filter(pk__in=team_ids)} if team_ids else {}
    return [(pid, teams.get(tid)) for pid, tid in pairs]
