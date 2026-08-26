"""
The player's connected-accounts endpoints.

WHAT THIS IS: the INBOUND half of identity. A player links an outside account (Discord, Google,
v-ent.co) to their AFC account, sees the list, and cuts any of them off. The OUTBOUND half, partner
orgs that use "Sign in with AFC", lives in afc_sso/api.py and is rendered as the second section of
the same page.

WHY THESE LIVE UNDER /auth/ AND NOT /sso/: SSOSessionTokenMiddleware sets request.user for every
/sso/ path from the auth_token cookie, which makes DRF's SessionAuthentication run a CSRF check that
403s a DELETE for any browser holding that cookie. afc_sso/api.py works around it with
@authentication_classes([]). Routing here avoids the middleware entirely, and a test with a
CSRF-enforcing client pins that.

CONSUMED BY: frontend lib/connections.ts, rendered by
app/(user)/profile/_components/ConnectedAccounts.tsx at /profile/connected-apps.
"""
from django.conf import settings
from django.shortcuts import redirect
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.models import ConnectedAccount, User
from afc_auth.views import validate_token

from . import oauth, state
from .links import LastCredentialError, link_account, serialize_for, unlink_account
from .redirects import safe_return_to
from .registry import enabled_providers, get_provider


def _require_player(request):
    """The house auth preamble, same shape as afc_sso/api.py::_require_player and
    afc_auth.views.get_user_profile. Returns (user, None) or (None, Response)."""
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        return None, Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    user = validate_token(header.split(" ")[1])
    if not user:
        return None, Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return user, None


def _callback_uri(request, provider_slug):
    """The redirect_uri registered with the provider. Built from settings.AFC_API_BASE_URL so local
    dev, staging and production each send the player back to their own API host."""
    base = (getattr(settings, "AFC_API_BASE_URL", "") or "").rstrip("/")
    if not base:
        base = request.build_absolute_uri("/").rstrip("/")
    return f"{base}/auth/connections/{provider_slug}/callback/"


@api_view(["GET"])
def list_connections(request):
    """Every ENABLED provider, with this player's link if there is one.

    AUTH     Bearer SessionToken
    REQUEST  no body
    RESPONSE 200 {"connections": [{provider, label, kind, connected, username, avatar_url,
                                   connected_at, can_disconnect}]}
    CONSUMED BY frontend lib/connections.ts listConnections()
    """
    user, refusal = _require_player(request)
    if refusal:
        return refusal
    return Response({"connections": serialize_for(user)}, status=status.HTTP_200_OK)


