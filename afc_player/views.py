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
    Match
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
from afc_auth.views import validate_token


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
    Decide whether `viewer` (a User or None) may see `player`'s detailed stats.

    True when ANY of:
      • the viewer IS the player (own profile),
      • the viewer is an AFC admin (User.role == "admin"),
      • the viewer shares an ACTIVE team with the player (teammate) — i.e. both
        are rows in TeamMembers for the SAME team.

    Anonymous (viewer is None) or unrelated viewers => False.

    Query cost: at most two tiny indexed lookups on TeamMembers (the player's
    team, then a membership existence check for the viewer on that team). No N+1.
    """
    if viewer is None:
        return False

    # Own profile — always full visibility.
    if viewer.user_id == player.user_id:
        return True

    # AFC admins always see full stats (consistent with require_admin elsewhere).
    if getattr(viewer, "role", None) == "admin":
        return True

    # Teammate check: find the player's current team (unique per member), then
    # confirm the viewer is also a member of that same team.
    player_team_id = (
        TeamMembers.objects.filter(member=player)
        .values_list("team_id", flat=True)
        .first()
    )
    if player_team_id is None:
        return False  # player is on no team -> nobody is a teammate

    return TeamMembers.objects.filter(
        team_id=player_team_id, member=viewer
    ).exists()



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

    # ── team membership: user -> [tournament_team ids] + last team name (mirrors .last()) ──
    # Ordered by id so the LAST membership row per user wins the display name, exactly like
    # the old `TournamentTeamMember.objects.filter(user=user).last()`.
    team_ids_by_user = {}
    last_team_name_by_user = {}
    for m in (TournamentTeamMember.objects
              .values("user", "tournament_team", "tournament_team__team__team_name")
              .order_by("user", "id")):
        uid = m["user"]
        team_ids_by_user.setdefault(uid, []).append(m["tournament_team"])
        last_team_name_by_user[uid] = m["tournament_team__team__team_name"]

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
            "team_name": last_team_name_by_user.get(uid),
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

    player_id = request.data.get("player_id")

    if not player_id:
        return Response({"message": "player_id is required"}, status=400)

    player = get_object_or_404(User, user_id=player_id)

    # Shared aggregation (kills/wins/mvps/kdr/avg_damage/win_rate + scrim/tournament splits
    # + booyahs + per_event[] + recent_matches[]). Defensive against null leaderboards.
    agg = compute_player_stats(player, include_breakdown=True)

    # Team + roles (unchanged behaviour: last tournament team for display name, current
    # TeamMembers row for in-game / management role).
    team_member = TournamentTeamMember.objects.filter(user=player).last()
    team_name = team_member.tournament_team.team.team_name if team_member else None
    member = TeamMembers.objects.filter(member=player).first()
    in_game_role = member.in_game_role if member else None
    management_role = member.management_role if member else None

    return Response({
        "player_id": player.user_id,
        "name": player.username,
        "team": team_name,
        "email": player.email,            # admin surface — PII allowed here (auth-gated)
        "uid": player.uid,
        "discord_username": player.discord_username,
        "country": player.country,
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
        "tier_history": tier_history,
        "stats_visible": stats_visible,
    }

    if stats_visible:
        # Full stat block (scalars + per_event + recent_matches), exactly as before.
        stats = compute_player_stats(player, include_breakdown=True)
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
        })

    return Response({"player": payload})