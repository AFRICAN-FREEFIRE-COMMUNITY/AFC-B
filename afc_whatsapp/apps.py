# backend/afc_whatsapp/apps.py
# ──────────────────────────────────────────────────────────────────────────────
# afc_whatsapp: AFC talking to Meta's WhatsApp Cloud API DIRECTLY.
#
# WHY THIS APP EXISTS
#   AFC currently sends WhatsApp through two third-party middlemen:
#     - Kapso  (afc_shop/services/kapso.py)             -> marketplace vendor alerts
#     - Zernio (afc_tournament_and_scrims/whatsapp_zernio.py) -> match room details
#   Both wrap the SAME Meta Cloud API, both cost money, neither gives us the message
#   id back in a form we can track, and neither records what was sent. This app is
#   the replacement: one client, one message log, one webhook, owned by AFC.
#
# WHAT LIVES WHERE
#   client.py    the HTTP layer. Talks to graph.facebook.com. Returns a result dict
#                carrying Meta's message id (wamid) and, on failure, Meta's error
#                code + title. Never raises past the caller.
#   phone.py     to_e164(). Turns the messy numbers we actually have stored
#                ("08051234567") into what Meta requires ("+2348051234567").
#   models.py    WhatsAppMessage (one row per message, keyed on wamid) and
#                WhatsAppTemplate (the approved-template registry).
#   tasks.py     the Celery send task (queue "whatsapp") plus queue_template /
#                queue_text, the two functions the rest of the codebase calls.
#   webhooks.py  the single public endpoint: Meta's GET handshake + the POST that
#                carries delivery receipts and inbound player messages.
#
# WHO CONSUMES IT (once the cutover task repoints the call sites)
#   afc_shop/fulfilment.py notify_vendor           -> queue_template(...)
#   afc_tournament_and_scrims room-details sends   -> queue_template(...)
#   Meta (Facebook) itself                         -> POST /whatsapp/webhook/
#
# NOTE: this app does NOT touch the Kapso or Zernio modules. They keep running
# until a separate cutover step repoints their callers here.
# ──────────────────────────────────────────────────────────────────────────────
from django.apps import AppConfig


class AfcWhatsappConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "afc_whatsapp"
