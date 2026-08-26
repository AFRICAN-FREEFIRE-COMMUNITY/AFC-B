"""
The generic OAuth2 authorization-code client, with PKCE.

ONE implementation for every redirect-style provider. Discord had its own hand-rolled version;
v-ent would have been a second copy, and the third would have drifted from both. Google does not use
this module at all: it is an id_token provider, verified without a redirect.

PKCE (RFC 7636) is used even though AFC is a confidential client holding a secret, because it costs
one hash and removes the whole class of attack where an intercepted authorization code is redeemed
by somebody else.

CONSUMED BY: afc_auth/connections/views.py (start and callback) and afc_auth.views for the legacy
Discord connect route.
"""
import base64
import hashlib
import secrets
from urllib.parse import urlencode

import requests

from .providers import vent

TIMEOUT_SECONDS = 15


class OAuthError(Exception):
    """A provider refused, or answered something unusable. Callers turn this into a redirect back to
    the profile page with an error flag, never a 500: the player did nothing wrong."""


def make_code_verifier():
    return secrets.token_urlsafe(64)


def code_challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _endpoints(provider):
    """A provider whose URLs are derived from an issuer (v-ent) resolves them at call time, so
    setting the env var takes effect on a restart rather than needing a code change."""
    if provider.slug == "vent":
        return vent.endpoints()
    return {
        "authorize_url": provider.authorize_url,
        "token_url": provider.token_url,
        "userinfo_url": provider.userinfo_url,
    }


def authorize_url(provider, nonce, code_verifier, redirect_uri):
    """Where to send the player's browser.

    `state` is the OPAQUE NONCE from state.py and nothing else. The previous Discord implementation
    put the player's live session token here, which handed it to discord.com.
    """
    params = {
        "client_id": provider.client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": nonce,
        "code_challenge": code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    return f"{_endpoints(provider)['authorize_url']}?{urlencode(params)}"


def exchange_code(provider, code, code_verifier, redirect_uri):
    """Swap the authorization code for a token response. Raises OAuthError on any refusal."""
    response = requests.post(
        _endpoints(provider)["token_url"],
        data={
            "client_id": provider.client_id(),
            "client_secret": provider.client_secret(),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        # The provider's body is NOT echoed to the player: it can carry the client secret back in an
        # error message. It goes to the server log, where operators can read it.
        raise OAuthError(f"{provider.slug} token exchange failed: {response.status_code}")
    return response.json()


def fetch_profile(provider, access_token):
    """The provider's own profile document. The token is used here and then dropped: AFC stores no
    provider tokens (see the ConnectedAccount docstring)."""
    response = requests.get(
        _endpoints(provider)["userinfo_url"],
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise OAuthError(f"{provider.slug} profile fetch failed: {response.status_code}")
    return response.json()
