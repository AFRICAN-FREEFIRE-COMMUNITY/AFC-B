# afc_organizers/blacklist.py
# ──────────────────────────────────────────────────────────────────────────────
# Registration-time enforcement for the organizer blacklist feature.
#
# ONE entry point: organizer_blacklist_block(organization, team, user_ids). The tournament
# registration view (afc_tournament_and_scrims.views.register_for_event) calls it for any event
# that has an owning Organization, AFTER its existing ban checks, from BOTH paths:
#   - TEAM path: organizer_blacklist_block(org, team, roster_user_ids)
#   - SOLO path: organizer_blacklist_block(org, None, [user_id])   <- team=None, player check only
# It returns a human-readable 403 message when the registration must be blocked, or None when it
# may proceed.
#
# Two independent reasons to block, matching the two model layers:
#   (a) TEAM-level  - an active OrganizerBlacklist exists for (organization, team). This blocks
#       re-registering the team ENTITY itself. SKIPPED ENTIRELY when `team` is None: a solo
#       registrant has no team, and filtering team=None would wrongly match PLAYER-target
#       blacklists (whose team column is NULL by design) and report them as a team block.
#   (b) PLAYER-level (the FOLLOWS-THE-PLAYER rule) - any registering user has an active
#       OrganizerBlacklistPlayer (is_active=True) under an ACTIVE blacklist of THIS organization.
#       Crucially this is queried by (organization, user), NOT by team, so a player who was
#       snapshotted onto a blacklist and has since left that team (and even joined a different
#       team) is STILL blocked from this organizer's events. The block tracks the person.
#       This same query is what makes PLAYER-TARGET blacklists (owner backlog item 1, 2026-08-03)
#       work with no extra code: such a blacklist is just an OrganizerBlacklist with team=NULL and
#       one OrganizerBlacklistPlayer row, so it is caught here like any snapshotted player.
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


def roster_change_block(event, team, user_ids):
    """Return a 403 message if these players may not join THIS event's roster, else None.

    WHY THIS EXISTS, and it is the more important half of the story:
    ``organizer_blacklist_block`` was called from ``register_for_event`` and nowhere else, so the
    blacklist only ever looked at the roster as it stood at the moment of registration. Three other
    endpoints create ``TournamentTeamMember`` rows and checked nothing:

        edit_roster, add_player_to_event_roster, add_teams_to_event

    A captain could therefore register a clean roster, pass every gate, and then swap the
    blacklisted player straight back in. The player competed in the organizer's event and the
    feature's entire promise, that a block follows the player, was decorative. The site-wide
    BannedPlayer check had the same hole for the same reason.

    So the rule lives HERE, once, and every door that can put a player on a roster calls it. A new
    roster path that forgets to call it is the bug this function is shaped to prevent; there is no
    second copy of the logic to drift from.

    Args:
        event:    the Event being rostered into. Events with no owning Organization (native AFC
                  events, organization_id None) cannot be organizer-blacklisted, so only the
                  site-wide ban applies to them.
        team:     the Team whose roster is changing, or None.
        user_ids: the users being ADDED. Callers pass only the additions, never the whole roster:
                  re-checking players who are already on the roster would let a blacklist created
                  mid-event block an unrelated later edit, which punishes the wrong person.
    """
    user_ids = [uid for uid in (user_ids or []) if uid]
    if not user_ids:
        return None

    # Site-wide ban first: it outranks any single organizer's decision, and its message says so.
    from afc_auth.models import BannedPlayer

    # is_active alone is not enough: a ban row keeps is_active True past its end date until
    # something sweeps it, so an expired ban would keep blocking. Check the window as well.
    banned = (
        BannedPlayer.objects.filter(
            banned_player_id__in=user_ids, is_active=True, ban_end_date__gt=timezone.now()
        )
        .select_related("banned_player")
        .first()
    )
    if banned:
        name = getattr(banned.banned_player, "username", "A player")
        return f"{name} is banned from AFC and cannot be added to an event roster."

    if not getattr(event, "organization_id", None):
        return None

    return organizer_blacklist_block(event.organization, team, user_ids)


def organizer_blacklist_block(organization, team, user_ids):
    """Return a 403 message if this (organization, team, roster) registration must be blocked
    by an organizer blacklist, else None.

    Args:
        organization: the event's owning Organization (caller only invokes this when set).
        team:         the Team being registered, or None for a SOLO registration (skips the
                      team-level check and runs the player check only).
        user_ids:     iterable of the registering user_ids (the whole roster, or the one solo
                      registrant).

    Block reasons (first match wins, team-level checked first):
        1. An active OrganizerBlacklist exists for (organization, team)  -> team is blacklisted.
           Only evaluated when `team` is not None.
        2. Any user_id has an active OrganizerBlacklistPlayer under an active blacklist of THIS
           organization -> that player is blacklisted (follows-the-player; queried by org+user).
           Catches both snapshotted team-mates and directly PLAYER-TARGET blacklists.
    """
    now = timezone.now()

    # ── (1) TEAM-level: is THIS team blacklisted by THIS organizer right now? ──
    # Blocks re-registering the team entity. end_date__gt=now mirrors is_currently_active()
    # so an expired blacklist does not block. Skipped for solo registrations (team is None) -
    # see the module header for why filtering on a NULL team would be actively wrong.
    team_block = (
        OrganizerBlacklist.objects.filter(
            organization=organization,
            team=team,
            status="active",
            end_date__gt=now,
        )
        .order_by("-end_date")
        .first()
        if team is not None else None
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
        .select_related("user", "blacklist")
        .first()
    )
    if blocked_player:
        # Name the person so a captain knows exactly who to drop from the roster, and give the
        # date the block lapses so nobody has to ask the organizer. On the SOLO path the caller
        # IS that person, which reads naturally too ("<their name> is blacklisted...").
        username = blocked_player.user.username if blocked_player.user else "A player"
        until = blocked_player.blacklist.end_date.date().isoformat() \
            if blocked_player.blacklist and blocked_player.blacklist.end_date else None
        suffix = f" until {until}" if until else ""
        return (
            f"{username} is blacklisted by this organizer and cannot register for their "
            f"events{suffix}."
        )

    # Nothing blocks this registration.
    return None
