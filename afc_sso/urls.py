# backend/afc_sso/urls.py
# Mounted at /sso/ by afc/urls.py. AFC's own authorize view is listed BEFORE the
# library's include so it takes precedence; everything else (token, revoke,
# userinfo, discovery, JWKS) is the library's standard surface, unmodified.
# Partners read /sso/.well-known/openid-configuration/ and need nothing else from us.
from django.urls import include, path

from .views import AFCAuthorizationView

urlpatterns = [
    path("authorize/", AFCAuthorizationView.as_view(), name="authorize"),
    path("", include("oauth2_provider.urls", namespace="oauth2_provider")),
]
