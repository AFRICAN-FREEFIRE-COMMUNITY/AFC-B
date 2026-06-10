# afc_organizers/blacklist.py
# ──────────────────────────────────────────────────────────────────────────────
# Registration-time enforcement for the organizer blacklist feature.
#
# ONE entry point: organizer_blacklist_block(organization, team, user_ids). The tournament
# registration view (afc_tournament_and_scrims.views.register_for_event, TEAM path) calls it
# for any event that has an owning Organization, AFTER its existing ban checks. It returns a
# human-readable 403 message when the registration must be blocked, or None when it may proceed.
#
# Two independent reasons to block, matching the two model layers:
#   (a) TEAM-level  - an active OrganizerBlacklist exists for (organization, team). This blocks
#       re-registering the team ENTITY itself.
#   (b) PLAYER-level (the FOLLOWS-THE-PLAYER rule) - any registering user has an active
#       OrganizerBlacklistPlayer (is_active=True) under an ACTIVE blacklist of THIS organization.
#       Crucially this is queried by (organization, user), NOT by team, so a player who was
#       snapshotted onto a blacklist and has since left that team (and even joined a different
#       team) is STILL blocked from this organizer's events. The block tracks the person.
#
# "Active" everywhere means OrganizerBlacklist.is_currently_active(): status == "active" AND
# end_date in the future. Expressed as a filter (status="active", end_date__gt=now) so a lapsed
# blacklist stops blocking the instant it expires, with no sweep required.
#
# Imported lazily inside register_for_event to avoid an afc_organizers <-> afc_tournament import
# cycle (Event references afc_organizers.Organization; this module is only needed at call time).
# Full spec: WEBSITE/tasks/organizer-blacklist-design.md.
# ──────────────────────────────────────────────────────────────────────────────
from django.utils import timezone

from .models import OrganizerBlacklist, OrganizerBlacklistPlayer


def organizer_blacklist_block(organization, team, user_ids):
    """Return a 403 message if this (organization, team, roster) registration must be blocked
    by an organizer blacklist, else None.

    Args:
        organization: the event's owning Organization (caller only invokes this when set).
        team:         the Team being registered.
        user_ids:     iterable of the registering roster members' user_ids.

    Block reasons (first match wins, team-level checked first):
        1. An active OrganizerBlacklist exists for (organization, team)  -> team is blacklisted.
        2. Any user_id has an active OrganizerBlacklistPlayer under an active blacklist of THIS
           organization -> that player is blacklisted (follows-the-player; queried by org+user).
    """
    now = timezone.now()

    # ── (1) TEAM-level: is THIS team blacklisted by THIS organizer right now? ──
    # Blocks re-registering the team entity. end_date__gt=now mirrors is_currently_active()
    # so an expired blacklist does not block.
    team_block = (
        OrganizerBlacklist.objects.filter(
            organization=organization,
            team=team,
            status="active",
            end_date__gt=now,
        )
        .order_by("-end_date")
        .first()
    )
    if team_block:
        return (
            "Your team is blacklisted by this organizer and cannot register for their "
            f"events until {team_block.end_date.date().isoformat()}."
        )

    # ── (2) PLAYER-level: is any registering player blacklisted by THIS organizer? ──
    # THE FOLLOWS-THE-PLAYER RULE. We deliberately query by (organization, user) and NOT by
    # team: the join walks OrganizerBlacklistPlayer -> its blacklist's organization, so a player
    # snapshotted onto ANY active blacklist of this organization is caught even if they have
    # since left the blacklisted team and joined a different one. is_active=True lets an
    # individually-lifted player through while their former team-mates stay blocked.
    user_ids = list(user_ids or [])
    if not user_ids:
        return None

    blocked_player = (
        OrganizerBlacklistPlayer.objects.filter(
            user_id__in=user_ids,
            is_active=True,
            blacklist__organization=organization,
            blacklist__status="active",
            blacklist__end_date__gt=now,
        )
        .select_related("user")
        .first()
    )
    if blocked_player:
        username = blocked_player.user.username if blocked_player.user else "A player"
        return (
            f"{username} is blacklisted by this organizer and cannot register for their events."
        )

    # Nothing blocks this registration.
    return None
