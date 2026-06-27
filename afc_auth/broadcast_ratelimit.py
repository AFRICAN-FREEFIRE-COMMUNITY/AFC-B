# afc_auth/broadcast_ratelimit.py
# ──────────────────────────────────────────────────────────────────────────────
# ORGANIZER BROADCAST RATE LIMIT (owner 2026-06-27)
#
# Organizers can email/notify the players registered to their events (the broadcast endpoints in
# afc_tournament_and_scrims: broadcast_announcement / broadcast_to_group / broadcast_to_stage /
# broadcast_match_room_details). To stop a single organizer from spamming players, every NON-ADMIN
# sender is held to TWO limits, both counted in the shared Redis cache (django_redis, same backend as
# afc_partner_api/ratelimit.py and the Mintroute limiter):
#
#   1. HOURLY CAP  — at most RATE_LIMIT_PER_HOUR (5) broadcasts per organizer per clock hour.
#   2. COOLDOWN    — at least COOLDOWN_SECONDS (300 = 5 min) between two consecutive broadcasts.
#
# AFC ADMINS ARE EXEMPT (coarse role admin/moderator/support, or a granular platform-admin role): they
# run the platform and may need to message everyone immediately. The event broadcast endpoints are
# reachable by BOTH admins (is-event-admin) and organizers (org_can_event), so the exemption is decided
# here per-sender, not per-endpoint.
#
# USAGE in an endpoint (after the permission + recipient checks, before deliver_broadcast):
#     allowed, info = check_broadcast_rate(user)
#     if not allowed:
#         return Response({"message": info["message"], "resets_at": info["resets_at"],
#                          "remaining": info["remaining"], "reason": info["reason"]}, status=429)
#     ... deliver_broadcast(...) ...
#     record_broadcast_send(user)            # only AFTER a successful send (so a failed send costs nothing)
#     # optionally surface info to the FE counter via remaining_after_send(user)
#
# The FE (ActionsTab / SendNotificationModal) shows "N of 5 left this hour", a cooldown countdown, and
# on a 429 a toast naming when sending re-opens (resets_at, rendered in the viewer's timezone).
# Mirrors afc_partner_api/ratelimit.py's add()-then-incr() idiom for the same django_redis reason.
# ──────────────────────────────────────────────────────────────────────────────
from django.core.cache import cache
from django.utils import timezone

# Tunables — kept as named constants so the endpoint code + the FE copy stay in lockstep with one edit.
RATE_LIMIT_PER_HOUR = 5      # max broadcasts per organizer per clock hour
COOLDOWN_SECONDS = 300       # min gap between two consecutive broadcasts (5 minutes)

# Granular admin roles that, like the coarse roles, are EXEMPT from the organizer broadcast limits.
_BROADCAST_ADMIN_ROLES = ("head_admin", "super_admin", "event_admin", "organizer_admin", "metrics_admin")


def is_broadcast_admin(user) -> bool:
    """True for AFC ADMINS (exempt from the limits): coarse role admin/moderator/support, or a granular
    _BROADCAST_ADMIN_ROLES role. Organizers (and everyone else) are NOT admins here and ARE limited."""
    if not user:
        return False
    if getattr(user, "role", None) in ("admin", "moderator", "support"):
        return True
    try:
        return user.userroles.filter(role__role_name__in=_BROADCAST_ADMIN_ROLES).exists()
    except Exception:
        return False


def _hour_key(user_id):
    """Per-user fixed clock-hour bucket key. The hour stamp self-rotates the window (new hour => new
    bucket, count starts fresh) so no reset job is needed."""
    return f"bc_hr:{user_id}:{timezone.now().strftime('%Y%m%d%H')}"


def _cooldown_key(user_id):
    return f"bc_cd:{user_id}"


def _next_hour_iso():
    """ISO timestamp of the next clock-hour boundary — when the hourly bucket rolls over (resets_at)."""
    now = timezone.now()
    # zero out minutes/seconds, add one hour
    nxt = now.replace(minute=0, second=0, microsecond=0) + timezone.timedelta(hours=1)
    return nxt.isoformat()


