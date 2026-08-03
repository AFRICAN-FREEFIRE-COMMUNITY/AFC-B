# ──────────────────────────────────────────────────────────────────────────────
# Wires AFC's data-release policy into django-oauth-toolkit.
#
# The library calls these hooks when minting an ID token and when answering
# /sso/userinfo/. Both delegate to afc_sso.claims.build_claims so the two payloads
# are always identical.
#
# oidc_claim_scope = None disables the library's own scope-to-claim map. Verified in
# the P0 spike: without it the library filters our claims by its map and our gates
# are silently overruled.
#
# Installed via OAUTH2_PROVIDER["OAUTH2_VALIDATOR_CLASS"] in afc/settings.py, which is
# how every /sso/ endpoint picks it up. Nothing imports it directly.
# ──────────────────────────────────────────────────────────────────────────────
import hashlib

from django.conf import settings
from oauth2_provider.oauth2_validators import OAuth2Validator

from .claims import build_claims


def pairwise_sub(user, application):
    """A stable, opaque subject identifier that DIFFERS per partner application.

    Derived from (SECRET_KEY, application pk, user pk) so it is deterministic without
    a lookup table, reveals nothing about the user, and cannot be reproduced by a
    partner. Keyed on the application's PRIMARY KEY, not its name, so renaming an org
    does not silently orphan every account link on their side.

    Operational warning: this is derived from SECRET_KEY, so rotating SECRET_KEY changes
    every partner's view of every player and breaks all existing account links. If
    SECRET_KEY ever has to rotate, this needs a stored per-application salt first.
    """
    material = f"{settings.SECRET_KEY}:{application.pk}:{user.pk}".encode()
    return hashlib.sha256(material).hexdigest()


class AFCOAuth2Validator(OAuth2Validator):
    oidc_claim_scope = None

    def _afc_claims(self, request):
        # `request` here is oauthlib's Request, not Django's. `client` is the
        # AFCSSOApplication row (confirmed in the spike) and `user` is the player.
        application = getattr(request, "client", None)
        user = getattr(request, "user", None)
        if application is None or user is None:
            return {}
        scopes = request.scopes if isinstance(request.scopes, (list, set, tuple)) else (
            (request.scope or "").split()
        )
        claims = build_claims(user, application, scopes)
        # Overrides the library's default sub (the raw user pk, identical for every org).
        claims["sub"] = pairwise_sub(user, application)
        return claims

    def get_additional_claims(self, request):
        return self._afc_claims(request)

    def get_userinfo_claims(self, request):
        claims = super().get_userinfo_claims(request)
        claims.update(self._afc_claims(request))
        return claims
