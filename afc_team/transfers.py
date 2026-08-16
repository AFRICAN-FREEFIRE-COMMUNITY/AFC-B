# afc_team/transfers.py
#
# The two rules behind the automatic transfer feed (backlog item 21, owner 2026-08-08), kept in ONE
# named place so neither of them turns into a magic condition copied around the codebase:
#
#   1. HAS_COMPETED  - which teams are newsworthy enough to appear in the feed at all.
#   2. record_transfer() - how a roster change becomes a afc_team.models.TeamTransfer row.
#
# How it connects to the rest of the system:
#   - Called by : afc_team.signals.record_team_join / record_team_leave (writes), and
#                 afc_team.views_transfers.get_transfer_feed (reads, via has_competed_subquery).
#   - Writes to : afc_team.models.TeamTransfer.
#   - Reads     : afc_tournament_and_scrims.TournamentTeamMatchStats (the competed rule) and
#                 afc_rankings.Season (the transfer-window state at the moment of the move).
import logging
import threading

from django.db.models import Exists, OuterRef

logger = logging.getLogger(__name__)


# ────────────────── §0 "this team / this player is being deleted right now" ──────────────────
# Deleting a Team cascades to its TeamMembers rows, and every one of those deletes fires the
# post_delete receiver below, which wants to write a TeamTransfer row pointing at that team. Django
# runs its SET_NULL field updates BEFORE the cascaded child deletes, so rows created during those
# deletes are brand new, still point at the team, and make the final `DELETE FROM afc_team_team`
# fail with MySQL error 1451 (foreign key constraint fails). In other words: without this guard,
# `team.delete()` CRASHES for any team with members - which is reachable from the Django admin, a
# shell, and any future code path, not just from disband_team (that endpoint happens to delete the
# memberships first, so it survives by accident).
#
# THE SAME TRAP EXISTS ON THE PLAYER SIDE, and it is the more likely one to be hit: TeamMembers.member
# is a CASCADE to the user, so deleting an ACCOUNT deletes its membership, fires the same receiver,
# and leaves a fresh TeamTransfer pointing at the user being deleted. Verified against MySQL: without
# this guard, deleting any user who is on a team dies with
#   (1451) ... `afc_team_teamtransfer`, CONSTRAINT `..._player_id_..._fk_afc_auth_user`
# which would break account deletion from the Django admin outright. Both sides are therefore
# guarded, by the same mechanism, so neither can be fixed and the other forgotten.
#
# The guard is a per-thread set of primary keys currently inside a delete, per SCOPE ("team" /
# "player"), maintained by the pre_delete/post_delete receivers in afc_team/signals.py. When a
# membership disappears because its TEAM or its OWNER is going away, record_transfer still writes
# the entry - history is not silently dropped - but with that side left NULL, keeping only the
# *_at_move name. That is the same shape the row would have ended up in anyway once SET_NULL ran,
# so a disband reached either way leaves identical data.
#
# Thread-local, not a plain module global, because Django's dev server and gunicorn both serve
# concurrent requests in threads: a global set would let one request's disband suppress another
# request's ordinary transfer.
TEAM_SCOPE = "team"
PLAYER_SCOPE = "player"

_deleting = threading.local()


def _deleting_ids(scope):
    scopes = getattr(_deleting, "scopes", None)
    if scopes is None:
        scopes = _deleting.scopes = {}
    return scopes.setdefault(scope, set())


def mark_deleting(scope, pk):
    """Called from a pre_delete receiver. `scope` is TEAM_SCOPE or PLAYER_SCOPE."""
    _deleting_ids(scope).add(pk)


def unmark_deleting(scope, pk):
    """Called from the matching post_delete receiver."""
    _deleting_ids(scope).discard(pk)


def is_deleting(scope, pk):
    return pk in _deleting_ids(scope)


