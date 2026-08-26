"""
The provider table.

WHY A REGISTRY RATHER THAN A MODEL: which providers exist is a code decision, not data an admin
edits. Keeping it in code means adding a fourth provider is one entry here plus one adapter module,
with no migration and no change to the event form (which reads this list to build its picker).

CREDENTIALS DECIDE VISIBILITY. A provider whose client id or secret is missing from the environment
is not returned by enabled_providers(), so it cannot be listed, connected, or required. v-ent.co
ships in this file today and appears the day VENT_CLIENT_ID and VENT_CLIENT_SECRET are set on the
server.

CONSUMED BY: afc_auth/connections/views.py (list and start), afc_auth/connections/oauth.py (URLs
and scopes), and afc_tournament_and_scrims for the required-connections picker and gate.
"""
from dataclasses import dataclass, field
from typing import Callable

from django.conf import settings

from .providers import discord, google, vent


@dataclass(frozen=True)
class Provider:
    slug: str                    # stored in ConnectedAccount.provider, max 20 chars
    label: str                   # English label; the FRONTEND translates, this is a fallback
    kind: str                    # "oauth2" (redirect round trip) or "id_token" (Google)
    client_id_setting: str
    client_secret_setting: str
    normalize: Callable[[dict], dict]
    authorize_url: str = ""
    token_url: str = ""
    userinfo_url: str = ""
    scopes: tuple = field(default_factory=tuple)
    # An issuer setting, for providers whose URLs are discovered rather than hardcoded (v-ent).
    issuer_setting: str = ""

    def client_id(self):
        return (getattr(settings, self.client_id_setting, None) or "").strip()

    def client_secret(self):
        return (getattr(settings, self.client_secret_setting, None) or "").strip()

    def enabled(self):
        """Configured means usable. An id_token provider needs no secret (Google verifies the
        credential against its own public keys), so only the client id is demanded there."""
        if self.kind == "id_token":
            return bool(self.client_id())
        return bool(self.client_id() and self.client_secret())


_REGISTRY = {
    "discord": Provider(
        slug="discord",
        label="Discord",
        kind="oauth2",
        client_id_setting="DISCORD_CLIENT_ID",
        client_secret_setting="DISCORD_CLIENT_SECRET",
        authorize_url="https://discord.com/api/oauth2/authorize",
        token_url="https://discord.com/api/oauth2/token",
        userinfo_url="https://discord.com/api/users/@me",
        # identify: read the profile. guilds.join: used INSIDE the callback so the bot can add the
        # player to an event server. Nothing is kept afterwards, which is why no token is stored.
        scopes=("identify", "guilds.join"),
        normalize=discord.normalize,
    ),
    "google": Provider(
        slug="google",
        label="Google",
        kind="id_token",
        client_id_setting="GOOGLE_OAUTH_CLIENT_ID",
        client_secret_setting="GOOGLE_OAUTH_CLIENT_SECRET",
        normalize=google.normalize,
    ),
    "vent": Provider(
        slug="vent",
        label="v-ent.co",
        kind="oauth2",
        client_id_setting="VENT_CLIENT_ID",
        client_secret_setting="VENT_CLIENT_SECRET",
        issuer_setting="VENT_ISSUER",
        authorize_url="",   # resolved from the issuer at call time, see providers/vent.py
        token_url="",
        userinfo_url="",
        scopes=("openid", "profile", "email"),
        normalize=vent.normalize,
    ),
}


def get_provider(slug):
    """The Provider for `slug`, or None. Returns None rather than raising because the slug reaches
    us from a URL and from event configuration, and a typo there is a 404, not a 500."""
    return _REGISTRY.get((slug or "").strip().lower())


def is_enabled(slug):
    provider = get_provider(slug)
    return bool(provider and provider.enabled())


def enabled_providers():
    """Every provider with credentials configured, in registry order. The ONE source of truth for
    what the profile page lists and what the event-requirement picker offers."""
    return [p for p in _REGISTRY.values() if p.enabled()]
