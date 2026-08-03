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
# ──────────────────────────────────────────────────────────────────────────────
from django.contrib.auth.models import AnonymousUser
from django.utils import translation
from django.utils.functional import SimpleLazyObject

from afc_auth.locale_middleware import SUPPORTED_LOCALES, get_locale

SSO_PATH_PREFIX = "/sso/"


def _resolve_user(request):
    from afc_auth.views import validate_token  # local import: avoids an app-loading cycle

    token = request.COOKIES.get("auth_token") or ""
    if not token:
        return AnonymousUser()
    return validate_token(token) or AnonymousUser()


class SSOSessionTokenMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(SSO_PATH_PREFIX):
            # Lazy: an unauthenticated discovery or JWKS hit costs no DB query.
            request.user = SimpleLazyObject(lambda: _resolve_user(request))
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
