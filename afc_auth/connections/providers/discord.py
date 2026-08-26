"""
Discord adapter.

Discord is the one provider that predates this layer: it lives in four columns on afc_auth.User
(discord_id, discord_username, discord_avatar, discord_connected) and is read directly by
check_discord_membership*, DiscordRoleAssignment, roster_discord.py, several serializers and the
AFC bot. Repointing all of those is a separate refactor with its own risk, so links.link_account()
DUAL-WRITES: the ConnectedAccount row plus the legacy columns, with the legacy columns remaining
authoritative for existing readers.
"""
CDN = "https://cdn.discordapp.com/avatars"


def normalize(profile):
    """Discord's /users/@me payload to the house shape."""
    discord_id = str(profile.get("id") or "").strip()
    avatar_hash = (profile.get("avatar") or "").strip()
    return {
        "provider_user_id": discord_id,
        "username": (profile.get("global_name") or profile.get("username") or "").strip()[:190],
        "email": (profile.get("email") or "").strip()[:254],
        "avatar_url": f"{CDN}/{discord_id}/{avatar_hash}.png" if avatar_hash else "",
        "raw_profile": {"discriminator": profile.get("discriminator") or ""},
    }
