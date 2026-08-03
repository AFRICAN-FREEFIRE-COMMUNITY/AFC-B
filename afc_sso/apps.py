# backend/afc_sso/apps.py
# ──────────────────────────────────────────────────────────────────────────────
# afc_sso: AFC acting as an OpenID Connect PROVIDER ("Sign in with AFC").
#
# Wraps django-oauth-toolkit. This app owns three things the library does not:
#   1. AFCSSOApplication  - a partner app row carrying per-org field toggles
#   2. AFCOAuth2Validator - the only place that decides which fields are released
#   3. the auth bridge    - turns AFC's SessionToken into request.user for /sso/
#
# Consumers: partner orgs' websites (via any standard OIDC client library) and,
# later, the AFC admin screens and the player's Connected apps page.
# Design: WEBSITE/tasks/afc-sso-provider-design.md
# ──────────────────────────────────────────────────────────────────────────────
from django.apps import AppConfig


class AfcSsoConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "afc_sso"

    def ready(self):
        # Registers the pre_delete receiver that tells every connected partner when an
        # AFC account is deleted. Imported for its side effect only; see afc_sso/signals.py
        # for why it is a signal rather than a call from a delete-account endpoint.
        from . import signals  # noqa: F401