def check_broadcast_rate(user):
    """Decide whether `user` may send a broadcast RIGHT NOW. Read-only (does NOT consume a slot — call
    record_broadcast_send AFTER a successful send).

    Returns (allowed: bool, info: dict). `info` always carries:
      • remaining   — broadcasts left in the current hour (after this one would be 0 when blocked by cap)
      • limit       — RATE_LIMIT_PER_HOUR
      • exempt      — True for admins (no limits apply)
    and WHEN BLOCKED additionally:
      • reason      — "cooldown" | "hourly"
      • resets_at   — ISO time when sending re-opens (cooldown end, or the next hour boundary)
      • message     — a human sentence the FE can show directly
    """
    # Admins bypass every limit.
    if is_broadcast_admin(user):
        return True, {"exempt": True, "remaining": RATE_LIMIT_PER_HOUR, "limit": RATE_LIMIT_PER_HOUR}

    uid = user.user_id

    # 1) Cooldown: a non-expired cooldown key means the last send was < COOLDOWN_SECONDS ago. We stored
    #    the cooldown-END ISO as the value so we can report exactly when it lifts without a TTL read.
    cd_until = cache.get(_cooldown_key(uid))
    if cd_until:
        return False, {
            "exempt": False,
            "reason": "cooldown",
            "resets_at": cd_until,
            "remaining": _remaining(uid),
            "limit": RATE_LIMIT_PER_HOUR,
            "message": "You're sending broadcasts too quickly. Please wait a few minutes before the next one.",
        }

    # 2) Hourly cap.
    count = cache.get(_hour_key(uid), 0) or 0
    if count >= RATE_LIMIT_PER_HOUR:
        return False, {
            "exempt": False,
            "reason": "hourly",
            "resets_at": _next_hour_iso(),
            "remaining": 0,
            "limit": RATE_LIMIT_PER_HOUR,
            "message": f"You've reached your limit of {RATE_LIMIT_PER_HOUR} broadcasts this hour. "
                       f"You'll be able to send again at the top of the next hour.",
        }

    return True, {"exempt": False, "remaining": RATE_LIMIT_PER_HOUR - count, "limit": RATE_LIMIT_PER_HOUR}


def record_broadcast_send(user):
    """Consume one slot for `user` — call ONLY after a broadcast actually went out (so a failed/empty
    send never costs the organizer a slot). No-op for admins. Stamps the 5-min cooldown and increments
    the hourly counter. Returns the remaining hourly allowance after this send (for the FE counter)."""
    if is_broadcast_admin(user):
        return RATE_LIMIT_PER_HOUR

    uid = user.user_id

    # Cooldown: store the END time as the value with a matching TTL, so check_broadcast_rate can report
    # resets_at directly. The window expires on its own after COOLDOWN_SECONDS.
    cooldown_end = (timezone.now() + timezone.timedelta(seconds=COOLDOWN_SECONDS)).isoformat()
    cache.set(_cooldown_key(uid), cooldown_end, COOLDOWN_SECONDS)

    # Hourly counter: add()-then-incr() (django_redis incr() raises on a missing key). TTL of one hour
    # so the bucket cleans itself up; the hour stamp in the key still rotates the window precisely.
    hk = _hour_key(uid)
    cache.add(hk, 0, 3600)
    try:
        new_count = cache.incr(hk)
    except ValueError:
        cache.set(hk, 1, 3600)
        new_count = 1
    return max(0, RATE_LIMIT_PER_HOUR - new_count)


def _remaining(user_id):
    """Hourly broadcasts left for this user right now (never negative). Used to enrich responses."""
    count = cache.get(_hour_key(user_id), 0) or 0
    return max(0, RATE_LIMIT_PER_HOUR - count)


def broadcast_rate_status(user):
    """Snapshot for the FE counter WITHOUT consuming a slot: {remaining, limit, cooldown_until, exempt}.
    cooldown_until is the ISO time the current cooldown lifts (or None). Lets the send UI show how many
    are left + a live cooldown countdown before the organizer even hits send."""
    if is_broadcast_admin(user):
        return {"exempt": True, "remaining": RATE_LIMIT_PER_HOUR, "limit": RATE_LIMIT_PER_HOUR, "cooldown_until": None}
    uid = user.user_id
    return {
        "exempt": False,
        "remaining": _remaining(uid),
        "limit": RATE_LIMIT_PER_HOUR,
        "cooldown_until": cache.get(_cooldown_key(uid)),
    }
