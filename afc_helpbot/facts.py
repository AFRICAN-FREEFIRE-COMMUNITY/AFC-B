"""afc_helpbot.facts - what the Help panel may know about the signed-in person (owner pick, 1 Oct 2026:
"also answers about the signed-in person's own account").

ONE RULE: EVERYTHING HERE IS READ FROM THE CALLER'S OWN ROWS
------------------------------------------------------------
`account_facts(user)` takes the user the SERVER resolved from the session token (views._actor), never
an id from the request (R58, R64, R88). Every query below is filtered by that user, or by the team
that user is a member of. There is no parameter through which a visitor could ask about somebody
else, so the assistant cannot leak another person's account even if it is talked into trying.

WHAT IS INCLUDED, AND WHY
-------------------------
The questions players actually bring to support (the Discord bot's history and the support desk):
"why can't I leave my team", "am I registered", "why can't I change my UID", "am I banned", "did
you get my ticket". So: the account basics, the team and role, the transfer window with its real
dates, the events that hold them in their team, their registrations, an active ban, and their open
tickets. Nothing secret: no email address, no phone, no payout details, no session data.

FAIL SOFT
---------
Each section is built on its own and a failure drops that section with a log line, never the
answer: a chat about tiers must not 500 because one join went wrong.

CONNECTS TO: afc_helpbot/views.py (help_chat calls account_facts for a signed-in caller and passes
the dict to brain.ask), afc_rankings.models.Season (the window), afc_team.views (the roster lock
rule, shared so the assistant and the Leave button can never disagree), afc_support.views (the
player's own tickets, the same query as My tickets on /support).
"""
import logging
from urllib.parse import quote

from django.utils import timezone

log = logging.getLogger(__name__)

MAX_REGISTRATIONS = 8
MAX_TICKETS = 5


def _account(user):
    from afc_auth import two_factor

    try:
        two_factor_on = bool(two_factor.is_enabled_for(user))
    except Exception:
        two_factor_on = None
    return {
        "in_game_name": user.username,
        "uid": user.uid or "",
        "country": user.country or "",
        "account_status": user.status,
        "discord_connected": bool(user.discord_connected),
        "two_factor_on": two_factor_on,
        "joined_afc": user.created_at.date().isoformat() if user.created_at else "",
        "profile_page": "/profile",
    }


def _membership(user):
    from afc_team.models import TeamMembers

    # One team per player (TeamMembers.unique_member_one_team), so first() is the only row.
    return TeamMembers.objects.filter(member=user).select_related("team").first()


def _team(user, membership):
    if membership is None:
        return None
    from afc_auth.models import TeamBan

    team = membership.team
    ban = TeamBan.objects.filter(team=team, ban_end_date__gt=timezone.now()).first()
    return {
        "name": team.team_name,
        "my_role": membership.get_management_role_display(),
        "my_in_game_role": membership.get_in_game_role_display() if membership.in_game_role else None,
        "i_am_owner": team.team_owner_id == user.user_id,
        # Teams are addressed by name (R22), encoded the way the site's lib/routes.ts segment() does.
        "team_page": "/teams/" + quote(team.team_name, safe=""),
        "team_banned_until": ban.ban_end_date.date().isoformat() if ban else None,
        "team_ban_reason": ban.reason if ban else None,
    }


def _transfer_window():
    from afc_rankings.models import Season

    today = timezone.localdate()
    season = Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()
    next_open = (Season.objects.filter(transfer_window_open__gt=today)
                 .order_by("transfer_window_open")
                 .values_list("transfer_window_open", flat=True).first())
    if season is None:
        return {"season": None, "rule": "There is no active season, so team moves are not locked by a window.",
                "next_window_opens": next_open.isoformat() if next_open else None}
    return {
        "season": season.name,
        "opens": season.transfer_window_open.isoformat() if season.transfer_window_open else None,
        "closes": season.transfer_window_close.isoformat() if season.transfer_window_close else None,
        "is_open_today": bool(season.is_transfer_window_open(today)),
        "next_window_opens": next_open.isoformat() if next_open else None,
        "rule": ("Players can join or create a team at any time. Leaving a team, or being removed from one, "
                 "is only possible while the transfer window is open."),
    }


def _roster_locks(user, membership):
    """The events that keep this player in their team right now: the SAME rule the Leave button and
    the kick refusal use (afc_team.views._active_event_roster_blockers)."""
    if membership is None:
        return []
    from afc_team.views import _active_event_roster_blockers

    return [{"event": e.event_name, "page": f"/tournaments/{e.slug}" if e.slug else ""}
            for e in _active_event_roster_blockers(membership.team, user.user_id)]


def _registrations(user, membership):
    from django.db.models import Q

    from afc_tournament_and_scrims.models import RegisteredCompetitors

    mine = Q(user=user)
    if membership is not None:
        mine |= Q(team=membership.team)
    rows = (RegisteredCompetitors.objects.filter(mine)
            .filter(event__event_status__in=["upcoming", "ongoing"], event__end_date__gte=timezone.localdate())
            .exclude(status__in=["withdrawn", "left", "rejected", "disqualified"])
            .select_related("event").order_by("event__start_date")[:MAX_REGISTRATIONS])
    return [{
        "event": r.event.event_name,
        "page": f"/tournaments/{r.event.slug}" if r.event.slug else "",
        "type": r.event.competition_type,
        "entered_as": "team" if r.team_id else "solo",
        "registration_status": r.status,
        "waitlisted": bool(r.is_waitlisted),
        "starts": r.event.start_date.isoformat() if r.event.start_date else None,
        "event_status": r.event.event_status,
    } for r in rows]


def _player_ban(user):
    from afc_auth.models import BannedPlayer

    ban = (BannedPlayer.objects.filter(banned_player=user, is_active=True, ban_end_date__gt=timezone.now())
           .order_by("-ban_end_date").first())
    if ban is None:
        return None
    return {"until": ban.ban_end_date.date().isoformat(), "reason": ban.reason}


def _tickets(user):
    from afc_support.models import SupportTicket
    from afc_support.views import _mine_queryset

    rows = (_mine_queryset(user)
            .exclude(status__in=[SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED])
            .order_by("-last_message_at")[:MAX_TICKETS])
    return [{"number": t.ticket_number, "status": t.status, "subject": (t.subject or "")[:80]} for t in rows]


def account_facts(user) -> dict:
    """The signed-in person's own account, for the assistant. Never raises."""
    facts = {"today": timezone.localdate().isoformat()}
    membership = None
    try:
        membership = _membership(user)
    except Exception:
        log.exception("help bot facts: team membership failed for user %s", user.pk)

    sections = (
        ("account", lambda: _account(user)),
        ("team", lambda: _team(user, membership)),
        ("transfer_window", _transfer_window),
        ("events_holding_me_in_my_team", lambda: _roster_locks(user, membership)),
        ("my_registrations", lambda: _registrations(user, membership)),
        ("my_active_ban", lambda: _player_ban(user)),
        ("my_open_support_tickets", lambda: _tickets(user)),
    )
    for key, build in sections:
        try:
            facts[key] = build()
        except Exception:
            log.exception("help bot facts: section %s failed for user %s", key, user.pk)
    return facts