# ────────────────────────────── §1 the HAS COMPETED rule ──────────────────────────────
# "Only show moves for teams that have actually competed" (owner). The threshold had to be one of
# two things, and the choice matters:
#
#   • registered for an event - REJECTED. Registration is self-service and free, so anybody can
#     mint a team, register it for a scrim, and have every one of its roster changes published.
#     That is precisely the churn from teams nobody has heard of that the rule exists to keep out.
#
#   • PLAYED AT LEAST ONE MATCH - CHOSEN. A TournamentTeamMatchStats row only exists once an
#     organizer has entered (or uploaded) a result for that team in a match, so it is a fact
#     produced by AFC's own results pipeline rather than by the team itself. It cannot be
#     manufactured by a shell team, and it is the same "this team has been seen competing" signal
#     the leaderboards and rankings already treat as real.
#
# The trade-off, stated plainly: a brand-new team that has registered for its first tournament but
# has not played it yet is invisible in the feed until its first result lands. That is the correct
# side to err on for a PUBLIC feed - a quiet feed is recoverable, a feed full of ghost teams is not.
HAS_COMPETED_RULE = "the team has at least one recorded match result in an AFC event"


def has_competed_subquery(team_field="team_id"):
    """An ``Exists()`` for use in a TeamTransfer queryset: True when the row's team has competed.

    Used as a filter rather than a per-row Python check so the whole feed stays ONE query no matter
    how many entries it returns (Best practices §10: never load all results into memory).

    ``team_field`` is the column on the OUTER queryset holding the team id, so the same rule can be
    reused by any future queryset that references a team.
    """
    # Imported here, not at module import time: afc_team.models is imported by
    # afc_tournament_and_scrims.models, so a top-level import in the other direction would be a
    # circular import at startup.
    from afc_tournament_and_scrims.models import TournamentTeamMatchStats

    return Exists(
        TournamentTeamMatchStats.objects.filter(
            tournament_team__team_id=OuterRef(team_field)
        )
    )


def team_has_competed(team_id):
    """Plain boolean form of :data:`HAS_COMPETED_RULE`, for a single team.

    Only for one-off checks (tests, the admin, a future team page). The feed uses
    :func:`has_competed_subquery` instead so it does not run one query per row.
    """
    from afc_tournament_and_scrims.models import TournamentTeamMatchStats

    return TournamentTeamMatchStats.objects.filter(
        tournament_team__team_id=team_id
    ).exists()


# ────────────────────────── §2 turning a roster change into a row ──────────────────────────
def _active_season_window_state():
    """(season, is_open) for the CURRENT ranking season, or (None, None) when there is no season.

    Mirrors how afc_team.views decides the roster lock (``Season.objects.filter(is_active=True)``
    ordered newest-first, then ``is_transfer_window_open()``), so the flag stored on a transfer says
    the same thing the lock said at that moment.
    """
    from afc_rankings.models import Season

    season = Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()
    if season is None:
        return None, None
    return season, season.is_transfer_window_open()


def record_transfer(membership, direction):
    """Write one TeamTransfer row for a roster change. Best-effort: never breaks the caller.

    ``membership`` is the afc_team.models.TeamMembers instance being created or deleted, and
    ``direction`` is "joined" or "left". Called only from afc_team.signals, which runs INSIDE the
    endpoint that moved the player - so an exception here would roll back a legitimate join or
    kick over a feed entry. It is caught and logged instead (the sibling country-recompute receiver
    in the same file takes the same best-effort stance, and for the same reason).
    """
    from .models import Team, TeamTransfer

    try:
        # Look the team up rather than touching membership.team: on a team DISBAND the Team row is
        # on its way out, and a stale cached relation would raise. team_id is always present.
        team = Team.objects.filter(pk=membership.team_id).first()
        if team is None:
            return  # the team is already gone; there is nothing to name in the entry

        member = membership.member
        season, window_open = _active_season_window_state()

        TeamTransfer.objects.create(
            # None while the ACCOUNT itself is being deleted, for the same reason as the team just
            # below: pointing a brand-new row at a user one statement away from deletion is what
            # makes that delete fail (see §0). The username is still recorded, so the entry reads.
            player=None if is_deleting(PLAYER_SCOPE, membership.member_id) else member,
            # None while the team itself is being deleted: pointing the row at a row that is about
            # to vanish is what breaks the delete outright (see §0). The name is still recorded, so
            # the entry reads; it just drops out of the public feed, which is correct because a
            # disband is not a transfer and there is no team page left to link to.
            team=None if is_deleting(TEAM_SCOPE, team.team_id) else team,
            player_username_at_move=member.username,
            team_name_at_move=team.team_name,
            direction=direction,
            management_role=membership.management_role or "",
            season=season,
            in_transfer_window=window_open,
        )
    except Exception:
        logger.exception(
            "Failed to record a %s transfer for membership %s", direction, getattr(membership, "pk", None)
        )
