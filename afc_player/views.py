from django.shortcuts import render
from django.shortcuts import get_object_or_404
from django.db.models import Sum, Count
from rest_framework import status
from rest_framework.response import Response
from rest_framework.decorators import api_view

from afc_auth.models import User, BannedPlayer
from afc_team.models import TeamMembers
from afc_tournament_and_scrims.models import Match, TournamentPlayerMatchStats
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db.models import Sum
from afc_auth.models import User
from afc_tournament_and_scrims.models import (
    TournamentPlayerMatchStats,
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
    compute_registered_events,
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
    # validate_token returns None for unknown / expired tokens - exactly the
    # anonymous-viewer behaviour we want, so no extra guarding is needed.
    return validate_token(token)


def _can_view_player_stats(viewer, player):
    """
    Decide whether `viewer` (a User or None) may see `player`'s INDIVIDUAL stats.

    Owner rule (2026-06-24 lockdown + 2026-06-27 per-user opt-in): individual player statistics are
    PRIVATE BY DEFAULT. Visible to:
      • the viewer themselves (own profile) - always, regardless of any preference, and
      • AFC admins (is_stats_admin: role admin/moderator/support or a granular platform-admin role) - 
        always, they override the user's choice, and
      • ANY other viewer (teammates, other players, organizers, sponsors, the public, anonymous) ONLY
        when the player has OPTED IN via their profile switch (player.stats_visible == True).

    So the default (stats_visible False) reproduces the original lockdown exactly - only self + admins.
    Flipping the switch on opens the individual stats to everyone else. anonymous (viewer None) can see
    them too once opted in (a public profile), since the stats are then explicitly public.

    Query cost: O(1) - own-id check, is_stats_admin (one indexed UserRoles existence check at most),
    then a boolean field read.
    """
    # Own profile - always full visibility, even if the user hides stats from others.
    if viewer is not None and viewer.user_id == player.user_id:
        return True

    # AFC admins (NOT organizers/sponsors) always see full stats - they override the user's choice.
    # Single source of truth shared with the team-stats gate so both surfaces agree on who is an admin.
    if viewer is not None and is_stats_admin(viewer):
        return True

    # Everyone else (including anonymous viewers) sees the stats ONLY if the player opted in.
    return bool(player.stats_visible)


# ──────────────────────────────────────────────────────────────────────────────
# TOURNAMENT WINNINGS (per-player prize history)
# ──────────────────────────────────────────────────────────────────────────────
# These rows are PlayerWinning records written by afc_rankings.admin_prize.prize_create
# (_distribute_payout) whenever an admin/organizer records a team/solo prize - the team
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



def _player_list_rows(include_identity=False):
    """The player-list rows both list endpoints below return.

    ONE builder, two callers, because the two lists must never disagree about a player's team,
    kills or ban state - the only difference between them is whether the row carries the
    account's LOGIN IDENTIFIERS:

      • get_all_users            (admin only)  include_identity=False
      • admin_list_players       (admin only)  include_identity=True

    `include_identity` adds `uid` and `email`. It exists so the identifiers are opt-IN at the
    call site: a future edit to the shared aggregate code cannot leak them into the public
    endpoint by accident, because the keys are only ever written under this flag.

    PERFORMANCE (why this looks like this):
    The original version ran ~6-8 ORM queries PER user inside a Python for-loop. With ~6k users
    that is an N+1 explosion of ~40k queries (the endpoint took 30-45s and the admin page never
    finished loading). It is a fixed handful of GROUPED/bulk queries assembled in memory, and
    `include_identity` adds two COLUMNS to the same single user query, never another query.
    """
    # Only the columns the rows actually use. uid/email are fetched ONLY when they will be
    # returned, so the public path cannot accidentally hold them in memory either.
    fields = ["user_id", "username", "status", "role"]
    if include_identity:
        fields += ["uid", "email"]
    users = list(User.objects.all().only(*fields))

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

    # ── wins per player: one grouped count over the player's OWN match lines ──────────────────
    # A win is a match THIS PLAYER was fielded in whose team line placed 1st. This used to sum
    # placement-1 rows across every tournament team the player had ever appeared on a roster for,
    # which credited a player fielded in one match with all four of their team's wins, and made
    # this column disagree with the "Wins" on the same player's detail page and public profile
    # (owner bug 2026-08-07 - the same population mix-up that printed a 400% win rate). Counting
    # the player's own rows makes all three surfaces report one number.
    wins_by_user = {
        row["player"]: row["c"]
        for row in TournamentPlayerMatchStats.objects.filter(team_stats__placement=1)
        .values("player").annotate(c=Count("pk"))
    }

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

    # ── active bans: one set lookup (was: an .exists() per user) ──
    banned_ids = set(
        BannedPlayer.objects.filter(is_active=True).values_list("banned_player", flat=True)
    )

    data = []
    for user in users:
        uid = user.user_id
        # total_wins = matches THIS player was fielded in whose team placed 1st (see wins_by_user)
        total_wins = wins_by_user.get(uid, 0)
        row = {
            "user_id": uid,
            "name": user.username,
            # CURRENT team from the live roster (was the stale tournament-history name).
            "team_name": current_team_name_by_user.get(uid),
            "total_kills": kills_by_user.get(uid, 0),
            "total_wins": total_wins,
            "total_mvps": mvps_by_user.get(uid, 0),
            "status": "banned" if uid in banned_ids else user.status,
            "role": user.role  # optional but useful
        }
        if include_identity:
            # Free Fire UID and email. Admin-only: see admin_list_players for why these two keys
            # can never ride on the public endpoint.
            row["uid"] = user.uid or ""
            row["email"] = user.email or ""
        data.append(row)

    return data


def _admin_or_refusal(request):
    """(admin_user, None) for a staff caller, or (None, Response) to send back.

    ONE gate for both list endpoints below, so they can never disagree about who counts as staff.

    The predicate - base role "admin" OR any granular UserRoles row - is the same one
    afc_auth.views.search_users uses to decide who may match a user by email, which keeps the two
    "look a player up" surfaces consistent. It is NOT head-admin: reading the player list is
    ordinary admin work. The identity EDIT controls (afc_auth/views_admin_identity.py) keep their
    narrower require_head_admin gate.

    "No token", "expired token" and "not an admin" deliberately return the SAME 401 body, so the
    endpoint cannot be used to test whether a stolen token belongs to staff.
    """
    auth = request.headers.get("Authorization") or ""
    user = validate_token(auth.split(" ", 1)[1].strip()) if auth.startswith("Bearer ") else None
    if not user or not (user.role == "admin" or user.userroles.exists()):
        return None, Response({"message": "Admin access required."},
                              status=status.HTTP_401_UNAUTHORIZED)
    return user, None


@api_view(["GET"])
def get_all_users(request):
    """
    GET /player/get-all-players/  Bearer auth, ADMIN ONLY (gated 2026-08-11, owner request).

    EVERY user with lightweight aggregate stats (total_kills / total_wins / total_mvps), their
    current team name, and ban/role status.

    WHY IT IS NOW GATED
      It used to be open, and on production it answered ANY anonymous caller with ~6,800 accounts:
      1,070,415 bytes of usernames, teams, roles and ban status. Nothing about that list is public
      information - a ban status in particular is a moderation record - and a full member roster is
      exactly the input an attacker wants before trying passwords, since a username is one of the
      three things sign-in accepts (afc_auth/backends.py). It is now behind the same admin gate as
      admin_list_players; the two differ ONLY in whether the row carries uid + email.

    CONSUMED BY  frontend app/(a)/a/rankings/page.tsx and app/(a)/a/rankings/results/page.tsx,
                 both admin-only screens, which send the viewer's token. (app/sitemap.ts mentions
                 this endpoint in a comment explaining why it does NOT list players; it never
                 calls it, so gating this changes nothing for crawlers.)
                 The admin Players tab reads admin_list_players below instead.

    RESPONSE  200 { users: [ {user_id, name, team_name, total_kills, total_wins, total_mvps,
                             status, role} ] }
              401 missing/invalid token or not an admin (ONE body for both).

    total_wins is the ONE number that deliberately no longer matches the original loop: it
    used to be the team's wins, not the player's, and disagreed with the same player's detail
    page. See wins_by_user in _player_list_rows (owner bug 2026-08-07).

    ⚠ Still do NOT add a login identifier (uid, email, phone) here. Authentication changed who can
    read this list, not what belongs in it: callers that need the identifiers have
    admin_list_players, and afc_player/tests_admin_player_list.py fails if they appear here.
    """
    _admin, refusal = _admin_or_refusal(request)
    if refusal:
        return refusal

    return Response({"users": _player_list_rows()})


@api_view(["GET"])
def admin_list_players(request):
    """
    GET /player/admin/list-players/  Bearer auth, ADMIN ONLY.

    PURPOSE
      The same player list as get_all_users PLUS the two identifiers support actually gets given
      on a ticket: the Free Fire `uid` and the account `email`. It exists so an admin can find a
      player by the UID they quote, which was impossible before: the Players tab filtered on the
      in-game name alone.

    WHY IT IS A SEPARATE ENDPOINT (owner 2026-08-11)
      When this was written get_all_users was UNAUTHENTICATED and, on production, returned ~6,800
      accounts to anyone who asked. `User.uid` is a LOGIN IDENTIFIER - afc_auth/backends.py
      EmailOrUsernameModelBackend resolves one typed string against username OR uid OR email - so
      adding uid/email there would have published two of the three ways to name every account on
      the site (the same defect fixed in afc_team.views_join_requests on 2026-08-08).

      get_all_users has SINCE been gated too, so the split is no longer what keeps the identifiers
      safe. It is kept anyway, because the two answer different questions: this one is "find a
      specific person", and identifiers are handed out only where that is the job. A caller that
      just needs names and stats still gets a response with no identifiers in it at all.

    REQUEST   no body.
    RESPONSE  200 { users: [ {user_id, name, team_name, total_kills, total_wins, total_mvps,
                             status, role, uid, email} ] }
              401 missing/invalid token or not an admin (ONE body for both - see below).

    AUTH      A valid SessionToken whose user is an admin: base role "admin" OR any granular
              UserRoles row. This is the SAME predicate afc_auth.views.search_users uses for
              deciding who may match on email, so the two admin lookups agree on who counts as
              staff. Not head-admin: this is the list every admin already reads, only now
              authenticated. The EDIT controls (afc_auth/views_admin_identity.py) stay
              head-admin only.
              "No token", "expired token" and "not an admin" all return the SAME 401 body, so the
              endpoint cannot be used to test whether a stolen token belongs to staff.

    CONSUMED BY  frontend app/(a)/a/_components/PlayersAdminContent.tsx (the Players tab of
                 /a/teams), which searches name + UID + email client-side and shows a UID column.
    """
    _admin, refusal = _admin_or_refusal(request)
    if refusal:
        return refusal

    return Response({"users": _player_list_rows(include_identity=True)})


@api_view(["POST"])
def get_player_details(request):
    # ADMIN player profile (keyed by player_id). The heavy stat aggregation now lives in
    # afc_player.aggregation.compute_player_stats so the public player page can reuse the
    # EXACT same numbers (single source of truth, no drift). This response keeps every key
    # it returned before - the shared helper produces the same scalar names - and additionally
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
        "email": player.email,            # admin surface - PII allowed here (auth-gated)
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

        # The TEAM record (every match of every team this player was rostered on), kept
        # separate from the player's own total_wins/win_rate above. See the two-statistics
        # note in afc_player.aggregation.compute_player_stats. The former scrim_booyah /
        # tournament_booyah keys are gone: they were a second name for scrims_wins /
        # tournaments_wins and this page summed the two, reporting double (owner 2026-08-07).
        "team_matches": agg["team_matches"],
        "team_wins": agg["team_wins"],
        "team_win_rate": agg["team_win_rate"],

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
      • a NON-sensitive identity block (NO email / no PII)        - basic_player_profile()
      • the player's published tier / rank history per season     - player_tier_history()
      • the SAME aggregated stats as the admin endpoint           - compute_player_stats()
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
    (truthful empty state - nothing is fabricated). A player on no team returns
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
        # Events the player is CURRENTLY registered for (upcoming/ongoing), solo + squad
        # (owner 2026-06-30). PUBLIC schedule data, so it sits OUTSIDE the stats_visible
        # branch below and is returned to every viewer. See aggregation.compute_registered_events;
        # rendered by PlayerClient.tsx's "Registered Events" section.
        "registered_events": compute_registered_events(player),
    }

    # ── Esport image for ADMINS (owner 2026-07-02: "if I go to a person's profile as an admin I
    # should see their esport image"). Broadcast-media asset, not public PII - exposed ONLY to
    # stats admins (same gate the sensitive stats use for staff). Rendered by PlayerClient.tsx
    # next to the avatar when present. esports_pic lives on UserProfile (see afc_auth.models).
    if viewer is not None and is_stats_admin(viewer):
        from afc_auth.models import esports_pic_url
        payload["esport_image"] = esports_pic_url(player, request)

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
            # The TEAM record, named separately from the player's own total_wins/win_rate
            # above (see compute_player_stats). Replaces the removed scrim_booyah /
            # tournament_booyah, which only ever mirrored scrims_wins / tournaments_wins.
            "team_matches": stats["team_matches"],
            "team_wins": stats["team_wins"],
            "team_win_rate": stats["team_win_rate"],
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
            "team_matches": 0,
            "team_wins": 0,
            "team_win_rate": 0,
            "per_event": [],
            "recent_matches": [],
            # Prize winnings are private too: zero the total + empty the list for unauthorized
            # viewers (same back-compat contract as the sensitive numbers above).
            "total_earnings_ngn": "0",
            "tournament_winnings": [],
        })

    return Response({"player": payload})