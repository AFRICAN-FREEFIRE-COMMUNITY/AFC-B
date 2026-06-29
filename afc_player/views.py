from django.shortcuts import render
from django.shortcuts import get_object_or_404
from django.db.models import Sum, Count
from rest_framework.response import Response
from rest_framework.decorators import api_view

from afc_auth.models import User, BannedPlayer
from afc_team.models import TeamMembers
from afc_tournament_and_scrims.models import Match, TournamentPlayerMatchStats, TournamentTeamMatchStats, TournamentTeamMember
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db.models import Sum
from afc_auth.models import User
from afc_tournament_and_scrims.models import (
    TournamentPlayerMatchStats,
    TournamentTeamMatchStats,
    TournamentTeamMember,
    Match,
    # PlayerWinning = a player's share of an event prize, written by
    # afc_rankings.admin_prize.prize_create (_distribute_payout) when an admin/organizer
    # records a team/solo prize. We read it back here to surface tournament winnings on
    # the public player profile (feature "Prizepool auto-links to winners' history", 2026-06-15).
    PlayerWinning,
)

# Shared player-stats aggregation (reused by the admin + public player endpoints).
from afc_player.aggregation import (
    compute_player_stats,
    basic_player_profile,
    player_tier_history,
)

# Session-token resolver. We reuse the SAME helper the authenticated team/auth
# endpoints use (afc_auth.views.validate_token: token string -> User or None) so
# the optional-auth path here behaves identically to the rest of the codebase.
from afc_auth.views import validate_token, is_stats_admin


# ──────────────────────────────────────────────────────────────────────────────
# PRIVACY HELPERS (player stats visibility)
# ──────────────────────────────────────────────────────────────────────────────
# The detailed performance numbers on a player profile are PRIVATE: only the
# player themselves and that player's CURRENT teammates may see them. Anonymous
# or unrelated viewers get the public identity block but NOT the sensitive stats.
#
# "Teammate" is defined by REAL roster membership in afc_team.TeamMembers (one row
# per (team, member); a UniqueConstraint on `member` means a user is on at most one
# team at a time). Two users are teammates iff they are BOTH members of the same
# Team. AFC admins (User.role == "admin") may always see the stats (moderation /
# support need full visibility), mirroring the existing require_admin gate.
#
# These helpers are consumed by get_public_player_stats below. The frontend caller
# is PlayerClient.tsx (public player page) and ProfileContent.tsx (owner's own
# profile), both of which POST /player/get-public-player-stats/ and now send the
# viewer's Bearer token when logged in so we can identify them here.


def _viewer_from_request(request):
    """
    Resolve the OPTIONAL viewer from an Authorization: Bearer <token> header.

    The endpoint stays public (no token required), so a missing / malformed /
    expired token simply yields None (anonymous viewer) instead of an error.
    Returns a User instance or None.
    """
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    # validate_token returns None for unknown / expired tokens — exactly the
    # anonymous-viewer behaviour we want, so no extra guarding is needed.
    return validate_token(token)


def _can_view_player_stats(viewer, player):
    """
    Decide whether `viewer` (a User or None) may see `player`'s INDIVIDUAL stats.

    Owner rule (2026-06-24 lockdown + 2026-06-27 per-user opt-in): individual player statistics are
    PRIVATE BY DEFAULT. Visible to:
      • the viewer themselves (own profile) — always, regardless of any preference, and
      • AFC admins (is_stats_admin: role admin/moderator/support or a granular platform-admin role) —
        always, they override the user's choice, and
      • ANY other viewer (teammates, other players, organizers, sponsors, the public, anonymous) ONLY
        when the player has OPTED IN via their profile switch (player.stats_visible == True).

    So the default (stats_visible False) reproduces the original lockdown exactly — only self + admins.
    Flipping the switch on opens the individual stats to everyone else. anonymous (viewer None) can see
    them too once opted in (a public profile), since the stats are then explicitly public.

    Query cost: O(1) — own-id check, is_stats_admin (one indexed UserRoles existence check at most),
    then a boolean field read.
    """
    # Own profile — always full visibility, even if the user hides stats from others.
    if viewer is not None and viewer.user_id == player.user_id:
        return True

    # AFC admins (NOT organizers/sponsors) always see full stats — they override the user's choice.
    # Single source of truth shared with the team-stats gate so both surfaces agree on who is an admin.
    if viewer is not None and is_stats_admin(viewer):
        return True

    # Everyone else (including anonymous viewers) sees the stats ONLY if the player opted in.
    return bool(player.stats_visible)


