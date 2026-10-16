"""
afc_mail/addresses.py - every absolute address an email carries: tracked links, the open pixel, the
unsubscribe page and one-click address, and AFC media (pictures and fonts of an email).

Kept apart from content.py so assets.py and content.py can both use it without importing each other.
CONNECTS TO: afc_mail.content, afc_mail.assets, afc_mail.tasks, afc_mail.views (click redirect).
"""
from django.conf import settings


def api_base():
    """Where the tracking, one-click unsubscribe and media addresses live (the API host)."""
    return (getattr(settings, "MAIL_PUBLIC_API_BASE", "") or "https://api.africanfreefirecommunity.com").rstrip("/")


def frontend_base():
    """The site an email's unsubscribe link opens. MAIL_FRONTEND_BASE is for a local walk only."""
    return (getattr(settings, "MAIL_FRONTEND_BASE", "") or getattr(settings, "FRONTEND_URL", "")
            or "https://africanfreefirecommunity.com").rstrip("/")


def media_url(name):
    """The public address of a stored file. Emails are read far from AFC, so it is always absolute."""
    return f"{api_base()}{settings.MEDIA_URL}{name}"


def tracked_link(token, links):
    def _for(url):
        try:
            i = links.index(url)
        except ValueError:
            return url  # cannot happen for a sent campaign: links were collected from these documents
        return f"{api_base()}/mail/c/{token}/{i}/"
    return _for


def pixel(token):
    return f"{api_base()}/mail/o/{token}.gif"


def unsubscribe_page(token):
    return f"{frontend_base()}/email/unsubscribe/{token}"


def one_click_unsubscribe(token):
    return f"{api_base()}/mail/unsubscribe/{token}/"
