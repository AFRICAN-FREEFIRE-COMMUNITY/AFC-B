"""afc_qr.views - QR codes for events, teams, players and news (inbox #46, owner 26 Sep 2026).

Four endpoints, mounted under qr/ in afc/urls.py. All answer the AFC envelope; every refusal carries a
`code` the frontend translates (R35).

  POST qr/link/                public, rate limited (R59)
      body  {"target_type": "event|team|player|news", "ref": "<slug | team name | username>"}
      200   {"token", "url_path": "/q/<token>", "target_type", "name"}
      400   bad_target_type | missing_ref     404 target_not_found     429 rate_limited
      Get-or-create: one link per page, so every QR ever made for a page shares one count.
      Caller: frontend components/qr/QrShareButton.tsx (lib/qr.ts getQrLink), on opening the dialog.

  GET  qr/info/<token>/        public, does NOT count
      200   {"token", "target_type", "name", "path", "picture"}      404 qr_not_found
      Caller: frontend app/qr/[token]/card/route.tsx (the card image) and the poster page.

  POST qr/scan/<token>/        public, counts
      200   {"path": "/teams/...", "counted": true|false}             404 qr_not_found
      Counts one scan unless the caller is a link preview or crawler, or the same device (salted hash
      of IP + user agent) already counted within SCAN_REPEAT_SECONDS.
      Caller: frontend app/q/[token]/route.ts, which forwards the scanner's user agent and address and
      then redirects to `path`. The path is asked for on every scan, so a renamed team still resolves.

  GET  qr/stats/<token>/       Bearer, page owner only (R58, rule in afc_qr/targets.can_see_stats)
      200   {"scan_count", "last_scanned_at"}
      401   authentication_credentials_not_provided | invalid_expired_token
      403   not_page_owner     404 qr_not_found
      Caller: frontend QrShareButton (lib/qr.ts getQrStats), shown only when the page says isOwner.
"""
import hashlib
import re

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.slugs import new_public_token
from afc_auth.views import validate_token

from . import targets
from .models import QrLink

QR_LINKS_PER_HOUR = 60     # generous: one person making QRs for a dozen pages in a sitting is normal
SCAN_REPEAT_SECONDS = 60   # the same phone scanning twice within a minute counts once
TOKEN_RE = re.compile(r"^q_[0-9a-f]{10}$")
# Link previews and crawlers fetch the short link when it is pasted into a chat. They are not people.
BOT_RE = re.compile(
    r"bot|crawl|spider|preview|whatsapp|telegram|discord|slack|facebookexternalhit|twitter|linkedin|"
    r"skype|embedly|quora|pinterest|vkshare|w3c_validator|headless|curl|wget|python-requests",
    re.I,
)
NOT_FOUND = "This QR code does not lead anywhere any more."


def _refuse(message, code, http):
    return Response({"message": message, "code": code}, status=http)


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.META.get("REMOTE_ADDR") or "")


def _hash(value):
    # Salted, so the cache never holds a raw address
    return hashlib.sha256(f"{settings.SECRET_KEY}:{value}".encode("utf-8")).hexdigest()[:32]


def _optional_user(request):
    """The signed-in user when a valid Bearer came with the request, else None (making a QR is public)."""
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ", 1)[1]) or None


def _under_hourly_limit(identity):
    key = f"qr_link_hr:{identity}:{timezone.now().strftime('%Y%m%d%H')}"
    cache.add(key, 0, 3600)
    try:
        count = cache.incr(key)
    except ValueError:  # the key expired between add and incr
        cache.set(key, 1, 3600)
        count = 1
    return count <= QR_LINKS_PER_HOUR


def _link(token):
    if not TOKEN_RE.match(token or ""):
        return None
    return QrLink.objects.filter(token=token).first()


@api_view(["POST"])
def create_link(request):
    target_type = str(request.data.get("target_type") or "")[:10]
    ref = str(request.data.get("ref") or "")[:220].strip()
    if target_type not in QrLink.TARGET_TYPES:
        return _refuse("Unknown page type.", "bad_target_type", status.HTTP_400_BAD_REQUEST)
    if not ref:
        return _refuse("Which page is this QR code for?", "missing_ref", status.HTTP_400_BAD_REQUEST)

    user = _optional_user(request)
    identity = f"u{user.user_id}" if user else f"ip{_hash(_client_ip(request))}"
    if not _under_hourly_limit(identity):
        return _refuse("Too many QR codes made in a short time. Try again later.", "rate_limited",
                       status.HTTP_429_TOO_MANY_REQUESTS)

    obj = targets.find_target(target_type, ref)
    if obj is None:
        return _refuse("That page was not found.", "target_not_found", status.HTTP_404_NOT_FOUND)
    target_id = targets.id_of(target_type, obj)

    # ── get-or-create; the unique constraint settles two people asking at the same instant ──
    link = QrLink.objects.filter(target_type=target_type, target_id=target_id).first()
    if link is None:
        try:
            with transaction.atomic():
                link = QrLink.objects.create(token=new_public_token("q"), target_type=target_type,
                                             target_id=target_id, created_by=user)
        except IntegrityError:
            link = QrLink.objects.get(target_type=target_type, target_id=target_id)

    info = targets.describe(link, request)
    if info is None:
        return _refuse("That page was not found.", "target_not_found", status.HTTP_404_NOT_FOUND)
    return Response({"token": link.token, "url_path": f"/q/{link.token}", "target_type": target_type,
                     "name": info["name"]})


@api_view(["GET"])
def link_info(request, token):
    link = _link(token)
    info = targets.describe(link, request) if link else None
    if info is None:
        return _refuse(NOT_FOUND, "qr_not_found", status.HTTP_404_NOT_FOUND)
    return Response({"token": link.token, **info})


@api_view(["POST"])
def scan(request, token):
    link = _link(token)
    info = targets.describe(link, request) if link else None
    if info is None:
        return _refuse(NOT_FOUND, "qr_not_found", status.HTTP_404_NOT_FOUND)

    agent = request.META.get("HTTP_USER_AGENT", "")
    counted = False
    if agent and not BOT_RE.search(agent):
        seen_key = f"qr_seen:{link.token}:{_hash(_client_ip(request) + '|' + agent)}"
        if cache.add(seen_key, 1, SCAN_REPEAT_SECONDS):
            # F() so two scans at the same instant both land
            QrLink.objects.filter(pk=link.pk).update(scan_count=F("scan_count") + 1,
                                                     last_scanned_at=timezone.now())
            counted = True
    return Response({"path": info["path"], "counted": counted})


@api_view(["GET"])
def link_stats(request, token):
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return _refuse("Authentication credentials were not provided.", "authentication_credentials_not_provided",
                       status.HTTP_401_UNAUTHORIZED)
    user = validate_token(auth.split(" ", 1)[1])
    if not user:
        return _refuse("Invalid or expired session token.", "invalid_expired_token", status.HTTP_401_UNAUTHORIZED)
    link = _link(token)
    if link is None:
        return _refuse(NOT_FOUND, "qr_not_found", status.HTTP_404_NOT_FOUND)
    if not targets.can_see_stats(user, link):
        return _refuse("Only the page's owner can see how often it was scanned.", "not_page_owner",
                       status.HTTP_403_FORBIDDEN)
    return Response({"scan_count": link.scan_count,
                     "last_scanned_at": link.last_scanned_at.isoformat() if link.last_scanned_at else None})
