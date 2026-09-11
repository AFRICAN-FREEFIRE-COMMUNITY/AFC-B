"""
afc_tournament_and_scrims/open_roster.py - the two helpers behind open-roster events
(owner 2026-09-11: "organizers select if they want teams to be able to use any players for that
particular event. Roster lock won't apply to this event, but the event also will not count towards
any rankings or tiers. For such events, admins will be able to input the results of teams without
having to input the result of players.")

The switch itself is one column, Event.open_roster, declared once in event_contract.py. Most of
what it changes is a one-line `if not event.open_roster` at an existing gate (see the column's
comment in models.py for the full list). The two things that needed a home of their own:

    sync_after_save(event, user)   create_event / edit_event call this after the event is saved:
                                   an open-roster event gets its EventCountingControl row
                                   materialised with counts_toward_rankings=False, so the Rankings
                                   admin shows the truth. The aggregation does NOT rely on that row
                                   (afc_rankings.aggregation._open_roster_event_ids reads the
                                   column), and event_counting_update refuses to switch such an
                                   event back on. Switching open_roster OFF again leaves the row as
                                   it is: the lock lifts and the admin decides.

    roster_conflict(event, user_ids, exclude_tournament_team)
                                   the one-team-per-player-per-event rule as a reusable check.
                                   register_for_event has always applied it inline; edit_roster
                                   and add_player_to_event_roster never did, because two clubs
                                   cannot share a member. An open-roster outsider CAN be fielded
                                   by two teams, so every roster door now asks this. Returns the
                                   409 body (same shape register_for_event answers) or None.

Callers: afc_tournament_and_scrims/views.py (create_event, edit_event, edit_roster,
add_player_to_event_roster). Tests: afc_tournament_and_scrims/test_open_roster.py.
"""


def sync_after_save(event, user):
    """Force the rankings counting control OFF for an open-roster event. No-op otherwise."""
    if not event.open_roster:
        return
    # Local import: afc_rankings imports this app's models, so a module-level import would cycle.
    from afc_rankings.models import EventCountingControl

    control, created = EventCountingControl.objects.get_or_create(event=event)
    if created or control.counts_toward_rankings:
        control.counts_toward_rankings = False
        control.updated_by = user
        control.save(update_fields=["counts_toward_rankings", "updated_by", "updated_at"])


def roster_conflict(event, user_ids, exclude_tournament_team=None):
    """The 409 body when any of ``user_ids`` already sits on another live roster of ``event``.

    "Live" mirrors register_for_event: a roster whose team was disqualified, withdrew or left no
    longer holds the player (owner 2026-06-30, "removal frees re-registration"). The caller's own
    TournamentTeam is excluded so re-saving a roster never conflicts with itself.
    """
    from .models import TournamentTeamMember

    qs = (
        TournamentTeamMember.objects.filter(user_id__in=list(user_ids), tournament_team__event=event)
        .exclude(tournament_team__status__in=["disqualified", "withdrawn", "left"])
        .select_related("user", "tournament_team__team", "tournament_team__ghost_team")
    )
    if exclude_tournament_team is not None:
        qs = qs.exclude(tournament_team=exclude_tournament_team)
    conflicting = list(qs)
    if not conflicting:
        return None
    conflicts = [
        {"user_id": m.user_id, "username": m.user.username, "team_name": m.tournament_team.display_name}
        for m in conflicting
    ]
    detail = ", ".join(f"{c['username']} (already registered in {c['team_name']})" for c in conflicts)
    return {
        "message": f"Cannot save roster: {detail}.",
        "conflicts": conflicts,
        "user_ids": [c["user_id"] for c in conflicts],
    }
