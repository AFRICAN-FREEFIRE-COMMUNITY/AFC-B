# ──────────────────────────────────────────────────────────────────────────────
# The deletion signal: telling a partner that a player disconnected them.
#
# WHY IT EXISTS: AFC releases real data about real players, some of them minors. When a
# player presses Remove on the Connected apps page, AFC stops answering that partner's
# calls immediately (afc_sso/tokens.py), but the copy of the player's data already sitting
# in the partner's database is beyond AFC's reach. Without a signal the partner never
# learns it should delete it, and the player's "remove" is only half true. This module is
# the other half.
#
# WHAT FIRES IT
#   * afc_sso/api.py revoke_connected_app  - the player pressed Remove. reason
#     "player_revoked".
#   * afc_sso/signals.py                   - the AFC account itself was deleted, by any
#     route (admin, shell, a future delete-account endpoint). reason "account_deleted",
#     one signal per partner the player was connected to.
#
# THE PAYLOAD IS A SIGNED JWT, NOT JSON WITH AN HMAC HEADER, and the reason matters:
#   * AFC CANNOT HMAC with the client secret. django-oauth-toolkit hashes client_secret on
#     save, so AFC does not hold the plaintext and could not compute a shared-secret
#     signature even if it wanted to.
#   * The partner ALREADY has everything needed to verify an RS256 JWT from AFC: they
#     verify the ID token on every sign-in, against the same jwks_uri, matching on the
#     same `kid`. The deletion signal reuses that machinery exactly, so "how do I verify
#     this" has an answer they have already implemented.
#   * Asymmetric signing also means one partner cannot forge a signal to another. A shared
#     secret only proves "somebody who knows this secret sent it".
#
# The token is signed with the SAME key as every ID token
# (OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"]) and carries the same `kid` the JWKS endpoint
# publishes, so a partner's existing key set lookup resolves it without a second source.
#
# THE SUBJECT IS THE PAIRWISE sub, NEVER THE AFC USER ID. Pairwise is the only identifier
# this partner has ever seen (afc_sso/validators.py pairwise_sub), so it is the only one
# they can act on, and sending the raw pk would hand every partner a shared key they could
# use to correlate players with each other. See section 8 of the partner guide.
#
# Shape (compact JWS, RS256), modelled on RFC 8417 Security Event Tokens:
#     {"iss": "<AFC issuer>", "aud": "<partner client_id>", "iat": ..., "jti": "<uuid4>",
#      "sub": "<64-hex pairwise sub>",
#      "events": {"https://africanfreefirecommunity.com/secevent/player-disconnected":
#                   {"subject": {"subject_type": "opaque", "id": "<same sub>"},
#                    "reason": "player_revoked" | "account_deleted"}}}
#
# The event type is AFC-namespaced rather than one of the OpenID RISC URIs, deliberately:
# "the player disconnected this partner" is not any of the RISC events, and claiming a
# RISC type AFC does not implement in full would be a lie a partner might build on.
# ──────────────────────────────────────────────────────────────────────────────
import logging
import uuid

from django.conf import settings
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_refresh_token_model
from oauth2_provider.utils import jwk_from_pem

logger = logging.getLogger(__name__)

# The one event AFC emits today. Namespaced on the AFC domain so it can never collide
# with a RISC type, and stable: partners branch on it.
EVENT_TYPE = "https://africanfreefirecommunity.com/secevent/player-disconnected"

# Why the connection ended. Both mean "delete your copy"; they differ only in whether the
# AFC account still exists, which a partner may want for its own audit trail.
REASON_PLAYER_REVOKED = "player_revoked"
REASON_ACCOUNT_DELETED = "account_deleted"

# RFC 8417 media type. A partner routing on Content-Type can tell this apart from any
# other POST they receive, and it is the correct type for a JWT-bodied security event.
CONTENT_TYPE = "application/secevent+jwt"


def issuer():
    """The `iss` value partners must expect, identical to the ID token's.

    Built the same way the library builds it for discovery: the OIDC issuer setting when
    one is configured, otherwise the API base URL with the /sso mount appended. Kept in
    one function so the signal and the guide cannot disagree about the value.
    """
    configured = settings.OAUTH2_PROVIDER.get("OIDC_ISS_ENDPOINT")
    if configured:
        return configured
    base = (getattr(settings, "AFC_API_BASE_URL", "") or "").rstrip("/")
    return f"{base}/sso" if base else "/sso"


