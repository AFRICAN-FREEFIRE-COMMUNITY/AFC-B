"""afc_qr.targets - the four page types a QR can point at, in ONE place (R24: one model per thing).

For each type: how the public ref a page URL carries finds the row, the page's CURRENT address, its display
name and picture (for the card and poster), and who counts as its owner for the scan count. Adding a fifth
type means one entry in each table below plus one value in QrLink.TARGET_TYPES; nothing else changes.

Refs, matching the frontend routes:
  event   slug       /tournaments/<slug>   published events only (is_draft False)
  team    team_name  /teams/<team_name>
  player  username   /players/<username>   not deleted accounts
  news    slug       /news/<slug>          published posts only (is_published True)
An unpublished event or post, or a deleted account, has no QR: the short link would otherwise reveal its title before it is out.

Owners, who may see the scan count (R58):
  everyone below, plus head admins (super_admin / head_admin) on every page
  event   AFC event admins, or anyone org_can_event(user, "can_edit_events", event)
  team    the team owner
  player  the player themself
  news    news admins, the same gate as create_news / edit_news (afc_auth.views._is_news_admin)
"""
from urllib.parse import quote

from afc_auth.models import News, User, canonical_profile
from afc_organizers.permissions import org_can_event
from afc_team.models import Team
from afc_tournament_and_scrims.models import Event

from .models import QrLink

MODELS = {QrLink.EVENT: Event, QrLink.TEAM: Team, QrLink.PLAYER: User, QrLink.NEWS: News}
PK = {QrLink.EVENT: "event_id", QrLink.TEAM: "team_id", QrLink.PLAYER: "user_id", QrLink.NEWS: "news_id"}
REF = {QrLink.EVENT: "slug", QrLink.TEAM: "team_name", QrLink.PLAYER: "username", QrLink.NEWS: "slug"}
# Extra conditions a row must meet to be reachable by QR at all
PUBLISHED = {QrLink.EVENT: {"is_draft": False}, QrLink.NEWS: {"is_published": True}}
# ...and rows it must not be: a deleted account keeps a tombstone username, and its profile page
# answers not-found (afc_player.views.get_public_player_stats excludes it the same way)
HIDDEN = {QrLink.PLAYER: {"status": "deleted"}}

HEAD_ROLES = ("super_admin", "head_admin")
EVENT_ADMIN_ROLES = HEAD_ROLES + ("event_admin",)


def _has_role(user, roles):
    return bool(user) and user.userroles.filter(role__role_name__in=roles).exists()


def _is_news_admin(user):
    # Mirrors afc_auth.views._is_news_admin (the create_news / edit_news gate) exactly
    return bool(user) and user.role in ("admin", "moderator", "support") and \
        _has_role(user, ("head_admin", "news_admin"))


def _rows(target_type):
    rows = MODELS[target_type].objects.filter(**PUBLISHED.get(target_type, {}))
    hidden = HIDDEN.get(target_type)
    return rows.exclude(**hidden) if hidden else rows


def find_target(target_type, ref):
    """The published row a public ref names, or None."""
    ref = (ref or "").strip()
    if target_type not in MODELS or not ref:
        return None
    return _rows(target_type).filter(**{REF[target_type]: ref}).first()


def id_of(target_type, obj):
    return getattr(obj, PK[target_type])


def target_of(link):
    """The row a link points at, or None once it is deleted or unpublished."""
    if link.target_type not in MODELS:
        return None
    return _rows(link.target_type).filter(**{PK[link.target_type]: link.target_id}).first()


def _file_url(request, field):
    if not field:
        return ""
    try:
        url = field.url
    except ValueError:
        return ""
    return request.build_absolute_uri(url) if request is not None and url.startswith("/") else url


def describe(link, request):
    """{target_type, name, path, picture} for the card, poster and redirect; None when the page is gone."""
    obj = target_of(link)
    if obj is None:
        return None
    kind = link.target_type
    if kind == QrLink.EVENT:
        name, path, picture = obj.event_name, f"/tournaments/{quote(obj.slug or '')}", obj.event_banner
    elif kind == QrLink.TEAM:
        name, path, picture = obj.team_name, f"/teams/{quote(obj.team_name)}", obj.team_logo
    elif kind == QrLink.PLAYER:
        profile = canonical_profile(obj)
        name, path, picture = obj.username, f"/players/{quote(obj.username)}", getattr(profile, "profile_pic", None)
    else:
        name, path, picture = obj.news_title, f"/news/{quote(obj.slug or '')}", obj.images
    if path.endswith("/"):  # a published event or post with no slug yet has no address to send anyone to
        return None
    return {"target_type": kind, "name": name, "path": path, "picture": _file_url(request, picture)}


def can_see_stats(user, link):
    if user is None:
        return False
    if getattr(user, "is_superuser", False) or _has_role(user, HEAD_ROLES):
        return True
    obj = target_of(link)
    if obj is None:
        return False
    if link.target_type == QrLink.EVENT:
        return _has_role(user, EVENT_ADMIN_ROLES) or org_can_event(user, "can_edit_events", obj)
    if link.target_type == QrLink.TEAM:
        return obj.team_owner_id == user.user_id
    if link.target_type == QrLink.PLAYER:
        return obj.user_id == user.user_id
    return _is_news_admin(user)
