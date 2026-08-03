# ──────────────────────────────────────────────────────────────────────────────
# The PLAYER's side of "Sign in with AFC": Connected apps.
#
# The consent screen promises, in so many words, "You can remove this at any time
# from Connected apps in your AFC profile" (afc_sso/templates/afc_sso/authorize.html).
# This module is the API behind that promise: one endpoint listing the partner orgs a
# player is signed in to, one endpoint that cuts a single org off.
#
# CONSUMED BY: the Connected apps page in the player's profile area of the Next.js
# frontend. Nothing else calls these two endpoints.
#
# HOW IT CONNECTS TO THE REST OF THE SYSTEM
#   - afc_sso stores no "connection" table of its own. A connection IS the set of live
#     django-oauth-toolkit tokens, so this module reads oauth2_provider's AccessToken,
#     RefreshToken, Grant and IDToken tables, always filtered to the calling player.
#   - The plain-language lines shown to the player come from afc_sso.claims.describe_scopes,
#     the SAME function the consent screen uses (afc_sso/views.py, get_context_data). That
#     is deliberate: what a player was promised when they clicked Allow and what this page
#     tells them they gave away can never drift apart.
#   - The org identity fields (display_name, logo_url, homepage_url) live on
#     afc_sso.models.AFCSSOApplication, which is swapped in for the library's own
#     Application model via OAUTH2_PROVIDER_APPLICATION_MODEL (afc/settings.py), so
#     get_application_model() and every token's `.application` resolve to it.
#
# AUTH HERE DIFFERS FROM THE REST OF afc_sso, ON PURPOSE
# Every other /sso/ view is opened by a browser mid-OAuth and gets request.user from the
# cookie bridge (afc_sso/middleware.py). These two are called by the AFC frontend with an
# `Authorization: Bearer <SessionToken>` header like every other AFC API endpoint, so they
# use the house preamble copied from afc_auth.views.get_user_profile and ignore
# request.user completely. See _require_player below for why authentication_classes is
# emptied rather than left at the DRF default.
# ──────────────────────────────────────────────────────────────────────────────
from django.conf import settings
from django.utils import timezone
from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_id_token_model,
    get_refresh_token_model,
)
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from .claims import describe_scopes

# `openid` is on every single connection and releases nothing but the pairwise sub, so it
# is noise on a screen whose job is to tell the player what a partner can see about them.
# describe_scopes drops it for the same reason on the consent screen.
HIDDEN_SCOPES = {"openid"}


