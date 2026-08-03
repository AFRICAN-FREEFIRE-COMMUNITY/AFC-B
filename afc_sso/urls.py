# backend/afc_sso/urls.py
# Mounted at /sso/ by afc/urls.py. Everything here is the library's standard OIDC
# surface: authorize, token, revoke, userinfo, discovery, JWKS. Partners read
# /sso/.well-known/openid-configuration/ and need nothing else from us.
from django.urls import include, path

urlpatterns = [
    path("", include("oauth2_provider.urls", namespace="oauth2_provider")),
]
