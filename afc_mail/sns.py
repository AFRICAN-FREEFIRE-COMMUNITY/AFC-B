"""
afc_mail/sns.py - SES delivery, bounce and complaint events, as Amazon SNS posts them.

HOW THE EVENTS ARRIVE (owner sets this up in the SES console, steps in GATES-bulk-email-2026-10-16.md
G11): the configuration set named in SES_CONFIGURATION_SET publishes Delivery, Bounce, Complaint and
Reject events to an SNS topic; the topic has an HTTPS subscription to
https://api.africanfreefirecommunity.com/mail/ses-events/ (afc_mail.views.ses_events).

TRUST (owner rule R72: every webhook is verified before the database is touched). SNS signs each
message with an RSA key whose certificate it names in SigningCertURL. We:
  1. accept a certificate URL only if it is https on sns.<region>.amazonaws.com and ends in .pem;
  2. rebuild the canonical string AWS signed (the documented key order per message type);
  3. verify the signature with that certificate (SignatureVersion 1 = SHA1, 2 = SHA256);
  4. accept only the topic in SES_SNS_TOPIC_ARN. No topic configured = nothing is accepted.
A subscription is confirmed only for that topic, and only by fetching a SubscribeURL that is itself
on sns.<region>.amazonaws.com.

WHAT AN EVENT DOES (afc_mail.models.CampaignRecipient, found by the "recipient" message tag that
afc_mail.transport sets): Delivery -> delivered; Bounce (Permanent) -> bounced + the address is
suppressed for good; Bounce (Transient) -> noted, nothing suppressed; Complaint -> complained + the
address suppressed + the person's consent switched off; Reject -> failed.
"""
import base64
import json
import logging
import re
from urllib.parse import urlparse

import requests
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
NOTIFICATION_KEYS = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
SUBSCRIPTION_KEYS = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]

_cert_cache = {}


class Refused(Exception):
    """The message is not provably from our SNS topic. The view answers 400 with this code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _is_sns_url(url):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and bool(SNS_HOST.match(u.hostname or ""))


def canonical_string(msg):
    keys = NOTIFICATION_KEYS if msg.get("Type") == "Notification" else SUBSCRIPTION_KEYS
    parts = []
    for k in keys:
        if k in msg and msg[k] is not None:
            parts.append(f"{k}\n{msg[k]}\n")
    return "".join(parts).encode("utf-8")


def _certificate(url, fetch):
    if url not in _cert_cache:
        _cert_cache[url] = x509.load_pem_x509_certificate(fetch(url))
    return _cert_cache[url]


def _default_fetch(url):
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.content


def verify(msg, *, fetch=None):
    """Raise Refused unless `msg` (the parsed POST body) is signed by SNS for our topic."""
    topic = getattr(settings, "SES_SNS_TOPIC_ARN", "") or ""
    if not topic:
        raise Refused("sns_topic_not_configured")
    if msg.get("TopicArn") != topic:
        raise Refused("sns_wrong_topic")
    cert_url = msg.get("SigningCertURL") or msg.get("SigningCertUrl") or ""
    if not (_is_sns_url(cert_url) and urlparse(cert_url).path.endswith(".pem")):
        raise Refused("sns_bad_certificate_url")
    version = str(msg.get("SignatureVersion") or "1")
    digest = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(version)
    if digest is None:
        raise Refused("sns_bad_signature_version")
    try:
        signature = base64.b64decode(msg.get("Signature") or "", validate=True)
        cert = _certificate(cert_url, fetch or _default_fetch)
        cert.public_key().verify(signature, canonical_string(msg), padding.PKCS1v15(), digest)
    except (InvalidSignature, ValueError, TypeError):
        raise Refused("sns_bad_signature")
    except requests.RequestException:
        raise Refused("sns_certificate_unreachable")


def confirm_subscription(msg, *, get=None):
    url = msg.get("SubscribeURL") or ""
    if not _is_sns_url(url):
        raise Refused("sns_bad_subscribe_url")
    # Looked up at call time, never bound as a default: a default argument freezes the function
    # the module saw at import, which no test patch can then replace.
    (get or requests.get)(url, timeout=10)


def apply_event(event):
    """Update the recipient row an SES event is about. Returns a short word for the log/tests."""
    from .models import CampaignRecipient
    from .recipients import set_consent, suppress

    kind = event.get("eventType") or event.get("notificationType") or ""
    mail = event.get("mail") or {}
    tags = mail.get("tags") or {}
    token = (tags.get("recipient") or [None])[0]
    row = CampaignRecipient.objects.filter(token=token).first() if token else None
    if row is None:
        return "unknown_recipient"
    now = timezone.now()
    if kind == "Delivery":
        if row.status not in ("complained", "bounced"):
            row.status = "delivered"
        row.delivered_at = now
        row.save(update_fields=["status", "delivered_at"])
        return "delivered"
    if kind == "Bounce":
        bounce = event.get("bounce") or {}
        if (bounce.get("bounceType") or "") == "Permanent":
            row.status = "bounced"
            row.bounced_at = now
            row.error = (bounce.get("bounceSubType") or "")[:255]
            row.save(update_fields=["status", "bounced_at", "error"])
            suppress(row.email, "bounce", bounce.get("bounceSubType") or "")
            return "bounced"
        row.error = f"soft bounce: {bounce.get('bounceSubType') or ''}"[:255]
        row.save(update_fields=["error"])
        return "soft_bounce"
    if kind == "Complaint":
        row.status = "complained"
        row.complained_at = now
        row.save(update_fields=["status", "complained_at"])
        suppress(row.email, "complaint", (event.get("complaint") or {}).get("complaintFeedbackType") or "")
        if row.user_id:
            set_consent(row.user, False, "complaint")
        return "complained"
    if kind == "Reject":
        row.status = "failed"
        row.error = "rejected by SES"
        row.save(update_fields=["status", "error"])
        return "rejected"
    return "ignored"


def handle(body, *, fetch=None, get=None):
    """Verify and act on one POST body from SNS. Returns a short word; raises Refused."""
    try:
        msg = json.loads(body)
    except (TypeError, ValueError):
        raise Refused("sns_bad_json")
    if not isinstance(msg, dict):
        raise Refused("sns_bad_json")
    verify(msg, fetch=fetch)
    kind = msg.get("Type")
    if kind == "SubscriptionConfirmation":
        confirm_subscription(msg, get=get)
        return "subscribed"
    if kind == "UnsubscribeConfirmation":
        logger.warning("afc_mail: SNS says the SES events subscription was removed")
        return "unsubscribed"
    if kind != "Notification":
        return "ignored"
    try:
        event = json.loads(msg.get("Message") or "{}")
    except ValueError:
        raise Refused("sns_bad_event_json")
    return apply_event(event)
