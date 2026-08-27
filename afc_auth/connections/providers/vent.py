"""
v-ent.co adapter.

STATUS 2026-08-28: endpoints and scopes CORRECTED against v-ent.co's own published metadata, AFC's
client registration confirmed live, and the userinfo claim names confirmed from v-ent.co's source.
Still not exercised end to end with a real player token, because that needs credentials set on a
running box.

WHAT WAS WRONG, AND HOW IT WAS FOUND
    This file used to derive the endpoints from VENT_ISSUER by the usual OIDC convention:

        {issuer}/oauth2/authorize   {issuer}/oauth2/token   {issuer}/oauth2/userinfo

    All three were wrong. v-ent.co publishes a metadata document, and it says:

        GET https://api.v-ent.co/partners/sso/metadata/      (read 2026-08-27)
        {
          "issuer":                 "https://api.v-ent.co",
          "authorization_endpoint": "https://v-ent.co/partners/authorize",
          "token_endpoint":         "https://api.v-ent.co/partners/sso/token/",
          "userinfo_endpoint":      "https://api.v-ent.co/partners/sso/userinfo/",
          "response_types_supported": ["code"],
          "grant_types_supported":    ["authorization_code"],
          "code_challenge_methods_supported": ["S256"],
          "scopes_supported": ["identity", "identity:email", "identity:teams"],
          "token_endpoint_auth_methods_supported": ["client_secret_post"]
        }

    The detail that breaks the old design outright: the authorize endpoint is on a DIFFERENT HOST
    from the issuer. `v-ent.co` is where the player's browser goes to approve, `api.v-ent.co` is
    where the server-to-server calls go. No path convention derives one host from the other, so the
    browser-facing host is its own setting.

    The scopes were wrong too. AFC asked for `openid profile email`, which v-ent.co does not
    publish; it uses `identity` and `identity:email`. Fixed in registry.py.

WHAT AFC ALREADY DOES CORRECTLY
    oauth.py sends `response_type=code` with PKCE `S256`, and posts `client_id` + `client_secret` in
    the token request body, which is `client_secret_post`. Those match the metadata above, so the
    flow itself needed no change.

CONFIRMED LIVE (2026-08-27), with AFC's client id and no secret:
    GET https://api.v-ent.co/partners/sso/authorize-info/?client_id=...&redirect_uri=...
    answered 200 with partner "AFRICAN FREE FIRE COMMUNITY" and echoed the redirect
    "https://api.africanfreefirecommunity.com/auth/connections/vent/callback/", so AFC's
    registration and its callback URL are both correct on v-ent.co's side.

THE USERINFO SHAPE, CONFIRMED 2026-08-28
    Read from v-ent.co's own source (`vent_partners/views_sso.py::sso_userinfo`), not guessed. The
    body is the same `{"status", "code", "message", "data"}` envelope its error responses use, and
    `data` carries:

        sub                 the v-ent user id, as a string   <- the subject AFC keys on
        username            handle
        name                full name
        country             ISO country
        city                (their `state` column)
        picture             absolute URL, or NULL when the player has no avatar
        is_founding_member  bool
        email               ONLY when identity:email was granted
        email_verified      ONLY when identity:email was granted
        teams               ONLY when identity:teams was granted, which AFC does not request

    Every field `normalize()` needs resolves: sub, username, email, picture. The tolerance below is
    kept anyway, because it costs nothing and the alternative is a hard failure if v-ent.co renames
    a key. `country` is available and deliberately unused: the house shape has no slot for it, and
    AFC already asks players their country.

    CAVEAT, stated because it is the honest limit: this was read from a checkout of v-ent.co's
    backend (commit bea71c2b), NOT from a live response, which would need a real player token. The
    metadata document served by the live API matches that same checkout exactly, which is good
    evidence the two agree, but it is evidence rather than proof.

SETTINGS
    VENT_CLIENT_ID / VENT_CLIENT_SECRET   issued by v-ent.co
    VENT_ISSUER                           API host, default https://api.v-ent.co
    VENT_AUTHORIZE_BASE                   browser host, default https://v-ent.co

    Both hosts have working defaults, so in practice only the id and the secret need setting.
"""
from django.conf import settings

# v-ent.co's published hosts. Defaults rather than hardcoding: either can be overridden by a
# setting, which is what a staging environment needs.
DEFAULT_ISSUER = "https://api.v-ent.co"
DEFAULT_AUTHORIZE_BASE = "https://v-ent.co"

