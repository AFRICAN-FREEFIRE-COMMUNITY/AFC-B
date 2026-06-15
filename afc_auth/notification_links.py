"""
afc_auth.notification_links
===========================

NOTIFICATION DEEP-LINKING helper (owner 2026-06-15).

Turns the (`target_type`, `target_id`) pair stored on a `Notifications` row into a RELATIVE
frontend path that the in-app "Take me there" button opens. The URL is computed on read (never
stored) so a later slug change is reflected automatically.

Where this fits in the system:
  - WRITTEN BY (the target pair): afc_auth.views.send_notification /
    send_notification_to_multiple_users / admin_send_message, and the tournament broadcast path
    (afc_auth.views.deliver_broadcast, called from
    afc_tournament_and_scrims.views.broadcast_announcement / broadcast_to_group).
  - READ HERE: afc_auth.views.get_notifications calls build_notification_link() per row and returns
    the result as the `link` field.
  - CONSUMED BY: the frontend notifications dropdown / page, which renders a "Take me there" button
    when `link` is non-null and routes the user to that relative path. The same choices back the
    admin notification-composer target selector.

Frontend routes these paths map to (see frontend/app/(user)/...):
  /tournaments/<slug-or-id>   -> app/(user)/tournaments/[slug]
  /news/<slug-or-id>          -> app/(user)/news/[slug]
  /teams/<id>                 -> app/(user)/teams/[id]
  /players/<username>         -> app/(user)/players/[username]
  /shop                       -> app/(user)/shop
  /organizations/<slug-or-id> -> app/(user)/organizations/[slug]
"""

from typing import Optional


def _slug_for_event(target_id: str) -> str:
    """For target_type='event': if target_id is a NUMERIC id, resolve the Event's slug so the link
    matches the slug-based /tournaments/[slug] route. Falls back to the raw value (a slug already, or
    the id when the event is gone / has no slug). Best-effort: any lookup error returns the input."""
    if not target_id.isdigit():
        return target_id  # already a slug
    try:
        # Local import: afc_auth.models imports Event lazily as a string FK, so importing the concrete
        # model at call-time (not module load) avoids any app-loading order issue.
        from afc_tournament_and_scrims.models import Event
        event = Event.objects.filter(event_id=int(target_id)).only("slug").first()
        if event and event.slug:
            return event.slug
    except Exception:
        pass
    return target_id


def _slug_for_news(target_id: str) -> str:
    """For target_type='news': resolve a NUMERIC id to the News slug for the /news/[slug] route.
    Falls back to the raw value. Best-effort (see _slug_for_event)."""
    if not target_id.isdigit():
        return target_id  # already a slug
    try:
        from afc_auth.models import News
        news = News.objects.filter(news_id=int(target_id)).only("slug").first()
        if news and news.slug:
            return news.slug
    except Exception:
        pass
    return target_id


def _slug_for_organizer(target_id: str) -> str:
    """For target_type='organizer': resolve a NUMERIC id to the Organization slug for the
    /organizations/[slug] route. Falls back to the raw value. Best-effort (see _slug_for_event)."""
    if not target_id.isdigit():
        return target_id  # already a slug
    try:
        from afc_organizers.models import Organization
        org = Organization.objects.filter(organization_id=int(target_id)).only("slug").first()
        if org and org.slug:
            return org.slug
    except Exception:
        pass
    return target_id


def build_notification_link(target_type: str, target_id: str) -> Optional[str]:
    """Map a (target_type, target_id) pair to a RELATIVE frontend path, or None when there is no link.

    Rules (kept in lock-step with the FE admin target selector + the "Take me there" button):
      event     -> /tournaments/<slug-or-id>   (numeric id resolved to slug when possible)
      news      -> /news/<slug-or-id>          (numeric id resolved to slug when possible)
      team      -> /teams/<id>
      player    -> /players/<username>
      shop      -> /shop                       (target_id ignored)
      organizer -> /organizations/<slug-or-id> (numeric id resolved to slug when possible)
      custom    -> target_id as-is             (must start with "/", else treated as no link)
      none / "" / unknown -> None

    Returns None (no link) when target_type is blank/"none"/unrecognised, or when a type that needs a
    target_id has none, or when a "custom" path is not a valid relative path.
    """
    target_type = (target_type or "").strip().lower()
    target_id = (target_id or "").strip()

    # No-link cases: explicit "none", blank, or any unrecognised type.
    if not target_type or target_type == "none":
        return None

    # /shop is fixed and needs no target_id.
    if target_type == "shop":
        return "/shop"

    # "custom" carries a full relative path; only honour it when it is actually relative ("/..."),
    # never an absolute or off-site URL (guards against open-redirect-style values).
    if target_type == "custom":
        if target_id.startswith("/") and not target_id.startswith("//"):
            return target_id
        return None

    # Every remaining type needs a target_id to point at something.
    if not target_id:
        return None

    if target_type == "event":
        return f"/tournaments/{_slug_for_event(target_id)}"
    if target_type == "news":
        return f"/news/{_slug_for_news(target_id)}"
    if target_type == "team":
        return f"/teams/{target_id}"
    if target_type == "player":
        return f"/players/{target_id}"
    if target_type == "organizer":
        return f"/organizations/{_slug_for_organizer(target_id)}"

    # Unknown type -> no link (forward-compatible: an unrecognised value never throws).
    return None
