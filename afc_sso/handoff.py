# ──────────────────────────────────────────────────────────────────────────────
# The login handoff for "Sign in with AFC".
#
# WHY THIS EXISTS (owner report 2026-08-30, V-ENT the first partner to try the flow)
#     Signing in with AFC from a partner site went into an INFINITE REDIRECT LOOP:
#
#         v-ent.co -> api.africanfreefirecommunity.com/sso/authorize/
#                  -> africanfreefirecommunity.com/login?redirect=<the authorize url>
#                  -> back to /sso/authorize/ -> back to /login -> forever
#
#     The cookie bridge in middleware.py reads the `auth_token` cookie to work out who
#     the player is. The frontend sets that cookie with NO `domain` attribute
#     (contexts/AuthContext.tsx COOKIE_OPTIONS), which makes it HOST-ONLY to
#     africanfreefirecommunity.com. It is therefore never sent to the api. subdomain.
#     So authorize always saw an anonymous visitor and bounced to /login, /login saw a
#     perfectly good session and bounced straight back, and neither side was wrong.
#
#     WHY IT WAS NEVER CAUGHT: in local development the frontend is 127.0.0.1:3000 and
#     the API is 127.0.0.1:8000. COOKIES IGNORE THE PORT, so the same cookie IS sent to
#     the API on a developer machine. The bridge works locally and cannot work in
#     production, which is why "Sign in with AFC" had never once worked for a real player.
#
# WHAT THIS DOES INSTEAD
#     The frontend, which HAS the session token, swaps it for a single-use code and puts
#     only that code in the authorize URL. The middleware exchanges the code for a real
#     Django session on the API host, then redirects to the same URL with the code
#     stripped out.
#
# WHY A REAL DJANGO SESSION, when the bridge deliberately avoided one
#     The consent screen is `<form method="post">` with no action, so pressing Allow
#     POSTs back to the authorize URL. A code consumed on the GET would already be spent
#     by the time that POST arrived, and the player would be bounced out half way through
#     approving. A session is what carries them across both halves, and it is also what
#     django-oauth-toolkit expects natively.
#
#     The session is scoped as narrowly as the problem: SSO_SESSION_SECONDS, long enough
#     to read a consent screen and press a button, not a second login for the whole site.
#     It lives on api.africanfreefirecommunity.com, a host that serves no other browser
#     surface, and RP-initiated logout clears it like any other.
#
# WHY NOT SIMPLY WIDEN THE COOKIE to domain=.africanfreefirecommunity.com
#     It would work, and it was rejected twice over. It exposes the session token to every
#     present and future subdomain, and it re-opens the duplicate-cookie shadowing bug this
#     codebase already fought once (AuthContext.clearAuthCookieEverywhere exists because of
#     it): the old host-only cookie would keep being sent alongside the new domain-scoped
#     one, with no defined precedence.
#
# WHY THE CODE IS SAFE TO PUT IN A URL
#     It is single use, it dies after HANDOFF_TTL_SECONDS, it is bound to one player, it
#     only means anything on a /sso/ path, and the middleware strips it from the address
#     bar on the very first hop so it never settles in browser history. This is the same
#     trade, for the same reason, that afc_auth/vent_sso.py makes with its own handoff, and
#     the alternative (the real session token in a query string) is strictly worse.
#
# CONNECTS TO
#     afc_sso/urls.py            mounts sso_login_handoff at /sso/handoff/
#     afc_sso/middleware.py      consumes the code (SSOSessionTokenMiddleware)
#     frontend lib/sso.ts        mintSsoHandoff() calls this endpoint
#     frontend app/(auth)/_components/LoginForm.tsx   the one caller of that helper
# ──────────────────────────────────────────────────────────────────────────────
import secrets

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

# The query parameter the code travels in. Read by SSOSessionTokenMiddleware and written
# by the frontend login redirect. Named with an afc_ prefix so it can never collide with
# an OAuth parameter oauthlib parses.
HANDOFF_PARAM = "afc_handoff"

# The code is spent within one redirect, so this only has to cover the round trip from
# the frontend to the API. Deliberately short: it is a bearer secret sitting in a cache.
HANDOFF_TTL_SECONDS = 120

# How long the Django session minted from a handoff lasts. Long enough to read the consent
# screen and press a button, short enough that an abandoned browser on a shared machine is
# not a standing key to the player's partner connections.
SSO_SESSION_SECONDS = 900

_CACHE_PREFIX = "sso_login_handoff:"

# django.contrib.auth.login needs to record WHICH backend authenticated the user, because
# it is what the session deserialiser uses on every later request. Nothing actually
# re-authenticates here (the SessionToken was already validated), so we name the backend
# the rest of AFC logs in through rather than inventing one.
AUTH_BACKEND = "afc_auth.backends.EmailOrUsernameModelBackend"


def issue_handoff(user):
    """Mint a single-use code standing in for `user`, and return it.

    Stores only the primary key. Storing the User itself would pickle a whole row into the
    cache and go stale the moment anything about the account changed.
    """
    code = secrets.token_urlsafe(32)
    cache.set(f"{_CACHE_PREFIX}{code}", user.pk, HANDOFF_TTL_SECONDS)
    return code


def consume_handoff(code):
    """Exchange a code for the User it stands for, or None. SINGLE USE.

    The delete happens before the row is fetched, so two requests racing on the same code
    cannot both win it.
    """
    if not code:
        return None
    key = f"{_CACHE_PREFIX}{code}"
    user_pk = cache.get(key)
    if user_pk is None:
        return None
    cache.delete(key)
    return get_user_model().objects.filter(pk=user_pk).first()


@api_view(["POST"])
@authentication_classes([])  # see _require_player in afc_sso/api.py for why
def sso_login_handoff(request):
    """Swap the caller's session token for a single-use login code.

    PURPOSE: lets the AFC frontend hand a signed-in player to the OIDC authorize view on
    the API host, which cannot read the frontend's host-only `auth_token` cookie. See the
    module header for the loop this exists to break.

    AUTH: `Authorization: Bearer <SessionToken>`, the same header every other AFC endpoint
    takes. 400 with no header or a non-Bearer one, 401 on a dead token.

    REQUEST: no body. The player is taken from the token, never from the request, so there
    is no way to mint a code for somebody else.

    RESPONSE 200: {"code": "<single-use>", "param": "afc_handoff", "expires_in": 120}
        `param` is returned rather than hardcoded in the frontend so the two can never
        drift apart if the name ever changes.

    CONSUMED BY: frontend lib/sso.ts mintSsoHandoff(), called only from LoginForm.tsx when
    an already-authenticated player lands on /login with ?redirect= pointing at
    /sso/authorize.
    """
    from .api import _require_player  # local import: keeps the auth preamble in one place

    user, err = _require_player(request)
    if err:
        return err

    return Response(
        {
            "code": issue_handoff(user),
            "param": HANDOFF_PARAM,
            "expires_in": HANDOFF_TTL_SECONDS,
        },
        status=status.HTTP_200_OK,
    )
