# ──────────────────────────────────────────────────────────────────────────────
# The auth bridge for "Sign in with AFC".
#
# WHY THIS EXISTS: django-oauth-toolkit's authorize view reads request.user from a
# Django session. AFC has no Django session - afc_auth login calls authenticate(),
# creates a SessionToken row and returns the token, which the Next.js frontend keeps
# in the `auth_token` cookie. Rather than introduce a SECOND way to be logged in
# (two logout paths, two expiry rules), this middleware resolves the existing token
# into request.user, and ONLY for /sso/ URLs.
#
# It deliberately calls afc_auth.views.validate_token, so expiry and the 3h sliding
# touch() behave identically to every other authenticated AFC request.
# Consumers: oauth2_provider's authorize view, mounted by afc_sso/urls.py.
#
# THE COOKIE ALONE IS NOT ENOUGH IN PRODUCTION (owner report 2026-08-30)
#     That cookie is set with no `domain`, which makes it HOST-ONLY to
#     africanfreefirecommunity.com, so it is never sent to api.africanfreefirecommunity.com
#     where this middleware runs. Signing in from a partner site therefore looped forever:
#     authorize saw nobody, bounced to /login, /login saw a good session and bounced back.
#
#     It went unnoticed because local development runs both halves on 127.0.0.1 with
#     different ports, and COOKIES IGNORE THE PORT. The bridge works on a developer machine
#     and cannot work in production.
#
#     So the cookie is now the SECOND thing tried. The first is a single-use handoff code
#     the frontend puts in the URL, which is exchanged here for a real Django session on
#     this host. See afc_sso/handoff.py for the whole design and why a session is required
#     rather than a one-shot user lookup.
# ──────────────────────────────────────────────────────────────────────────────
from django.contrib.auth import login as auth_login
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponseRedirect
from django.utils import translation
from django.utils.functional import SimpleLazyObject

from afc_auth.locale_middleware import SUPPORTED_LOCALES, get_locale

from .handoff import (
    AUTH_BACKEND,
    HANDOFF_PARAM,
    SSO_SESSION_SECONDS,
    consume_handoff,
)

SSO_PATH_PREFIX = "/sso/"


def _resolve_user(request, session_user):
    """Who this /sso/ request belongs to.

    THE SESSION WINS. `session_user` is whatever AuthenticationMiddleware already worked
    out, which after a handoff exchange is the real logged-in player. Overwriting it with a
    cookie lookup that is guaranteed to fail in production is precisely the bug that made
    the consent POST unreachable, so the session is checked FIRST and returned untouched.

    The cookie remains the fallback because it is what still works in local development,
    where the frontend and the API share 127.0.0.1 and the cookie does reach this host.
    """
    if session_user is not None and session_user.is_authenticated:
        return session_user

    from afc_auth.views import validate_token  # local import: avoids an app-loading cycle

    token = request.COOKIES.get("auth_token") or ""
    if not token:
        return AnonymousUser()
    return validate_token(token) or AnonymousUser()


def _url_without_handoff(request):
    """The current URL with the handoff code stripped, so a spent code never settles in
    browser history or leaks through a Referer header to the partner."""
    params = request.GET.copy()
    params.pop(HANDOFF_PARAM, None)
    query = params.urlencode()
    return f"{request.path}?{query}" if query else request.path


def _exchange_handoff(request, code):
    """Turn a valid handoff code into a real Django session on this host.

    Returns a redirect to the same URL minus the code, ALWAYS: on a bad or expired code
    too. Redirecting either way means a stale code in history degrades to "you are not
    signed in" (the normal bounce to /login) rather than to an error page, and it
    guarantees the code is gone from the address bar before anything is rendered.
    """
    user = consume_handoff(code)
    if user is not None:
        # The SessionToken was already validated when the code was minted, so nothing is
        # re-authenticated here; auth_login is being used for its session, and it needs to
        # be told which backend to record. See handoff.AUTH_BACKEND.
        auth_login(request, user, backend=AUTH_BACKEND)
        # Scoped to the consent interaction, not to the whole site.
        request.session.set_expiry(SSO_SESSION_SECONDS)
    return HttpResponseRedirect(_url_without_handoff(request))


class SSOSessionTokenMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(SSO_PATH_PREFIX):
            # A handoff code short-circuits everything: it is exchanged for a session and
            # the player is redirected to the same URL without it. Only ever on a GET, so a
            # code cannot be spent by the consent form's own POST.
            code = request.GET.get(HANDOFF_PARAM)
            if code and request.method == "GET":
                return _exchange_handoff(request, code)

            # Lazy: an unauthenticated discovery or JWKS hit costs no DB query. The
            # session-derived user is captured now and preferred inside _resolve_user;
            # reading it here rather than in the lambda keeps the laziness intact.
            session_user = getattr(request, "user", None)
            request.user = SimpleLazyObject(
                lambda: _resolve_user(request, session_user)
            )
        return self.get_response(request)


# ──────────────────────────────────────────────────────────────────────────────
# Language activation for the consent screen.
#
# WHY THIS EXISTS: the consent screen is the one player-facing surface AFC renders
# from a DJANGO template, so neither the Next.js catalogs (frontend/messages/*) nor
# the email catalog (afc_auth/email_i18n.py) can reach it. Without this, a French or
# Portuguese player agreed to share their data in a language they may not read.
#
# WHICH SIGNAL: the SAME one the rest of AFC uses, in the same preference order the
# transactional emails use.
#   1. request.user.language - the player's own saved preference. On this screen they
#      are ALWAYS logged in (the view bounces anonymous visitors to /login), so this is
#      the accurate signal, and it is exactly what send_email(language=<recipient>.language)
#      reads. No second mechanism is introduced.
#   2. afc_auth.locale_middleware.get_locale(request) - the Accept-Language locale that
#      LocaleMiddleware already parsed, for the /sso/me/ API routes that authenticate with
#      a Bearer token rather than the cookie bridge above and so have no request.user here.
#
# SCOPE: /sso/ paths ONLY, on purpose. Activating a language process-wide would also
# translate Django's and DRF's own built-in messages on every other API endpoint, which
# would change responses the frontend already handles. This touches one surface.
#
# Consumers: afc_sso/templates/afc_sso/authorize.html ({% trans %}), the SCOPES
# descriptions in afc/settings.py (via afc_sso.claims.describe_scopes), and the refusal
# messages in afc_sso/views.py. Catalogs live in locale/<lang>/LC_MESSAGES/django.po.
# ──────────────────────────────────────────────────────────────────────────────
class SSOLanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(SSO_PATH_PREFIX):
            return self.get_response(request)

        language = _language_for(request)
        translation.activate(language)
        # Django's own convention for "the language this response was rendered in";
        # ConditionalGetMiddleware and the Vary header machinery both look for it.
        request.LANGUAGE_CODE = language
        try:
            return self.get_response(request)
        finally:
            # Workers are reused across requests, so an activated language MUST be
            # released or the next request on this thread inherits it.
            translation.deactivate()


def _language_for(request):
    """The player's language for this /sso/ request: their saved preference, else the
    Accept-Language locale. Always one of SUPPORTED_LOCALES, never raises."""
    try:
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            # user.language may be "", None or a regional tag like "pt-BR"; fold it the
            # same way afc_auth.email_i18n._norm does so the two agree on one code.
            code = (str(getattr(user, "language", "") or "").strip().lower())[:2]
            if code in SUPPORTED_LOCALES:
                return code
    except Exception:
        # A bad cookie or a dead session must not break the authorization flow; the
        # header fallback below still gives the player something readable.
        pass
    return get_locale(request)