def build_signal(application, user, reason):
    """Sign one disconnection event for one partner. Returns the compact JWS string.

    `user` must still exist when this is called: the pairwise sub is derived from its
    primary key, so an account-deletion signal has to be built BEFORE the row goes (which
    is why afc_sso/signals.py uses pre_delete rather than post_delete).

    Returns "" when the application has no signing key configured, rather than raising:
    a missing OIDC key is a deployment problem, and it must not turn a player's revoke
    into a 500.
    """
    from jwcrypto import jwt as jwcrypto_jwt  # local: only this path needs the dependency

    from .validators import pairwise_sub

    private_key_pem = settings.OAUTH2_PROVIDER.get("OIDC_RSA_PRIVATE_KEY")
    if not private_key_pem:
        logger.warning(
            "sso webhook: no OIDC_RSA_PRIVATE_KEY, cannot sign the disconnect signal "
            "for application #%s", application.pk,
        )
        return ""

    key = jwk_from_pem(private_key_pem)
    sub = pairwise_sub(user, application)

    claims = {
        "iss": issuer(),
        "aud": application.client_id,
        "iat": int(timezone.now().timestamp()),
        # Stable per signal, and stable across retries because the token is signed once
        # and the SAME string is redelivered. A partner can use it to make its handler
        # idempotent, which matters when a retry follows a response AFC never saw.
        "jti": str(uuid.uuid4()),
        "sub": sub,
        "events": {
            EVENT_TYPE: {
                "subject": {"subject_type": "opaque", "id": sub},
                "reason": reason,
            }
        },
    }

    token = jwcrypto_jwt.JWT(
        # `kid` is the key thumbprint, which is exactly what JwksInfoView publishes
        # (site-packages/oauth2_provider/views/oidc.py), so the partner's existing key
        # lookup finds it. `typ` marks it as a security event token per RFC 8417.
        header={"alg": "RS256", "kid": key.thumbprint(), "typ": "secevent+jwt"},
        claims=claims,
    )
    token.make_signed_token(key)
    return token.serialize()


def connected_application_ids(user):
    """Every partner this player currently holds credentials for.

    Used by the account-deletion path, which has to notify all of them. Reads the token
    tables rather than a connection table because there is no connection table: a
    connection IS a live token (see afc_sso/api.py _connection_rows).

    Deliberately looser than the Connected apps list: that page hides expired connections
    because a player does not need to act on them, but a partner whose token merely lapsed
    still holds the player's data and still has to be told to delete it.
    """
    ids = set(
        get_access_token_model().objects.filter(user=user)
        .values_list("application_id", flat=True)
    )
    ids.update(
        get_refresh_token_model().objects.filter(user=user)
        .values_list("application_id", flat=True)
    )
    return {pk for pk in ids if pk is not None}


def notify_disconnected(application, user, reason):
    """Queue the signal for one partner. Never raises, never blocks the caller.

    THIS IS THE FUNCTION THE REST OF AFC CALLS. It is deliberately total: a partner with
    no webhook URL, an unsigned key, a dead broker or an exploding task must not turn a
    player's revoke into an error, because the revoke itself has ALREADY succeeded
    locally by the time this runs. The player is disconnected whatever happens here; the
    signal is a courtesy to the partner, and a failure to deliver it is logged, retried
    (afc_sso/tasks.py) and eventually given up on, in that order.

    Returns True when a delivery was dispatched, False when there was nothing to send.
    """
    try:
        url = (application.deletion_webhook_url or "").strip()
        if not url:
            return False  # the partner never asked for one

        token = build_signal(application, user, reason)
        if not token:
            return False

        from .tasks import dispatch_disconnect_signal
        dispatch_disconnect_signal(application.pk, url, token)
        return True
    except Exception as exc:  # noqa: BLE001 - see the docstring: this can never propagate
        logger.warning(
            "sso webhook: could not queue the disconnect signal for application #%s: %s",
            getattr(application, "pk", None), exc,
        )
        return False
