# ─────────────────────────────────────────────────────────────────────────────
# afc_tournament_and_scrims/roster_discord.py
#
# Roster Discord verification for the sponsor-engagement registration form
# (owner feature 2026-06-13: "if it was discord it would check their profile,
# see if each player has linked their discord ... and confirm and then return
# feedback").
#
# ONE endpoint lives here: roster_discord_status (POST events/roster-discord-status/).
# Given a list of platform user ids it answers, per player:
#   - has this player CONNECTED Discord on their AFC profile?
#     (User.discord_connected + User.discord_id, set by the Discord OAuth flow
#      in afc_auth.views.connect_discord / discord_callback)
#   - is that Discord account IN the AFC Discord server? (the bot asks the real
#     Discord API via the existing check_discord_membership helper)
#
# HOW IT CONNECTS:
#   - Frontend caller: the registration modal's SPONSOR step
#     (app/(user)/tournaments/[slug]/_components/EventDetailsWrapper.tsx,
#     checkRosterDiscordStatus) fires this once for the selected roster (squad)
#     or the solo registrant whenever the event's sponsorships include a
#     Discord join_group engagement. SponsorEngagementForm.tsx renders the
#     per-player green/red/orange verification panel from the results, and a
#     VERIFIED player's join_group payload is auto-filled with
#     {discord_username, verified: true}.
#   - Reuses check_discord_membership (afc_auth.views, re-exported through
#     afc_tournament_and_scrims.views) - the same helper register_for_event
#     and validate_team_roster_discord already trust.
#   - Unlike validate_team_roster_discord this is intentionally roster-rule
#     free: no event/team/captain checks, ANY authenticated user may ask about
#     a list of user ids. It returns only Discord linkage facts (no PII beyond
#     the public username + discord identity the profile already exposes).
#
# Kept in its own small module (same isolation idiom as event_payments.py /
# event_links.py) because views.py is being worked on in parallel.
# ─────────────────────────────────────────────────────────────────────────────

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc_auth.models import User

# Hard cap on user_ids per call: rosters are 2-6 players, so 20 leaves headroom
# while keeping the per-request Discord API fan-out bounded (best practice:
# never unbounded list work).
MAX_ROSTER_IDS = 20


@api_view(["POST"])
def roster_discord_status(request):
    """
    POST events/roster-discord-status/  ·  per-player Discord readiness check.

    Purpose : lets the registering captain/coach see, for every rostered player
              (or themselves when solo), whether the player has connected
              Discord AND joined the AFC Discord server - BEFORE submitting a
              sponsor join_group(discord) engagement.
    Auth    : house idiom - Authorization: Bearer <session token>
              (validate_token). Any active authenticated user.
    Request : { "user_ids": [1, 2, ...] }   (ints or numeric strings, max 20)
    Response: 200 {
        "results": [{
            "user_id":           <int>,
            "username":          <str|None>,   # platform username
            "discord_connected": <bool>,       # User.discord_connected + discord_id
            "discord_id":        <str|None>,
            "discord_username":  <str|None>,   # stored at OAuth time (may be None)
            "in_server":         <bool|None>,  # None = not connected OR check failed
            "error":             <str|None>    # set when the Discord check failed
                                               # or the user id does not exist
        }, ...],
        "count": <int>
    }
    Errors  : 400 missing/invalid token header or bad user_ids,
              401 expired/unknown token, 403 inactive account.
    Consumer: frontend SPONSOR step (EventDetailsWrapper.checkRosterDiscordStatus
              -> SponsorEngagementForm discord join_group verification panel).
    """
    # Lazy import: views.py re-exports check_discord_membership + validate_token
    # from afc_auth.views (views.py line 13). Importing INSIDE the function keeps
    # this module free of an import-time dependency on the big views.py (owned by
    # parallel work) and dodges any circular-import risk at app load.
    from .views import check_discord_membership, validate_token

    # ── AUTH (house Bearer idiom, mirrors validate_team_roster_discord) ──
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    if user.status != "active":
        return Response({"message": "Your account is not active."}, status=403)

    # ── INPUT: user_ids -> de-duplicated list of ints (order preserved) ──
    raw_ids = request.data.get("user_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return Response({"message": "user_ids is required and must be a non-empty list."}, status=400)

    user_ids = []
    for raw in raw_ids:
        try:
            user_ids.append(int(raw))
        except (TypeError, ValueError):
            return Response({"message": f"Invalid user id: {raw!r}."}, status=400)
    user_ids = list(dict.fromkeys(user_ids))  # dedupe, keep order

    if len(user_ids) > MAX_ROSTER_IDS:
        return Response({"message": f"At most {MAX_ROSTER_IDS} user_ids per request."}, status=400)

    # One query for the whole roster; per-id lookup below keeps request order.
    by_id = {u.user_id: u for u in User.objects.filter(user_id__in=user_ids)}

    # Per-request memo so two roster slots pointing at the same Discord account
    # (shouldn't happen - discord_id is unique - but cheap insurance) only cost
    # one Discord API round trip. Same idiom as validate_team_roster_discord.
    membership_cache = {}

    results = []
    for uid in user_ids:
        u = by_id.get(uid)

        # Unknown id: report it instead of 400-ing the whole roster, so one
        # stale member row can't kill the check for everyone else.
        if u is None:
            results.append({
                "user_id": uid,
                "username": None,
                "discord_connected": False,
                "discord_id": None,
                "discord_username": None,
                "in_server": None,
                "error": "User not found.",
            })
            continue

        connected = bool(u.discord_connected and u.discord_id)

        # in_server: None means "not checkable" - either Discord isn't connected
        # (nothing to look up) or the live Discord API call failed (error set).
        in_server = None
        error = None
        if connected:
            discord_id = str(u.discord_id)
            try:
                if discord_id not in membership_cache:
                    # Live call to the Discord guild-members API (bot token).
                    membership_cache[discord_id] = bool(check_discord_membership(discord_id))
                in_server = membership_cache[discord_id]
            except Exception as e:  # network blip / Discord 5xx - report, don't fail the roster
                in_server = None
                error = f"Discord check failed: {e}"

        results.append({
            "user_id": u.user_id,
            "username": u.username,
            "discord_connected": connected,
            "discord_id": u.discord_id,
            "discord_username": u.discord_username,
            "in_server": in_server,
            "error": error,
        })

    return Response({"results": results, "count": len(results)}, status=200)
