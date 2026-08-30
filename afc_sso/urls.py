# backend/afc_sso/urls.py
# Mounted at /sso/ by afc/urls.py. AFC's own views are listed BEFORE the library's
# include so they take precedence; everything else (token, revoke, userinfo,
# discovery, JWKS) is the library's standard surface, unmodified.
# Partners read /sso/.well-known/openid-configuration/ and need nothing else from us.
#
# The `me/` routes are not part of the OIDC surface at all: they are AFC's own player
# API behind the Connected apps page in the profile area, and they authenticate with a
# Bearer SessionToken rather than the /sso/ cookie bridge. See afc_sso/api.py.
#
# Neither are the `admin/` routes: they are the AFC-STAFF provisioning surface behind the
# "Sign in with AFC" tab of the admin API Keys page, and they authenticate the same way
# (Bearer SessionToken) with a head_admin / partner_admin gate on top. See
# afc_sso/admin_api.py. They sit under /sso/ so everything about this product is reachable
# from one prefix, and they are declared BEFORE the library include for the same reason
# AFC's own authorize view is.
from django.urls import include, path

from .admin_api import (
    rotate_sso_client_secret,
    sso_application_detail,
    sso_application_logo,
    sso_applications,
    sso_integration_guide,
    sso_scope_catalogue,
    suspend_sso_application,
)
from .api import list_connected_apps, revoke_connected_app
from .brand import brand_kit, brand_logo, brand_logo_svg
from .handoff import sso_login_handoff
from .views import AFCAuthorizationView, AFCRPInitiatedLogoutView

urlpatterns = [
    path("authorize/", AFCAuthorizationView.as_view(), name="authorize"),

    # The login handoff. Declared beside authorize/ because it exists solely to get a
    # signed-in player INTO that view: the frontend swaps its session token for a
    # single-use code, and SSOSessionTokenMiddleware exchanges the code for a real
    # session on this host. Without it the flow loops between /sso/authorize/ and the
    # frontend /login forever, because the auth_token cookie is host-only to the apex
    # domain and never reaches this one. See afc_sso/handoff.py.
    path("handoff/", sso_login_handoff, name="sso-login-handoff"),

    # ── AFC's own brand kit, PUBLIC ──
    # A partner needs the mark and the short name to draw the button that STARTS a
    # sign-in, which is before anyone has signed in, so neither route is gated. Added
    # 2026-08-30 after a partner shipped a "Continue with African Free Fire Community"
    # button with no logo, because AFC published nothing for them to use. See
    # afc_sso/brand.py.
    path("brand/", brand_kit, name="sso-brand-kit"),
    # The VECTOR first, because it is what a partner should reach for. Declared before the
    # sized PNG route so the two cannot be confused by a reader; they do not overlap.
    path("brand/logo.svg", brand_logo_svg, name="sso-brand-logo-svg"),
    path("brand/logo/<int:size>.png", brand_logo, name="sso-brand-logo"),
    # RP-initiated logout. Declared BEFORE the library include for the same reason
    # authorize/ is: AFC's subclass has to win the route. The library's own view deletes
    # the player's tokens at EVERY partner, not just the one asking; ours scopes the
    # disconnection to the requesting application. See afc_sso/views.py.
    path("logout/", AFCRPInitiatedLogoutView.as_view(), name="rp-initiated-logout"),

    # ── Player-facing: Connected apps (frontend profile area) ──
    path("me/connected-apps/", list_connected_apps, name="connected-apps"),
    path("me/connected-apps/<int:application_id>/", revoke_connected_app,
         name="revoke-connected-app"),

    # ── AFC-staff: partner app administration (frontend /a/partners, SSO tab) ──
    # The scope catalogue is declared before the <int:application_id> routes purely for
    # readability; the int converter could never swallow "scopes" anyway.
    path("admin/scopes/", sso_scope_catalogue, name="sso-admin-scopes"),
    # The partner integration guide PDF, built from docs/afc-sso-integration-guide.md and
    # shipped inside this app (afc_sso/docs/). Declared beside the scope catalogue because
    # both are read-only reference downloads rather than per-application routes.
    path("admin/integration-guide/", sso_integration_guide, name="sso-admin-guide"),
    # GET list + POST create share one path; GET detail + PATCH update share another
    # (each @api_view routes by verb, and DRF 405s anything else).
    path("admin/apps/", sso_applications, name="sso-admin-apps"),
    path("admin/apps/<int:application_id>/", sso_application_detail,
         name="sso-admin-app-detail"),
    path("admin/apps/<int:application_id>/suspend/", suspend_sso_application,
         name="sso-admin-app-suspend"),
    # The partner logo AFC hosts itself. Its own route because it is the one multipart
    # upload here: POST replaces the file, DELETE removes it. The detail PATCH above is
    # JSON and cannot carry a file.
    path("admin/apps/<int:application_id>/logo/", sso_application_logo,
         name="sso-admin-app-logo"),
    path("admin/apps/<int:application_id>/rotate-secret/", rotate_sso_client_secret,
         name="sso-admin-app-rotate-secret"),

    path("", include("oauth2_provider.urls", namespace="oauth2_provider")),
]
