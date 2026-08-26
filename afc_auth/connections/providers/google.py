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
