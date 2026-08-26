"""
The OAuth `state` value: a short-lived, single-use, server-side nonce.

WHAT IT REPLACES, AND WHY THAT MATTERED
The existing Discord connect flow used the player's SESSION TOKEN as the state value
(afc_auth/views.py: state = f-string of token plus return_to), and the frontend put that same token
in the query string of the URL it opened. A session token in a URL is copied into the third party's
server logs, the browser's history, and any Referer header. The original author left a comment
saying a short-lived nonce would be better. This is that nonce.

WHERE THE STATE LIVES: the Redis cache (afc/settings.py CACHES default, db 1). Nothing here needs a
database row: the value is meaningless ten minutes after it is minted.

CONSUMED BY: afc_auth/connections/views.py (start mints, callback consumes) and the legacy
afc_auth.views.connect_discord_account / discord_callback pair.
"""
import secrets

from django.core.cache import cache

# Long enough for a human to complete a provider's consent screen, short enough that a leaked value
# is worthless by the time anyone finds it.
TTL_SECONDS = 600

_KEY = "conn_state:{}"


def mint(user_id, provider, return_to, code_verifier=""):
    """Create a nonce standing for "this user started linking this provider, send them back here".

    The nonce itself is an opaque random string: it carries no identity, so a copy of it in a log
    proves nothing and expires anyway. The identity sits in the cache, server side, where the
    player's browser and the provider never see it.
    """
    nonce = secrets.token_urlsafe(32)
    cache.set(
        _KEY.format(nonce),
        {
            "user_id": int(user_id),
            "provider": provider,
            "return_to": return_to,
            "code_verifier": code_verifier,
        },
        timeout=TTL_SECONDS,
    )
    return nonce


def consume(nonce):
    """Resolve a nonce ONCE and delete it. Returns None for unknown, expired or already-used.

    Single use is the point: without the delete, anyone who captured a callback URL could replay it
    and re-link an account. cache.delete after a read is the cheapest correct version of that rule
    here; a race between two simultaneous replays of the SAME nonce would at worst link the same
    account twice, which the uniqueness constraint already refuses.
    """
    if not nonce:
        return None
    key = _KEY.format(nonce)
    payload = cache.get(key)
    if payload is None:
        return None
    cache.delete(key)
    return payload
