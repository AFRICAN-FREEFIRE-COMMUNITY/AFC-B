"""afc_helpbot.brain - asks the AFC Discord bot's process to answer a Help panel question.

WHY THE ANSWER COMES FROM THE DISCORD BOT'S PROCESS
---------------------------------------------------
The owner asked for a website bot "just like discord", with the same model. That process already
holds everything an answer needs: GPT-4o, the knowledge files (and whatever an admin uploads on the
Bot page, PDFs included), the 3-hourly scrape of the site, the live event list, the team lookup
tools, and the OpenAI -> Gemini -> Groq failover (inbox #112). Copying all of that into Django would
mean two knowledge loaders and two failover chains drifting apart, so the website asks the same
process over its loopback control API instead (afcbot/bot.py control_web_chat, route
POST /control/web-chat). Same address and shared secret as the admin Bot page (afc_bot/views.py):
BOT_CONTROL_URL and BOT_CONTROL_TOKEN in the server .env. The token never leaves this server.

WHAT THIS MODULE DOES NOT DO
----------------------------
It decides nothing about WHO may ask: identity, the daily allowance, the bot check and the account
facts are all settled in afc_helpbot/views.py before ask() is called.

ERRORS
------
Every failure becomes one of three exceptions the view turns into a coded answer the panel
translates: BrainOffline (not configured, process down, every AI provider down), BrainTimeout
(no answer in time), BrainFailed (anything else). The raw error is logged, never shown (R79).
"""
import logging

import requests
from django.conf import settings

log = logging.getLogger(__name__)

# Under gunicorn's 120 s and nginx's 120 s, with room for the rest of the request. The bot's own
# OpenAI client gives up at 60 s with one retry, then fails over; most answers take 3 to 15 s.
ASK_TIMEOUT_SECS = 75
# A short connect timeout: the process is on this machine, so it either answers at once or is down.
CONNECT_TIMEOUT_SECS = 3


class BrainError(Exception):
    """Base class. `code` is the refusal code the view sends to the panel."""
    code = "help_ai_failed"


class BrainOffline(BrainError):
    code = "help_ai_offline"


class BrainTimeout(BrainError):
    code = "help_ai_timeout"


class BrainFailed(BrainError):
    code = "help_ai_failed"


def is_configured() -> bool:
    return bool(getattr(settings, "BOT_CONTROL_URL", "") and getattr(settings, "BOT_CONTROL_TOKEN", ""))


def ask(*, messages, facts, locale, signed_in) -> dict:
    """One answer. `messages` is [{role, content}] oldest first, the last one the visitor's.

    Returns {"reply": str, "needs_person": bool, "used_account": bool, "needs_sign_in": bool}.
    Raises BrainOffline / BrainTimeout / BrainFailed.
    """
    if not is_configured():
        raise BrainOffline("BOT_CONTROL_URL / BOT_CONTROL_TOKEN are not set")
    url = settings.BOT_CONTROL_URL.rstrip("/") + "/control/web-chat"
    try:
        resp = requests.post(
            url,
            json={"locale": locale, "signed_in": bool(signed_in), "facts": facts, "messages": messages},
            headers={"Authorization": f"Bearer {settings.BOT_CONTROL_TOKEN}"},
            timeout=(CONNECT_TIMEOUT_SECS, ASK_TIMEOUT_SECS),
        )
    except requests.Timeout as exc:
        log.warning("help bot: the bot process did not answer in time (%s)", exc.__class__.__name__)
        raise BrainTimeout(str(exc)) from exc
    except requests.RequestException as exc:
        log.warning("help bot: cannot reach the bot process (%s)", exc.__class__.__name__)
        raise BrainOffline(str(exc)) from exc

    if resp.status_code == 503:
        raise BrainOffline("every AI provider is unavailable")
    if resp.status_code != 200:
        log.warning("help bot: the bot process answered %s", resp.status_code)
        raise BrainFailed(f"status {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise BrainFailed("not JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("reply"), str):
        raise BrainFailed("unexpected shape")
    return {
        "reply": data["reply"],
        "needs_person": data.get("needs_person") is True,
        "used_account": data.get("used_account") is True,
        "needs_sign_in": data.get("needs_sign_in") is True,
    }
