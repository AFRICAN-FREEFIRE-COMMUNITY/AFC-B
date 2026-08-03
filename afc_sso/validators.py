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
from oauth2_provider.oauth2_validators import OAuth2Validator

from .claims import build_claims


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
        return build_claims(user, application, scopes)

    def get_additional_claims(self, request):
        return self._afc_claims(request)

    def get_userinfo_claims(self, request):
        claims = super().get_userinfo_claims(request)
        claims.update(self._afc_claims(request))
        return claims
