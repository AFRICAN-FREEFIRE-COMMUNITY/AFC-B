# ──────────────────────────────────────────────────────────────────────────────
# Cutting ONE partner off from ONE player, in one place.
#
# There are two ways a connection ends and they must end it identically, or a player
# who used the less thorough one would still be connected without knowing:
#   * the player presses Remove on the Connected apps page
#     (afc_sso/api.py revoke_connected_app)
#   * the partner calls RP-initiated logout
#     (afc_sso/views.py AFCRPInitiatedLogoutView)
#
# FOUR TABLES, not one, and each one matters:
#   access tokens   the partner's current key to /sso/userinfo/
#   refresh tokens  miss these and the partner silently mints a new access token on its
#                   next refresh, so the disconnection achieves nothing
#   grants          an authorization code the partner has not exchanged yet is still
#                   exchangeable for a brand new token pair
#   id tokens       the issued OIDC identity assertions, which would otherwise be left
#                   orphaned in the table once their access token is deleted
#
# Deleting rather than flagging revoked is the library's own semantics for access tokens
# (AccessToken.revoke() calls self.delete()).
# ──────────────────────────────────────────────────────────────────────────────
from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_id_token_model,
    get_refresh_token_model,
)


def revoke_tokens_for(user, application_id):
    """Delete every credential linking `user` to `application_id`. Returns the counts.

    Returns {"access_tokens": n, "refresh_tokens": n, "grants": n, "id_tokens": n}, the
    shape the Connected apps endpoint hands back to the player and the logout view logs.

    `user=user` is repeated on every queryset on purpose: it is the one line standing
    between this and cutting off somebody else's connection, so it is visible on each
    query rather than hidden in a shared filter.

    IDEMPOTENT BY CONSTRUCTION: a second call, or a call naming a partner this player was
    never connected to, deletes nothing and returns zero counts rather than raising.
    """
    access_tokens = get_access_token_model().objects.filter(
        user=user, application_id=application_id)
    refresh_tokens = get_refresh_token_model().objects.filter(
        user=user, application_id=application_id)
    grants = get_grant_model().objects.filter(
        user=user, application_id=application_id)
    id_tokens = get_id_token_model().objects.filter(
        user=user, application_id=application_id)

    # Counted BEFORE anything is deleted: IDToken is the parent of AccessToken.id_token
    # with on_delete=CASCADE, so deleting first would make the counts lie.
    revoked = {
        "access_tokens": access_tokens.count(),
        "refresh_tokens": refresh_tokens.count(),
        "grants": grants.count(),
        "id_tokens": id_tokens.count(),
    }

    # Order: grants first (nothing depends on them), then refresh tokens (their
    # access_token link is SET_NULL), then access tokens, and finally the id tokens whose
    # children are by then already gone.
    grants.delete()
    refresh_tokens.delete()
    access_tokens.delete()
    id_tokens.delete()

    return revoked
