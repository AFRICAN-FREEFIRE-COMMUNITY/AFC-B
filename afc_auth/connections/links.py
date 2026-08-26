"""
The ONLY writers of ConnectedAccount.

Everything that links or unlinks an outside account goes through here, so three rules cannot be
forgotten by a future caller:

  1. RELINKING UPDATES, it does not stack. update_or_create on (user, provider) keeps the profile
     page showing one row per provider, which is what the uniqueness constraint promises anyway.
  2. DISCORD DUAL-WRITES the four legacy User columns. check_discord_membership*,
     DiscordRoleAssignment, roster_discord.py, several serializers and the AFC bot all read
     User.discord_id directly, and they keep working untouched.
  3. A PLAYER CANNOT DISCONNECT THEIR WAY OUT OF THEIR OWN ACCOUNT. See LastCredentialError.

CONSUMED BY: afc_auth/connections/views.py, afc_auth.views.google_auth (which links on every Google
sign-in so an existing player gets a row without doing anything) and afc_auth.views.discord_callback.
"""
from django.utils import timezone

from afc_auth.models import ConnectedAccount, DiscordRoleAssignment

from .registry import enabled_providers


class LastCredentialError(Exception):
    """Raised when disconnecting would leave the player no way to sign in.

    This is not hypothetical. afc_auth.views.google_auth calls set_unusable_password() on every
    account it creates, so a player who signed up with Google has no password until they set one.
    Disconnecting Google would be irreversible and self-service recovery would be impossible.
    """


def _has_usable_password(user):
    return bool(user.password) and user.has_usable_password()


def can_disconnect(user, provider_slug):
    """True when the player keeps a way in after removing this link: a usable password, or another
    connected provider."""
    if _has_usable_password(user):
        return True
    return ConnectedAccount.objects.filter(user=user).exclude(provider=provider_slug).exists()


def link_account(user, provider_slug, normalized, scopes=()):
    """Create or refresh the link. `normalized` is the dict a provider adapter's normalize() built."""
    row, _created = ConnectedAccount.objects.update_or_create(
        user=user,
        provider=provider_slug,
        defaults={
            "provider_user_id": normalized.get("provider_user_id", ""),
            "username": normalized.get("username", ""),
            "email": normalized.get("email", ""),
            "avatar_url": normalized.get("avatar_url", ""),
            "raw_profile": normalized.get("raw_profile", {}),
            "scopes": list(scopes),
            "last_verified_at": timezone.now(),
        },
    )

    # -- Discord compatibility: keep the legacy columns authoritative for existing readers --
    if provider_slug == "discord":
        user.discord_id = normalized.get("provider_user_id") or None
        user.discord_username = normalized.get("username") or None
        user.discord_avatar = normalized.get("avatar_url") or None
        user.discord_connected = True
        user.save(update_fields=[
            "discord_id", "discord_username", "discord_avatar", "discord_connected",
        ])
    return row


def unlink_account(user, provider_slug):
    """Remove the link, or refuse if it is the player's last way in."""
    if not can_disconnect(user, provider_slug):
        raise LastCredentialError(provider_slug)

    ConnectedAccount.objects.filter(user=user, provider=provider_slug).delete()

    if provider_slug == "discord":
        # Same side effect the old disconnect_discord_account had: a queued role assignment for an
        # account we can no longer resolve is garbage that would fail later, out of sight.
        DiscordRoleAssignment.objects.filter(user=user, status="pending").delete()
        user.discord_id = None
        user.discord_username = None
        user.discord_avatar = None
        user.discord_connected = False
        user.save(update_fields=[
            "discord_id", "discord_username", "discord_avatar", "discord_connected",
        ])


def serialize_for(user):
    """Every ENABLED provider, with this player's link if there is one.

    One row per provider, connected or not, so the page can offer Connect on the empty ones without
    the frontend needing its own provider list to keep in sync.
    """
    linked = {row.provider: row for row in ConnectedAccount.objects.filter(user=user)}
    out = []
    for provider in enabled_providers():
        row = linked.get(provider.slug)
        out.append({
            "provider": provider.slug,
            "label": provider.label,
            "kind": provider.kind,
            "connected": bool(row),
            "username": row.username if row else "",
            "avatar_url": row.avatar_url if row else "",
            "connected_at": row.connected_at.isoformat() if row else None,
            # Told to the frontend so the Disconnect button can be disabled WITH A REASON rather
            # than failing on tap.
            "can_disconnect": can_disconnect(user, provider.slug) if row else False,
        })
    return out
