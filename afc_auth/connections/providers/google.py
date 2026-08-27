"""
Google adapter.

Google already backs a SIGN-IN path (afc_auth.views.google_auth), which verifies an ID token and
then finds the AFC user BY EMAIL, storing nothing. That has a real defect: a player who changes
their Gmail address becomes a different person to AFC, and a new AFC account created under the new
address would silently absorb their Google sign-in. This adapter stores the Google `sub`, which is
stable for the life of the Google account, and google_auth prefers a `sub` match once one exists.

There is no redirect round trip here: the frontend already obtains an ID token for sign-in, and the
connect endpoint verifies that same credential with the same library call.
"""


def normalize(claims):
    """Verified Google ID token claims to the house shape. `sub` is Google's stable subject id."""
    return {
        "provider_user_id": str(claims.get("sub") or "").strip(),
        "username": (claims.get("name") or "").strip()[:190],
        "email": (claims.get("email") or "").strip().lower()[:254],
        "avatar_url": (claims.get("picture") or "").strip(),
        "raw_profile": {"email_verified": bool(claims.get("email_verified"))},
    }


# ── ONE code-exchange, shared by SIGN-IN and CONNECT ──────────────────────────────────────────
# WHY THIS EXISTS (bug, owner 2026-08-27: "We could not start connecting Google", from an account
# that had already signed in with Google).
#
# Two things reach Google with a user's consent, and they had drifted apart:
#
#   SIGN IN  afc_auth.views.google_auth accepts EITHER a GIS id token (`credential`) OR an auth
#            CODE from the GIS popup code client, which it exchanges server-side.
#   CONNECT  afc_auth.connections.views.link_google accepted ONLY a `credential`.
#
# The docstring above this module still said "the frontend already obtains an ID token for
# sign-in". That stopped being true on 2026-06-21, when the sign-in button moved to the CODE
# client so it could be a full-width AFC button instead of Google's locked 400px iframe. So the
# connect endpoint was written against how a sibling flow used to work, and could never have
# succeeded: the browser has a code, and the endpoint only understood id tokens.
#
# The fix is deliberately ONE function rather than a second copy of the exchange. Copying is how
# these two drifted in the first place, and the hard rule in WEBSITE/CLAUDE.md ("One contract per
# domain object") was written about exactly this shape of duplication a day earlier.


class GoogleAuthError(Exception):
    """A failure with the message and HTTP status the caller should return verbatim.

    Carrying the status here keeps the two endpoints answering identically: a misconfigured server
    is a 400 (nothing the player can do), an unverifiable credential is a 401.
    """

    def __init__(self, message, status_code):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def resolve_id_token(*, credential=None, code=None, client_id=None, client_secret=None):
    """Turn whatever the frontend sent into a Google ID TOKEN, ready to verify.

    Accepts the two shapes the browser can produce and returns the id token unverified: the CALLER
    verifies, because sign-in and connect log different context on failure.

    credential  a GIS id token, returned as-is
    code        an auth code from the GIS popup code client, exchanged with Google. Needs the
                client SECRET, which the credential path does not.

    Raises GoogleAuthError, whose message and status are meant to be returned unchanged.
    """
    if not credential and not code:
        raise GoogleAuthError("Google credential is required.", 400)

    if not client_id:
        raise GoogleAuthError("Google sign-in is not configured on the server.", 400)

    if credential:
        return credential

    if not client_secret:
        raise GoogleAuthError("Google sign-in is not fully configured on the server.", 400)

    import logging

    import requests

    log = logging.getLogger("afc_auth")
    try:
        tok = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                # "postmessage" is what the GIS POPUP code client expects. It is not a URL and must
                # not be replaced with one: the popup hands the code back through postMessage, so
                # there is no redirect URI to register.
                "redirect_uri": "postmessage",
            },
            timeout=10,
        )
    except Exception as exc:
        log.warning("Google code exchange error: %s: %s", type(exc).__name__, exc)
        raise GoogleAuthError("Could not verify your Google sign-in. Please try again.", 401)

    if tok.status_code != 200:
        log.warning("Google code exchange failed: %s %s", tok.status_code, tok.text[:200])
        raise GoogleAuthError("Could not verify your Google sign-in. Please try again.", 401)

    id_token = tok.json().get("id_token")
    if not id_token:
        raise GoogleAuthError("Could not verify your Google sign-in. Please try again.", 401)
    return id_token
