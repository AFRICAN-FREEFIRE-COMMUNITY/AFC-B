# ── WhatsApp via ZERNIO (owner 2026-07-02: "use Zernio in the meantime") ────────
# Zernio wraps Meta's OFFICIAL WhatsApp Cloud API behind one key (no Meta developer-app dance,
# no ban risk); templates + numbers are managed in their dashboard. This module is the single
# WhatsApp chokepoint: room details (and later reminders) go out here.
#
# CONFIG (environment via backend/.env, never git):
#   ZERNIO_API_KEY        - dashboard API key. ABSENT = the integration is INERT (no-ops).
#   ZERNIO_ACCOUNT_ID     - the WhatsApp-connected social account id (GET /v1/accounts).
#   ZERNIO_PROFILE_ID     - the Zernio profile id owning that account (auto-discovered from
#                           /v1/accounts when unset; cached for the process lifetime).
#   ZERNIO_API_BASE       - default https://zernio.com/api.
#   ZERNIO_ROOM_TEMPLATE  - approved template name (default "room_details").
#                           Variables: 1=player name, 2=event, 3=room id, 4=password, 5=map.
#   ZERNIO_TEMPLATE_LANG  - template language code (default "en").
#
# SEND FLOW (their OpenAPI: business-initiated template sends ride BROADCASTS, addressed by
# contact TAGS - there is no direct send-to-phone endpoint):
#   1. POST /v1/contacts/bulk      - upsert each recipient {name, platformIdentifier=phone}
#                                    carrying a one-off tag for this send.
#   2. POST /v1/broadcasts         - create a whatsapp broadcast: the template + variableMapping
#                                    (player name = contact field, the room facts = static custom
#                                    values, identical for every recipient of the map) targeted at
#                                    segmentFilters.tags=[that tag].
#   3. POST /v1/broadcasts/{id}/send
#
# Recipients: ONLY users with whatsapp_opt_in=True and a whatsapp_number on their UserProfile
# (consent = Meta policy; the profile edit page collects both). Best-effort by design: a WhatsApp
# hiccup must never block the site notification path (broadcast_match_room_details).

import logging
import os

import requests

log = logging.getLogger(__name__)

_PROFILE_CACHE = {"id": None}


def _cfg():
    return {
        "key": os.environ.get("ZERNIO_API_KEY", "").strip(),
        "account": os.environ.get("ZERNIO_ACCOUNT_ID", "").strip(),
        "profile": os.environ.get("ZERNIO_PROFILE_ID", "").strip(),
        "base": os.environ.get("ZERNIO_API_BASE", "https://zernio.com/api").rstrip("/"),
        "template": os.environ.get("ZERNIO_ROOM_TEMPLATE", "room_details").strip(),
        "lang": os.environ.get("ZERNIO_TEMPLATE_LANG", "en").strip(),
    }


def is_configured():
    c = _cfg()
    return bool(c["key"] and c["account"])


def _headers(c):
    return {"Authorization": f"Bearer {c['key']}", "Content-Type": "application/json"}


def _profile_id(c):
    """The profile owning the WhatsApp account: env override, else discovered once from
    /v1/accounts and cached for the process lifetime."""
    if c["profile"]:
        return c["profile"]
    if _PROFILE_CACHE["id"]:
        return _PROFILE_CACHE["id"]
    r = requests.get(f"{c['base']}/v1/accounts", headers=_headers(c), timeout=15)
    r.raise_for_status()
    for acc in r.json().get("accounts", []):
        if acc.get("_id") == c["account"]:
            prof = acc.get("profileId")
            pid = prof.get("_id") if isinstance(prof, dict) else prof
            _PROFILE_CACHE["id"] = pid
            return pid
    return None


def send_room_details(users, event, match):
    """Send ONE map's room details to every opted-in recipient's WhatsApp via a tagged broadcast.
    Returns how many recipients were included (0 when unconfigured / nobody opted in). Never
    raises - callers treat WhatsApp as a bonus channel."""
    try:
        c = _cfg()
        if not (c["key"] and c["account"]):
            return 0

        # Gather consenting recipients (number + opt-in live on UserProfile).
        from afc_auth.models import profile_of
        contacts = []
        for u in users:
            prof = profile_of(u)
            phone = (getattr(prof, "whatsapp_number", "") or "").strip() if prof else ""
            if not phone or not getattr(prof, "whatsapp_opt_in", False):
                continue
            contacts.append({
                "name": getattr(u, "full_name", "") or u.username,
                "platformIdentifier": phone,
            })
        if not contacts:
            return 0

        profile_id = _profile_id(c)
        if not profile_id:
            log.warning("zernio: could not resolve profile id for account %s", c["account"])
            return 0

        # One-off tag scopes THIS send: the broadcast below targets exactly these contacts.
        tag = f"room-ev{event.event_id}-m{getattr(match, 'match_id', 0)}"
        for ct in contacts:
            ct["tags"] = [tag]

        r = requests.post(
            f"{c['base']}/v1/contacts/bulk", headers=_headers(c), timeout=20,
            json={"profileId": profile_id, "accountId": c["account"],
                  "platform": "whatsapp", "contacts": contacts},
        )
        if r.status_code >= 300:
            log.warning("zernio contacts/bulk failed %s: %s", r.status_code, r.text[:300])
            return 0

        # Broadcast: player name resolves per contact; the room facts are the same for everyone.
        def _static(v):
            return {"field": "custom", "customValue": str(v or "-")}
        r = requests.post(
            f"{c['base']}/v1/broadcasts", headers=_headers(c), timeout=20,
            json={
                "profileId": profile_id,
                "accountId": c["account"],
                "platform": "whatsapp",
                "name": f"Room details: {event.event_name} / match {getattr(match, 'match_id', '?')}",
                "template": {
                    "name": c["template"],
                    "language": c["lang"],
                    "variableMapping": {
                        "1": {"field": "name"},
                        "2": _static(event.event_name),
                        "3": _static(match.room_id),
                        "4": _static(match.room_password),
                        "5": _static(getattr(match, "match_map", "")),
                    },
                },
                "segmentFilters": {"tags": [tag]},
            },
        )
        if r.status_code >= 300:
            log.warning("zernio broadcast create failed %s: %s", r.status_code, r.text[:300])
            return 0
        bid = (r.json().get("broadcast") or {}).get("id") or r.json().get("id")
        if not bid:
            log.warning("zernio broadcast create returned no id: %s", r.text[:300])
            return 0

        r = requests.post(f"{c['base']}/v1/broadcasts/{bid}/send", headers=_headers(c), timeout=20)
        if r.status_code >= 300:
            log.warning("zernio broadcast send failed %s: %s", r.status_code, r.text[:300])
            return 0
        return len(contacts)
    except Exception:
        log.exception("zernio room-details send crashed (ignored)")
        return 0
