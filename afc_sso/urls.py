# backend/afc_sso/urls.py
# Mounted at /sso/ by afc/urls.py. AFC's own views are listed BEFORE the library's
# include so they take precedence; everything else (token, revoke, userinfo,
# discovery, JWKS) is the library's standard surface, unmodified.
# Partners read /sso/.well-known/openid-configuration/ and need nothing else from us.
#
# The `me/` routes are not part of the OIDC surface at all: they are AFC's own player
# API behind the Connected apps page in the profile area, and they authenticate with a
# Bearer SessionToken rather than the /sso/ cookie bridge. See afc_sso/api.py.
from django.urls import include, path

from .api import list_connected_apps, revoke_connected_app
from .views import AFCAuthorizationView

urlpatterns = [
    path("authorize/", AFCAuthorizationView.as_view(), name="authorize"),

    # ── Player-facing: Connected apps (frontend profile area) ──
    path("me/connected-apps/", list_connected_apps, name="connected-apps"),
    path("me/connected-apps/<int:application_id>/", revoke_connected_app,
         name="revoke-connected-app"),

    path("", include("oauth2_provider.urls", namespace="oauth2_provider")),
]
