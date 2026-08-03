# backend/afc_whatsapp/urls.py
# Mounted at /whatsapp/ by afc/urls.py, so the one public route is
#   /whatsapp/webhook/   (prod: https://api.africanfreefirecommunity.com/whatsapp/webhook/)
# That URL is what the owner registers as the callback URL on the WhatsApp product
# in the Meta app dashboard. It is deliberately NOT under /shop/, unlike the Kapso
# webhook it replaces (afc_shop/whatsapp_webhook.py at /shop/whatsapp/webhook/):
# this one serves the whole site, not just the marketplace, and keeping the two
# paths distinct means both can run side by side during the cutover.
from django.urls import path

from .webhooks import whatsapp_webhook

urlpatterns = [
    # Named afc_whatsapp_webhook, not whatsapp_webhook: afc_shop/urls.py already owns
    # that name for the Kapso endpoint, and neither URLconf is namespaced, so reusing
    # it would make reverse() resolve to whichever loaded last.
    path("webhook/", whatsapp_webhook, name="afc_whatsapp_webhook"),
]