def _require_player(request):
    """The house auth preamble, lifted from afc_auth.views.get_user_profile (line ~2934).

    Returns (user, None) on success or (None, Response) to return straight to the caller,
    the same shape as afc_partner_api.views_admin._require_partner_admin. Status codes match
    the rest of the AFC API: 400 for a missing or malformed Authorization header (the caller
    sent a bad request), 401 for a well-formed token that is expired or unknown.

    WHY BOTH VIEWS CARRY @authentication_classes([]): these endpoints sit under /sso/, and
    SSOSessionTokenMiddleware sets request.user for every /sso/ path from the `auth_token`
    cookie. DRF's default SessionAuthentication would then see an authenticated
    request._request.user and run its CSRF check, which fails the DELETE with a 403 for any
    caller whose browser also sends that cookie (it does whenever frontend and API share a
    host, as they do in local dev). These two views authenticate off the Bearer token and
    nothing else, so DRF's authenticators are switched off rather than fought.
    """
    session_token = request.headers.get("Authorization")
    if not session_token:
        return None, Response(
            {"status": "error", "message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not session_token.startswith("Bearer "):
        return None, Response(
            {"status": "error", "message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from afc_auth.views import validate_token  # local import: avoids an app-loading cycle

    user = validate_token(session_token.split(" ")[1])
    if not user:
        return None, Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return user, None


def _iso(moment):
    return moment.isoformat() if moment else None


def _earliest(current, candidate):
    if candidate is None:
        return current
    return candidate if current is None or candidate < current else current


def _latest(current, candidate):
    if candidate is None:
        return current
    return candidate if current is None or candidate > current else current


def _connection_rows(user):
    """Group this player's live tokens into one row per partner org.

    WHAT COUNTS AS CONNECTED (the decision, stated once so the tests can assert it):
    an org is listed when the player holds EITHER an unexpired access token for it OR a
    refresh token that has not been revoked. A refresh token is the stronger of the two:
    AFC leaves REFRESH_TOKEN_EXPIRE_SECONDS unset (afc/settings.py), so an unrevoked
    refresh token lets the partner mint a fresh access token forever. Hiding an org whose
    access token merely lapsed would tell the player they are disconnected while the
    partner still has the keys, and would leave them no button to press.

    An org whose tokens have ALL expired or been revoked is not listed. It is not a live
    connection, and nothing an org holds can be used to read the player's data.
    """
    now = timezone.now()

    # Two queries, all tokens (expired ones included) so `granted_at` can reach back to when
    # the connection actually began. select_related keeps the org lookup off the per-row path.
    access_tokens = (
        get_access_token_model()
        .objects.filter(user=user)
        .select_related("application")
    )
    refresh_tokens = (
        get_refresh_token_model()
        .objects.filter(user=user)
        .select_related("application")
    )

    connections = {}

    def slot(application):
        return connections.setdefault(application.pk, {
            "application": application,
            "live": False,
            "granted_at": None,
            "last_used_at": None,
            "expires_at": None,
            "live_scopes": set(),
            # Scope of the NEWEST access token whatever its expiry, kept as the fallback for
            # a refresh-token-only connection: RefreshToken carries no scope column of its
            # own, and a refresh re-mints the scopes the last access token carried.
            "latest_scope": "",
            "latest_access_created": None,
        })

    for token in access_tokens:
        if token.application is None:
            continue
        row = slot(token.application)
        row["granted_at"] = _earliest(row["granted_at"], token.created)
        # AFC does not log every userinfo call, so the newest token issuance (the first
        # exchange, or the most recent refresh) is the closest record of "last used" there is.
        row["last_used_at"] = _latest(row["last_used_at"], token.created)
        if row["latest_access_created"] is None or token.created > row["latest_access_created"]:
            row["latest_access_created"] = token.created
            row["latest_scope"] = token.scope or ""
        if token.expires and token.expires > now:
            row["live"] = True
            row["live_scopes"].update((token.scope or "").split())
            row["expires_at"] = _latest(row["expires_at"], token.expires)

    for token in refresh_tokens:
        if token.application is None:
            continue
        row = slot(token.application)
        row["granted_at"] = _earliest(row["granted_at"], token.created)
        row["last_used_at"] = _latest(row["last_used_at"], token.created)
        if token.revoked is None:
            row["live"] = True

    catalogue = settings.OAUTH2_PROVIDER["SCOPES"]
    rows = []
    for row in connections.values():
        if not row["live"]:
            continue
        application = row["application"]
        granted = row["live_scopes"] or set(row["latest_scope"].split())
        # Filtered and sorted with the same rule describe_scopes applies, so scope_codes[i]
        # is always the machine code for scopes[i]. The frontend can zip the two lists.
        codes = sorted(s for s in granted if s in catalogue and s not in HIDDEN_SCOPES)
        rows.append({
            "application_id": application.pk,
            "name": application.display_name or application.name,
            "logo_url": application.logo_url or "",
            "homepage_url": application.homepage_url or "",
            "scopes": describe_scopes(codes),
            "scope_codes": codes,
            "granted_at": _iso(row["granted_at"]),
            # expires_at is when the partner's CURRENT access token lapses, not the end of
            # the connection: with a live refresh token they simply mint another. It is null
            # for a connection whose access token has already lapsed.
            "expires_at": _iso(row["expires_at"]),
            "last_used_at": _iso(row["last_used_at"]),
            "_sort_key": row["last_used_at"],
        })

    # Most recently active org first, which is the order a player scanning the page expects.
    rows.sort(key=lambda r: (r["_sort_key"] is not None, r["_sort_key"]), reverse=True)
    for r in rows:
        r.pop("_sort_key")
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# 1) list_connected_apps  (GET sso/me/connected-apps/)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def list_connected_apps(request):
    """Every partner org the calling player is currently signed in to.

    PURPOSE: fills the Connected apps page in the player's AFC profile, which exists so a
    player can see who holds a "Sign in with AFC" connection to their account and what that
    org can read about them.

    AUTH: `Authorization: Bearer <SessionToken>`, validated by afc_auth.views.validate_token
    (see _require_player). 400 with no header or a non-Bearer header, 401 on a bad token.

    REQUEST: no body, no query parameters. The player is taken from the token, never from
    the request, so there is no way to ask for somebody else's list.

    RESPONSE 200:
        {"apps": [
            {"application_id": 3,
             "name": "Partner Org",                      # display_name, falling back to name
             "logo_url": "https://cdn.partner.test/l.png",   # "" when the org set none
             "homepage_url": "https://partner.test",         # "" when the org set none
             "scopes": ["Your Free Fire UID", "Your in-game name, avatar, country and language"],
             "scope_codes": ["afc.freefire", "profile"],     # parallel to `scopes`
             "granted_at": "2026-08-01T10:00:00+00:00",
             "expires_at": "2026-08-03T11:00:00+00:00",      # null once the access token lapses
             "last_used_at": "2026-08-03T10:00:00+00:00"}
        ]}
    `apps` is [] for a player who has never used "Sign in with AFC". One entry per org no
    matter how many tokens that org holds. Ordered by last_used_at, newest first.

    The `scopes` lines are generated by afc_sso.claims.describe_scopes, the same function
    behind the consent screen, so the wording a player reads here is word for word what they
    approved. See _connection_rows for which tokens count as a live connection.
    """
    user, err = _require_player(request)
    if err:
        return err

    return Response({"apps": _connection_rows(user)}, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# 2) revoke_connected_app  (DELETE sso/me/connected-apps/<application_id>/)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["DELETE"])
@authentication_classes([])
def revoke_connected_app(request, application_id):
    """Cut one partner org off from the calling player's AFC account.

    PURPOSE: the Remove button on the Connected apps page, and the thing the consent screen
    promises exists.

    AUTH: identical to list_connected_apps, `Authorization: Bearer <SessionToken>`.

    REQUEST: no body. `application_id` is the AFCSSOApplication primary key, exactly the
    `application_id` returned by the list endpoint above.

    RESPONSE 200:
        {"message": "Connection removed.",
         "application_id": 3,
         "revoked": {"access_tokens": 2, "refresh_tokens": 1, "grants": 1, "id_tokens": 2}}

    FOUR TABLES, not one, and each one matters:
      - access tokens  the partner's current key to /sso/userinfo/
      - refresh tokens miss these and the partner silently mints a new access token on its
                       next refresh, so the player's Remove click achieves nothing
      - grants         an authorization code the partner has not exchanged yet is still
                       exchangeable for a brand new token pair
      - id tokens      the issued OIDC identity assertions, which would otherwise be left
                       orphaned in the table once their access token is deleted
    Deleting rather than flagging revoked is the library's own semantics for access tokens
    (AccessToken.revoke() calls self.delete()).

    IDEMPOTENT AND SAFE BY CONSTRUCTION: every queryset is filtered by `user=user` as well as
    the application, so a player can only ever delete their own rows, and a second call (or a
    call naming an org they never connected to) deletes nothing and still returns 200 with
    zero counts rather than a 404 or a 500.

    NOT IN SCOPE HERE: AFCSSOApplication.deletion_webhook_url, the outbound "this player left"
    signal from section 7b of the spec, is a later plan and is deliberately not fired.
    """
    user, err = _require_player(request)
    if err:
        return err

    # `user=user` is repeated on every queryset on purpose: it is the one line standing
    # between this endpoint and a player revoking somebody else's connection, so it should be
    # visible on each query rather than hidden in a shared filter.
    access_tokens = get_access_token_model().objects.filter(
        user=user, application_id=application_id)
    refresh_tokens = get_refresh_token_model().objects.filter(
        user=user, application_id=application_id)
    grants = get_grant_model().objects.filter(
        user=user, application_id=application_id)
    id_tokens = get_id_token_model().objects.filter(
        user=user, application_id=application_id)

    # Counted BEFORE anything is deleted: IDToken is the parent of AccessToken.id_token with
    # on_delete=CASCADE, so deleting in the wrong order would make the counts lie.
    revoked = {
        "access_tokens": access_tokens.count(),
        "refresh_tokens": refresh_tokens.count(),
        "grants": grants.count(),
        "id_tokens": id_tokens.count(),
    }

    # Order: grants first (nothing depends on them), then refresh tokens (their access_token
    # link is SET_NULL), then access tokens, and finally the id tokens whose children are by
    # then already gone.
    grants.delete()
    refresh_tokens.delete()
    access_tokens.delete()
    id_tokens.delete()

    return Response(
        {
            "message": "Connection removed.",
            "application_id": application_id,
            "revoked": revoked,
        },
        status=status.HTTP_200_OK,
    )
