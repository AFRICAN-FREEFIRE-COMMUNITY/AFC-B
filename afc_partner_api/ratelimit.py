# afc_partner_api/ratelimit.py
# ──────────────────────────────────────────────────────────────────────────────
# Per-key fixed-window rate limiter. Every read endpoint runs check_rate_limit(key)
# right after authenticate_partner, so a single partner key cannot hammer the API.
#
# Design — fixed window of one wall-clock minute, counted in the shared Redis cache
# (django_redis, db 1 — see afc/settings.py CACHES):
#   • bucket key = "partner_rl:<key_prefix>:<YYYYMMDDHHMM>". The minute stamp IS the
#     window, so when the clock rolls to the next minute we look at a brand-new bucket
#     and the count starts fresh — no sweep/reset job needed.
#   • the bucket is created with a 60s TTL and auto-expires, so stale minutes clean
#     themselves up; Redis never accumulates dead counters.
#   • we count THIS request, then compare to the ceiling, so a limit of N admits
#     exactly N calls per window and rejects the (N+1)th.
#
# Why add()-then-incr() and not a bare cache.incr():
#   django_redis' cache.incr() raises ValueError on a key that doesn't exist yet
#   (verified against the configured RedisCache backend), so the first request of a
#   window would crash. cache.add(bucket, 0, 60) is a no-op if the bucket already
#   exists and a create-with-TTL if it doesn't, so it guarantees the key is present
#   before incr(). incr() does NOT reset the TTL set by add(), so the window expires
#   60s after its FIRST request — true fixed-window semantics (also verified).
# Full spec: WEBSITE/tasks/partner-api-design.md (§7 rate limiting).
# ──────────────────────────────────────────────────────────────────────────────
from django.core.cache import cache
from django.utils import timezone

# Window length / TTL in seconds — one wall-clock minute, matching the minute stamp
# used to build the bucket key. A named constant keeps the two in lockstep.
WINDOW_SECONDS = 60


class RateLimitExceeded(Exception):
    """Raised when a key exceeds its per-minute allowance (-> the view returns 429)."""


def check_rate_limit(key):
    """Count this request against ``key``'s current-minute bucket; raise if over limit.

    ``key`` only needs two attributes: ``.key_prefix`` (identifies the bucket) and
    ``.rate_limit_per_min`` (the ceiling). It is normally a PartnerApiKey row, but any
    object exposing those two attributes works.
    """
    # The minute stamp makes the window self-rotating: a new minute => a new bucket.
    window = timezone.now().strftime("%Y%m%d%H%M")
    bucket = f"partner_rl:{key.key_prefix}:{window}"
    # Ensure the counter exists with a 60s TTL before we incr() it. add() is a no-op
    # when the bucket already exists, so it never clobbers an in-progress count or
    # resets the window's expiry.
    cache.add(bucket, 0, WINDOW_SECONDS)
    # Atomic +1 for THIS request; returns the new running total for the window.
    count = cache.incr(bucket)
    # Ceiling is inclusive: a limit of N admits N calls, the (N+1)th trips the guard.
    if count > key.rate_limit_per_min:
        raise RateLimitExceeded()