# Paths under those hosts, from the metadata document quoted above. The trailing slashes on the
# token and userinfo endpoints are Django's and are load-bearing: without them the request is
# redirected, and a 302 on a POST loses the body.
AUTHORIZE_PATH = "/partners/authorize"
TOKEN_PATH = "/partners/sso/token/"
USERINFO_PATH = "/partners/sso/userinfo/"

# Where the metadata lives, so the next person can re-read it rather than trusting this docstring.
METADATA_PATH = "/partners/sso/metadata/"


def _base(setting_name, default):
    """A configured base URL, or the published default, with any trailing slash removed."""
    return (getattr(settings, setting_name, None) or default).strip().rstrip("/")


def endpoints():
    """Authorize / token / userinfo for v-ent.co.

    Called by oauth.py at request time rather than at import time, so changing a setting takes
    effect on a restart with no rebuild.

    The authorize endpoint deliberately comes from a DIFFERENT base to the other two: v-ent.co sends
    the player's browser to `v-ent.co` and takes server-to-server calls on `api.v-ent.co`.
    """
    issuer = _base("VENT_ISSUER", DEFAULT_ISSUER)
    authorize_base = _base("VENT_AUTHORIZE_BASE", DEFAULT_AUTHORIZE_BASE)
    return {
        "authorize_url": f"{authorize_base}{AUTHORIZE_PATH}",
        "token_url": f"{issuer}{TOKEN_PATH}",
        "userinfo_url": f"{issuer}{USERINFO_PATH}",
    }


def metadata_url():
    """v-ent.co's own metadata document, for a human checking this file has not drifted."""
    return f"{_base('VENT_ISSUER', DEFAULT_ISSUER)}{METADATA_PATH}"


def access_token(payload):
    """Pull the access token out of v-ent.co's TOKEN response.

    This exists because v-ent.co wraps it. Discord and Google answer the flat OAuth 2 body, so
    `payload["access_token"]` works there and the generic reader is enough. v-ent.co answers its
    house envelope, the same one its errors use:

        {"status": "success", "message": "Token",
         "data": {"access_token": "...", "token_type": "Bearer", "expires_in": 3600}}

    Read from `vent_partners/views_sso.py::sso_token`, which returns `_ok({...})`.

    WHAT THIS PREVENTS, because it is not a small failure: the generic reader returns None here,
    AFC then calls userinfo with `Authorization: Bearer None`, v-ent.co answers 401 BAD_TOKEN, and
    the connection fails for EVERY player, AFTER they have already approved AFC on v-ent.co's
    consent screen. It would have looked like v-ent.co's fault.

    Tolerant in the same way and for the same reason as normalize(): if v-ent.co ever flattens the
    body, the top level is tried too.
    """
    if not isinstance(payload, dict):
        return ""
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return str(inner.get("access_token") or payload.get("access_token") or "").strip()


def normalize(profile):
    """v-ent.co's userinfo document to the house shape.

    Tolerant on purpose. The exact claim names are NOT confirmed (see the module docstring), so each
    field tries the OIDC spelling and the plain one. Every field except the id is optional, and a
    missing optional must not fail a connection: the player would be told their v-ent account was
    broken when the only thing wrong is a key name.

    The id is the one field that cannot be guessed at. Without a stable subject there is nothing to
    key ConnectedAccount on, so an empty one is returned as empty and the caller refuses the link
    rather than inventing an identity.
    """
    empty = {
        "provider_user_id": "",
        "username": "",
        "email": "",
        "avatar_url": "",
        "raw_profile": {},
    }
    if not isinstance(profile, dict):
        return empty

    # v-ent.co's other endpoints answer {"status": ..., "data": {...}}, so the same envelope here
    # would be unsurprising. Unwrap it rather than reading None out of the top level.
    if isinstance(profile.get("data"), dict):
        profile = profile["data"]
    if isinstance(profile.get("user"), dict):
        profile = {**profile, **profile["user"]}

    def first(*keys):
        for key in keys:
            value = profile.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    return {
        "provider_user_id": first("sub", "id", "user_id", "uuid"),
        "username": first("preferred_username", "username", "display_name", "name")[:190],
        "email": first("email").lower()[:254],
        "avatar_url": first("picture", "avatar_url", "avatar", "image"),
        "raw_profile": {},
    }
