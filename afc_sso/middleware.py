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
from django.utils.functional import SimpleLazyObject

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
