"""
afc_bot.permissions - WHO may manage the Discord bot.

HEAD ADMINS ONLY, and deliberately narrower than most admin surfaces here.

The bot is AFC-wide infrastructure, not a per-organization or per-event thing, so the organizer
gates that `afc_polls` and `afc_fantasy` compose do not apply: there is no organization whose bot
this is. And the settings behind this page decide where room IDs, ban notices and announcements
are delivered, so somebody who can edit them can silently redirect every automated message AFC
sends. That is a smaller circle than "can edit events".

`organizer_admin` is deliberately EXCLUDED even though `is_platform_org_admin` would accept it:
that role exists to oversee organizations, which has nothing to do with the Discord bot.

CONSUMED BY: every view in afc_bot.views.
"""


def can_manage_bot(user) -> bool:
    """True only for AFC head admins."""
    return (
        bool(user)
        and getattr(user, "role", None) == "admin"
        and user.userroles.filter(role__role_name="head_admin").exists()
    )
