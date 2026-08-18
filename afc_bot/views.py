"""
afc_bot.views - the AFC admin's Bot page, backed by the bot's own control API.

WHAT THIS IS (backlog item 31, owner 2026-08-18)
    A PROXY, and almost nothing else. The Discord bot runs as its own process, in its own repo
    (AFC/AFCBot), and exposes a small authenticated control API. These views stand between the
    admin dashboard and that API.

WHY A PROXY RATHER THAN LETTING THE BROWSER CALL THE BOT
    Three reasons, in order of how much they matter:

    1. THE TOKEN. The bot's control token would otherwise have to reach the browser, where it is
       readable by anyone with devtools and by every extension the admin has installed. Here it
       stays on the server and never crosses the wire to a client.
    2. ONE GATE. Who may manage the bot is decided by the same SessionToken and role check as the
       rest of the admin (afc_bot.permissions.can_manage_bot, head admins only), instead of a
       second, weaker answer living in a second process.
    3. THE BOT STAYS PRIVATE. BOT_CONTROL_HOST defaults to 127.0.0.1. The control port never has to
       be exposed to the internet at all, because the only thing that needs to reach it is this
       Django process.

WHEN THE BOT IS DOWN
    Every view answers 503 with a sentence saying so, rather than a stack trace or a hang. The page
    is the thing an admin opens BECAUSE the bot looks wrong, so "cannot reach the bot" is a first-
    class answer here, not an error.

CONFIGURED BY (afc/settings.py)
    BOT_CONTROL_URL    where the bot's control API listens, e.g. http://127.0.0.1:8099
    BOT_CONTROL_TOKEN  the shared secret, the SAME value the bot has

CONSUMED BY: frontend app/(a)/a/bot/page.tsx via lib/botAdmin.ts.
"""
import requests
from django.conf import settings

from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from afc_auth.views import validate_token

from .permissions import can_manage_bot

# The bot is on the same host over loopback, so it either answers quickly or it is not there. A
# long timeout would just hold an admin's page open while they wait to be told the same thing.
TIMEOUT_SECS = 10


def _gate(request):
    """(user, error_response). One place decides both identity and permission."""
    header = request.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        return None, Response({"message": "You need to be signed in to do this."},
                              status=status.HTTP_401_UNAUTHORIZED)
    user = validate_token(header.split(" ", 1)[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."},
                              status=status.HTTP_401_UNAUTHORIZED)
    if not can_manage_bot(user):
        return None, Response({"message": "Only AFC head admins can manage the bot."},
                              status=status.HTTP_403_FORBIDDEN)
    return user, None


def _bot_url(path):
    base = (getattr(settings, "BOT_CONTROL_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else ""


def _forward(method, path, **kwargs):
    """Call the bot's control API and hand its answer back nearly untouched.

    The bot's own error messages are written for a human and are more specific than anything this
    layer could invent ("NEWS_POLL_INTERVAL_SECS must be between 30 and 86400"), so they are passed
    through rather than replaced with a generic failure.
    """
    url = _bot_url(path)
    token = getattr(settings, "BOT_CONTROL_TOKEN", "")
    if not url or not token:
        return Response(
            {"message": "The bot control API is not configured on this server. Set "
                        "BOT_CONTROL_URL and BOT_CONTROL_TOKEN."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        response = requests.request(
            method, url, timeout=TIMEOUT_SECS,
            headers={"Authorization": f"Bearer {token}"}, **kwargs)
    except requests.RequestException as e:
        return Response(
            {"message": "Could not reach the bot. It may be offline or restarting.",
             "detail": str(e)[:200]},
            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:300] or "The bot returned an unreadable response."}
    return Response(body, status=response.status_code)


@api_view(["GET"])
def bot_status(request):
    """GET bot/status/ - is it up, which AI provider is serving, are the loops running.

    The one screen that answers "why is the bot quiet?" without opening a server console.
    """
    _user, err = _gate(request)
    if err:
        return err
    return _forward("GET", "/control/status")


@api_view(["GET", "POST", "DELETE"])
def bot_config(request):
    """GET / POST / DELETE bot/config/ - the settings the bot applies live.

    POST body: ``{"values": {"NEWS_POLL_INTERVAL_SECS": 120, ...}}``
    DELETE ?name=SETTING resets one back to the value declared in bot.py.

    The bot validates every value and applies the whole set or none of it, so a bad entry here
    cannot leave announcements routed half to the old channel and half to the new one.
    """
    _user, err = _gate(request)
    if err:
        return err
    if request.method == "GET":
        return _forward("GET", "/control/config")
    if request.method == "DELETE":
        return _forward("DELETE", f"/control/config?name={request.GET.get('name', '')}")
    return _forward("POST", "/control/config", json=request.data)


@api_view(["GET", "POST", "DELETE"])
@parser_classes([MultiPartParser, FormParser])
def bot_knowledge(request):
    """GET / POST / DELETE bot/knowledge/ - the documents the bot answers from.

    Until now this was CLI-only (`python upload_docs.py`), which meant knowledge could only be
    changed by somebody with shell access to the bot host.

    NOTE what is NOT here: knowledge_base.txt. A GitHub Action rewrites it from the website every
    3 hours, so anything typed into it is gone by teatime. Curated content belongs in the folders
    this endpoint manages, which the bot reads live on every reply.
    """
    _user, err = _gate(request)
    if err:
        return err
    if request.method == "GET":
        return _forward("GET", "/control/knowledge")
    if request.method == "DELETE":
        return _forward(
            "DELETE",
            f"/control/knowledge?name={request.GET.get('name', '')}"
            f"&scope={request.GET.get('scope', 'public')}")

    upload = request.FILES.get("file")
    if not upload:
        return Response({"message": "Attach a file."}, status=status.HTTP_400_BAD_REQUEST)
    scope = request.data.get("scope", "public")
    return _forward(
        "POST", f"/control/knowledge?scope={scope}",
        files={"file": (upload.name, upload.read(), upload.content_type or "application/octet-stream")})


@api_view(["POST"])
def bot_rescrape(request):
    """POST bot/rescrape/ - pull the website into the knowledge base now.

    The scheduled scrape runs every 3 hours; this is for the moment right after somebody publishes
    a rules change and wants the bot answering with it immediately.
    """
    _user, err = _gate(request)
    if err:
        return err
    return _forward("POST", "/control/rescrape")


@api_view(["GET", "POST"])
def bot_approvals(request):
    """GET / POST bot/approvals/ - the scrim and tournament announcements waiting on a mod.

    POST body: ``{"message_id": "...", "action": "approve"|"reject"}``

    The same gate item 30 put in Discord, reachable from the admin. Approving here calls the same
    announce_event() the Discord button calls, so the two routes cannot drift, and the bot edits
    the Discord preview afterwards so nobody can approve something twice from the other side.
    """
    _user, err = _gate(request)
    if err:
        return err
    if request.method == "GET":
        return _forward("GET", "/control/approvals")
    return _forward("POST", "/control/approvals", json=request.data)