@api_view(["GET"])
def list_providers(request):
    """Every provider an organizer may require, i.e. every provider a player could connect.

    AUTH     Bearer SessionToken
    RESPONSE 200 {"providers": [{"slug", "label", "kind"}]}
    CONSUMED BY the "Required connected accounts" picker on all four event forms
               (frontend components/events/RequiredConnectionsPicker.tsx).

    WHY IT MATTERS THAT THIS IS THE SAME LIST the profile page uses: an organizer must not be able
    to require something no player can connect. Both read enabled_providers(), so v-ent.co becomes
    requirable on exactly the day it becomes connectable.
    """
    _user, refusal = _require_player(request)
    if refusal:
        return refusal
    return Response(
        {"providers": [
            {"slug": p.slug, "label": p.label, "kind": p.kind} for p in enabled_providers()
        ]},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
def start_connection(request, provider_slug):
    """Begin a redirect-style link: mint a nonce, send the browser to the provider.

    AUTH     Bearer SessionToken
    REQUEST  ?return_to=<AFC path>, validated against AFC's own origin, never trusted raw
    RESPONSE 302 to the provider, or 404 when the provider is unknown or not configured
    """
    user, refusal = _require_player(request)
    if refusal:
        return refusal

    provider = get_provider(provider_slug)
    if not provider or not provider.enabled():
        return Response({"message": "Unknown provider."}, status=status.HTTP_404_NOT_FOUND)
    if provider.kind != "oauth2":
        return Response(
            {"message": "This provider is linked without a redirect."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    verifier = oauth.make_code_verifier()
    nonce = state.mint(
        user_id=user.user_id,
        provider=provider.slug,
        return_to=safe_return_to(request.GET.get("return_to")),
        code_verifier=verifier,
    )
    return redirect(oauth.authorize_url(
        provider, nonce=nonce, code_verifier=verifier,
        redirect_uri=_callback_uri(request, provider.slug),
    ))


@api_view(["GET"])
def finish_connection(request, provider_slug):
    """The provider sends the player back here.

    AUTH     the NONCE in ?state, not a session token: this request arrives from the provider's
             redirect, so no Authorization header exists.
    RESPONSE 302 back into the AFC frontend, with ?connected=<slug> or ?connect_error=<reason>
    """
    provider = get_provider(provider_slug)
    payload = state.consume(request.GET.get("state"))

    frontend = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    if (
        not provider
        or not provider.enabled()
        or not payload
        or payload.get("provider") != provider_slug
    ):
        return redirect(f"{frontend}/profile/connected-apps?connect_error=expired")

    destination = safe_return_to(payload.get("return_to"))
    if request.GET.get("error"):
        # The player pressed Cancel on the provider's consent screen. Not an error to shout about.
        return redirect(f"{destination}?connect_error=cancelled")

    try:
        tokens = oauth.exchange_code(
            provider,
            code=request.GET.get("code"),
            code_verifier=payload.get("code_verifier", ""),
            redirect_uri=_callback_uri(request, provider.slug),
        )
        profile = oauth.fetch_profile(provider, tokens.get("access_token"))
    except oauth.OAuthError:
        return redirect(f"{destination}?connect_error=provider")

    normalized = provider.normalize(profile)
    if not normalized.get("provider_user_id"):
        return redirect(f"{destination}?connect_error=provider")

    user = User.objects.filter(user_id=payload["user_id"]).first()
    if not user:
        return redirect(f"{frontend}/profile/connected-apps?connect_error=expired")

    # The uniqueness rule made visible: this outside account may already belong to a DIFFERENT AFC
    # account. Refusing here with a named reason beats an IntegrityError 500, and it is the rule
    # that stops one Discord account satisfying a required-connection rule for five AFC accounts.
    taken = ConnectedAccount.objects.filter(
        provider=provider.slug, provider_user_id=normalized["provider_user_id"],
    ).exclude(user=user).exists()
    if taken:
        return redirect(f"{destination}?connect_error=already_linked")

    link_account(user, provider.slug, normalized, scopes=provider.scopes)
    return redirect(f"{destination}?connected={provider.slug}")


@api_view(["POST"])
def link_google(request):
    """Link Google from an ID token the frontend already holds. No redirect round trip.

    AUTH     Bearer SessionToken
    REQUEST  {"credential": "<Google ID token>"}
    RESPONSE 200 {"message", "connections": [...]} | 401 unverifiable | 409 already_linked
    CONSUMED BY frontend lib/connections.ts linkGoogle().
    """
    user, refusal = _require_player(request)
    if refusal:
        return refusal

    provider = get_provider("google")
    if not provider or not provider.enabled():
        return Response({"message": "Unknown provider."}, status=status.HTTP_404_NOT_FOUND)

    credential = (request.data or {}).get("credential")
    if not credential:
        return Response({"message": "credential is required"}, status=status.HTTP_400_BAD_REQUEST)

    # The SAME verification the sign-in path uses (afc_auth.views.google_auth), so a credential good
    # enough to log in with is good enough to link, and neither can drift from the other.
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token

        claims = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), provider.client_id(), clock_skew_in_seconds=60,
        )
    except Exception:
        return Response(
            {"message": "Could not verify your Google sign-in. Please try again."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    normalized = provider.normalize(claims)
    if not normalized.get("provider_user_id"):
        return Response(
            {"message": "Could not verify your Google sign-in. Please try again."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    taken = ConnectedAccount.objects.filter(
        provider="google", provider_user_id=normalized["provider_user_id"],
    ).exclude(user=user).exists()
    if taken:
        return Response(
            {"code": "already_linked",
             "message": "That Google account is already connected to another AFC account."},
            status=status.HTTP_409_CONFLICT,
        )

    link_account(user, "google", normalized)
    return Response(
        {"message": "Google connected.", "connections": serialize_for(user)},
        status=status.HTTP_200_OK,
    )


@api_view(["DELETE"])
def disconnect(request, provider_slug):
    """Cut an outside account off.

    AUTH     Bearer SessionToken
    RESPONSE 200 {"message", "connections": [...]}
             409 {"code": "last_credential"} when it is the player's only way to sign in
    IDEMPOTENT: disconnecting something already disconnected is a 200 with nothing removed, so a
    double tap on a phone cannot produce a scary failure toast.
    """
    user, refusal = _require_player(request)
    if refusal:
        return refusal

    if not get_provider(provider_slug):
        return Response({"message": "Unknown provider."}, status=status.HTTP_404_NOT_FOUND)

    try:
        unlink_account(user, provider_slug)
    except LastCredentialError:
        return Response(
            {"code": "last_credential",
             "message": "Set a password before disconnecting your only way to sign in."},
            status=status.HTTP_409_CONFLICT,
        )
    return Response(
        {"message": "Disconnected.", "connections": serialize_for(user)},
        status=status.HTTP_200_OK,
    )