# ──────────────────────────────────────────────────────────────────────────────
# TOURNAMENT WINNINGS (per-player prize history)
# ──────────────────────────────────────────────────────────────────────────────
# These rows are PlayerWinning records written by afc_rankings.admin_prize.prize_create
# (_distribute_payout) whenever an admin/organizer records a team/solo prize — the team
# payout is split equally among the active roster and one PlayerWinning is saved per player.
# We read them back here so each player's lifetime prize total + per-event winnings show on
# their public profile (frontend PlayerClient.tsx "Earnings share" / Tournament Winnings card).
# Gated behind the SAME stats_visible flag as the rest of the performance stats below.
def _player_winnings(player):
    """Return (total_earnings_ngn:str, tournament_winnings:list) for a player.

    Reads PlayerWinning (written by admin_prize.prize_create) newest-first, with the event +
    team prefetched so the listing is query-cheap. ``total_earnings_ngn`` is the Decimal sum as
    a string (full NGN precision, no float rounding); each row carries event id/name, the share
    amount as a string, the team name (or None for solo prizes), and the awarded date.
    """
    rows = (
        PlayerWinning.objects.filter(player=player)
        .select_related("event", "tournament_team__team")
        .order_by("-created_at", "-id")
    )
    total = PlayerWinning.objects.filter(player=player).aggregate(s=Sum("amount"))["s"] or 0
    winnings = [
        {
            "event_id": w.event_id,
            "event_name": w.event.event_name if w.event_id else None,
            "amount": str(w.amount),                       # NGN, this player's share
            "tournament_team_name": (
                w.tournament_team.team.team_name
                if w.tournament_team_id and w.tournament_team.team_id
                else None                                  # None for solo prizes
            ),
            "created_at": w.created_at.isoformat() if w.created_at else None,
        }
        for w in rows
    ]
    return str(total), winnings


# Create your views here.



@api_view(["GET"])
def get_all_users(request):
    """
    ADMIN players list. Returns EVERY user with lightweight aggregate stats
    (total_kills / total_wins / total_mvps), their current team name, and ban/role
    status. Consumed by the admin Players page (frontend app/(a)/a/players/page.tsx,
    which fetches GET /player/get-all-players/ and paginates/filters client-side).

    PERFORMANCE (why this looks like this):
    The previous version ran ~6-8 ORM queries PER user inside a Python for-loop. With
    ~6k users that is an N+1 explosion of ~40k queries (the endpoint took 30-45s and the
    admin page never finished loading). It is now a fixed handful of GROUPED/bulk queries
    assembled in memory. The response shape and every number are byte-for-byte identical
    to the old loop - the win/team-name/ban semantics below mirror the original exactly.
    """
    users = list(User.objects.all().only("user_id", "username", "status", "role"))

    # ── total kills per player: one grouped aggregate (was: 1 aggregate per user) ──
    kills_by_user = {
        row["player"]: row["total"] or 0
        for row in TournamentPlayerMatchStats.objects
        .values("player").annotate(total=Sum("kills"))
    }

    # ── MVP count per player: one grouped count (was: Match.filter(mvp=user).count() per user) ──
    mvps_by_user = {
        row["mvp"]: row["c"]
        for row in Match.objects.filter(mvp__isnull=False)
        .values("mvp").annotate(c=Count("pk"))
    }

    # ── tournament-team participation: user -> [tournament_team ids] (for WINS only) ──
    # TournamentTeamMember is the per-event participation HISTORY (who played for which
    # team in a tournament); rows are never deleted, so it is the right source for
    # counting wins across every team a player ever competed with - but it is the WRONG
    # source for "current team" (a player who once played under a team keeps showing it
    # forever even after leaving). See the bug fix below for the displayed team name.
    team_ids_by_user = {}
    for m in (TournamentTeamMember.objects
              .values("user", "tournament_team")
              .order_by("user", "id")):
        team_ids_by_user.setdefault(m["user"], []).append(m["tournament_team"])

    # ── CURRENT team name: from the LIVE roster TeamMembers (owner bug 2026-06-20) ──
    # The admin players list used to show last_team_name from TournamentTeamMember (the
    # tournament history above), so it displayed a stale team a player had since LEFT
    # (e.g. NVS.PRIME showed "RESTART ESPORTS" though he is not on that roster). The
    # source of truth for the CURRENT team is afc_team.TeamMembers, which has a
    # unique-one-team-per-member constraint and is what the team roster + public profile
    # read. One bulk query, keyed by member -> current team name.
    current_team_name_by_user = {
        row["member"]: row["team__team_name"]
        for row in TeamMembers.objects.values("member", "team__team_name")
    }

    # ── wins (placement == 1) per tournament_team: one grouped count ──
    wins_by_team = {
        row["tournament_team"]: row["c"]
        for row in TournamentTeamMatchStats.objects.filter(placement=1)
        .values("tournament_team").annotate(c=Count("pk"))
    }

    # ── active bans: one set lookup (was: an .exists() per user) ──
    banned_ids = set(
        BannedPlayer.objects.filter(is_active=True).values_list("banned_player", flat=True)
    )

    data = []
    for user in users:
        uid = user.user_id
        # total_wins = matches where ANY of this user's teams placed 1st (same as old query)
        total_wins = sum(wins_by_team.get(tid, 0) for tid in team_ids_by_user.get(uid, []))
        data.append({
            "user_id": uid,
            "name": user.username,
            # CURRENT team from the live roster (was the stale tournament-history name).
            "team_name": current_team_name_by_user.get(uid),
            "total_kills": kills_by_user.get(uid, 0),
            "total_wins": total_wins,
            "total_mvps": mvps_by_user.get(uid, 0),
            "status": "banned" if uid in banned_ids else user.status,
            "role": user.role  # optional but useful
        })

    return Response({"users": data})


