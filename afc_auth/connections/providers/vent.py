"""
v-ent.co adapter.

STATUS: written, and DARK. v-ent.co has not issued AFC a client id, client secret or issuer URL, so
enabled() is False and this provider is invisible on every surface. Nothing here has been exercised
against a live v-ent server and nothing claims it verified. The day the credentials are set on the
box (VENT_CLIENT_ID / VENT_CLIENT_SECRET / VENT_ISSUER) it appears with no code change.

It is a plain OIDC provider, so the endpoints are derived from the issuer by the standard
convention rather than hardcoded.
"""
from django.conf import settings


def endpoints():
    """Authorize / token / userinfo derived from VENT_ISSUER. Called by oauth.py at request time
    rather than at import time, so setting the env var takes effect on a restart with no rebuild."""
    issuer = (getattr(settings, "VENT_ISSUER", None) or "").strip().rstrip("/")
    if not issuer:
        return {"authorize_url": "", "token_url": "", "userinfo_url": ""}
    return {
        "authorize_url": f"{issuer}/oauth2/authorize",
        "token_url": f"{issuer}/oauth2/token",
        "userinfo_url": f"{issuer}/oauth2/userinfo",
    }


def normalize(profile):
    """A standard OIDC userinfo document to the house shape."""
    return {
        "provider_user_id": str(profile.get("sub") or "").strip(),
        "username": (profile.get("preferred_username") or profile.get("name") or "").strip()[:190],
        "email": (profile.get("email") or "").strip().lower()[:254],
        "avatar_url": (profile.get("picture") or "").strip(),
        "raw_profile": {},
    }
