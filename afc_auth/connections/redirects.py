"""
Where a connect flow is allowed to send the player afterwards.

THE PROBLEM THIS CLOSES: connect_discord_account accepted any `return_to` from the query string and
redirected to it. An unvalidated redirect on an AFC domain lets someone build a link that starts on
africanfreefirecommunity.com and lands anywhere, which is the standard shape of a phishing lure that
borrows a trusted domain.

THE RULE: the destination is always on settings.FRONTEND_URL. A relative path is kept and made
absolute. An absolute URL is kept ONLY if its scheme and host match the frontend EXACTLY, compared
as parsed components rather than by string prefix, because "africanfreefirecommunity.com.evil.example"
starts with the real host and would pass a naive check. Anything else silently becomes the default
page rather than erroring, because a player who did nothing wrong should still land somewhere
sensible.

CONSUMED BY: afc_auth/connections/views.py and afc_auth.views.connect_discord_account.
"""
from urllib.parse import urljoin, urlparse

from django.conf import settings

DEFAULT_PATH = "/profile/connected-apps"


def safe_return_to(candidate):
    frontend = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    default = f"{frontend}{DEFAULT_PATH}"
    candidate = (candidate or "").strip()
    if not candidate:
        return default

    # A protocol-relative URL is read by the browser as another ORIGIN even though it starts with a
    # slash, so the cheap "starts with /" test is not enough on its own.
    if candidate.startswith("//"):
        return default

    parsed = urlparse(candidate)
    if not parsed.scheme and not parsed.netloc:
        return urljoin(f"{frontend}/", candidate.lstrip("/"))

    allowed = urlparse(frontend)
    if (parsed.scheme, parsed.netloc) == (allowed.scheme, allowed.netloc):
        return candidate
    return default