@api_view(["POST"])
def get_player_details(request):
    # ADMIN player profile (keyed by player_id). The heavy stat aggregation now lives in
    # afc_player.aggregation.compute_player_stats so the public player page can reuse the
    # EXACT same numbers (single source of truth, no drift). This response keeps every key
    # it returned before — the shared helper produces the same scalar names — and additionally
    # gains per_event[] / recent_matches[] breakdown lists (additive; old callers ignore them).

    # AUTH (2026-06-08): this endpoint returns PII (player.email) and is the ADMIN players
    # directory detail (frontend app/(a)/a/players/[id]/page.tsx sends a Bearer token). It
    # previously had NO check despite the "auth-gated" comment below, so any caller could POST
    # a player_id and read that player's email. Require a valid token AND an AFC staff caller
    # (coarse role admin/moderator/support OR any granular UserRoles row).
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    caller = validate_token(auth.split(" ")[1])
    if not caller:
        return Response({"message": "Invalid session."}, status=401)
    if caller.role not in ("admin", "moderator", "support") and not caller.userroles.exists():
        return Response({"message": "Unauthorized."}, status=403)

    player_id = request.data.get("player_id")

    if not player_id:
        return Response({"message": "player_id is required"}, status=400)

    player = get_object_or_404(User, user_id=player_id)

    # Shared aggregation (kills/wins/mvps/kdr/avg_damage/win_rate + scrim/tournament splits
    # + booyahs + per_event[] + recent_matches[]). Defensive against null leaderboards.
    agg = compute_player_stats(player, include_breakdown=True)

    # Team + roles, ALL from the CURRENT live roster row (owner bug 2026-06-20). This
    # used to take the team NAME from the last TournamentTeamMember (tournament history),
    # which showed a team the player had since LEFT. The source of truth for the current
    # team + roles is the single TeamMembers row (unique one-team-per-member), the same
    # source the team roster + public profile read.
    member = TeamMembers.objects.filter(member=player).select_related("team").first()
    team_name = member.team.team_name if member and member.team_id else None
    in_game_role = member.in_game_role if member else None
    management_role = member.management_role if member else None

    return Response({
        "player_id": player.user_id,
        "name": player.username,
        "team": team_name,
        "email": player.email,            # admin surface — PII allowed here (auth-gated)
        "uid": player.uid,
        "discord_username": player.discord_username,
        # Admin player detail: show the IP-derived location (owner 2026-06-29), profile country as
        # fallback. Same source as the public flag (afc_auth.views.set_ip_country / User.ip_country).
        "country": (player.ip_country or player.country),
        "in_game_role": in_game_role,
        "management_role": management_role,

        # ── scalar aggregates (unchanged keys, now from the shared helper) ──
        "kdr": agg["kdr"],
        "avg_damage": agg["avg_damage"],
        "win_rate": agg["win_rate"],

        "total_kills": agg["total_kills"],
        "total_wins": agg["total_wins"],
        "total_mvps": agg["total_mvps"],

        "scrims_kills": agg["scrims_kills"],
        "tournaments_kills": agg["tournaments_kills"],

        "scrims_wins": agg["scrims_wins"],
        "tournaments_wins": agg["tournaments_wins"],

        "scrim_booyah": agg["scrim_booyah"],
        "tournament_booyah": agg["tournament_booyah"],

        # ── NEW additive breakdown (admin page can render the same tables the public page does) ──
        "total_matches": agg["total_matches"],
        "per_event": agg["per_event"],
        "recent_matches": agg["recent_matches"],
    })


@api_view(["POST"])
def get_public_player_stats(request):
    """
    PUBLIC player profile + PRIVACY-GATED stats, keyed by USERNAME / IGN.

    This is the public counterpart to the admin get_player_details above. It powers
    the public Player Profile page (PlayerClient.tsx) AND the owner's own profile
    Stats tab (ProfileContent.tsx). It returns:
      • a NON-sensitive identity block (NO email / no PII)        — basic_player_profile()
      • the player's published tier / rank history per season     — player_tier_history()
      • the SAME aggregated stats as the admin endpoint           — compute_player_stats()
        ONLY when the viewer is allowed to see them (see below).

    AUTH (optional): the endpoint stays public, but it now reads an OPTIONAL
    Authorization: Bearer <session-token> header to identify the viewer. The token
    is resolved with the shared validate_token helper; a missing/expired token just
    means "anonymous viewer".

    PRIVACY (stats_visible):
      The detailed performance numbers (kdr, avg_damage, win_rate, totals,
      per_event, recent_matches, booyah/scrim splits) are visible ONLY to:
        - the player themselves,
        - an AFC admin,
        - a CURRENT teammate (shares a team in afc_team.TeamMembers).
      For everyone else `stats_visible` is False and those sensitive numbers are
      ZEROED / EMPTIED. The public IDENTITY block (name, team, country, roles) and
      the tier_history are ALWAYS returned so the profile still reads as a real
      player page. The response is back-compatible: no keys were renamed; we only
      added the `stats_visible` flag and gate the values behind it.

    Body: {"player_ign": "<username>"}.
    A player with no recorded matches simply returns zeroes and empty lists
    (truthful empty state — nothing is fabricated). A player on no team returns
    team: null. Consumers: PlayerClient.tsx, ProfileContent.tsx (both send the
    viewer's token when logged in).
    """
    player_ign = request.data.get("player_ign")
    if not player_ign:
        return Response({"message": "player_ign is required."}, status=400)

    try:
        player = User.objects.get(username=player_ign)
    except User.DoesNotExist:
        return Response({"message": "Player not found."}, status=404)

    # Identify the (optional) viewer and decide whether the sensitive stats are
    # visible to them (self / admin / teammate). Anonymous => not visible.
    viewer = _viewer_from_request(request)
    stats_visible = _can_view_player_stats(viewer, player)

    # Identity (public, no PII) + published tier history are ALWAYS returned.
    profile = basic_player_profile(player, request=request)
    tier_history = player_tier_history(player)

    # Base payload: identity + tier history + the visibility flag. The sensitive
    # numbers are layered on below ONLY when the viewer is permitted to see them.
    payload = {
        **profile,
        # Player PK (identity-level, not PII) - the public profile needs it for the
        # fan/hater sentiment widget (owner 2026-06-20). basic_player_profile keys off
        # IGN and historically omitted the id; expose it explicitly here.
        "user_id": player.user_id,
        "tier_history": tier_history,
        "stats_visible": stats_visible,
    }

    if stats_visible:
        # Full stat block (scalars + per_event + recent_matches), exactly as before.
        stats = compute_player_stats(player, include_breakdown=True)
        # Per-player prize history (PlayerWinning rows written by admin_prize.prize_create).
        # Gated behind the SAME visibility flag as the other performance stats.
        total_earnings_ngn, tournament_winnings = _player_winnings(player)
        payload.update({
            # scalar aggregates
            "total_matches": stats["total_matches"],
            "total_kills": stats["total_kills"],
            "total_wins": stats["total_wins"],
            "total_mvps": stats["total_mvps"],
            "kdr": stats["kdr"],
            "avg_damage": stats["avg_damage"],
            "win_rate": stats["win_rate"],
            "scrims_kills": stats["scrims_kills"],
            "tournaments_kills": stats["tournaments_kills"],
            "scrims_wins": stats["scrims_wins"],
            "tournaments_wins": stats["tournaments_wins"],
            "scrim_booyah": stats["scrim_booyah"],
            "tournament_booyah": stats["tournament_booyah"],
            # breakdown lists
            "per_event": stats["per_event"],
            "recent_matches": stats["recent_matches"],
            # tournament prize winnings (lifetime total + per-event rows, newest first)
            "total_earnings_ngn": total_earnings_ngn,
            "tournament_winnings": tournament_winnings,
        })
    else:
        # PRIVATE: keep the same keys (back-compat for the frontend types) but ZERO
        # the sensitive performance numbers and EMPTY the breakdown lists, so no
        # private stat ever leaves the server for an unauthorized viewer. We skip
        # the heavy compute_player_stats() aggregation entirely in this branch.
        payload.update({
            "total_matches": 0,
            "total_kills": 0,
            "total_wins": 0,
            "total_mvps": 0,
            "kdr": 0,
            "avg_damage": 0,
            "win_rate": 0,
            "scrims_kills": 0,
            "tournaments_kills": 0,
            "scrims_wins": 0,
            "tournaments_wins": 0,
            "scrim_booyah": 0,
            "tournament_booyah": 0,
            "per_event": [],
            "recent_matches": [],
            # Prize winnings are private too: zero the total + empty the list for unauthorized
            # viewers (same back-compat contract as the sensitive numbers above).
            "total_earnings_ngn": "0",
            "tournament_winnings": [],
        })

    return Response({"player": payload})