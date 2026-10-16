"""
afc_mail/transport.py - the ONE place bulk marketing mail is handed to Amazon SES.

A message transport, so it obeys the outbound switch FIRST (afc_auth.outbox, owner 2026-09-17: the
test runner and the scratch server must never mail real people). It is listed in the `except` of
tools/known_bugs.json be-transport-outside-chokepoint, which fails an smtplib call anywhere else.

WHY SES OVER SMTP (and not the API): production carries no AWS SDK, and SES's SMTP interface takes
the same headers we need: X-SES-CONFIGURATION-SET routes delivery, bounce and complaint events to
the SNS topic (afc_mail.sns), and X-SES-MESSAGE-TAGS carries the recipient token so each event finds
its CampaignRecipient row again.

SETTINGS (all from the box's .env; the owner creates the SMTP credentials in the SES console and
types them there, never in chat):
  SES_SMTP_HOST         default email-smtp.eu-north-1.amazonaws.com (the region set up 13 Oct)
  SES_SMTP_PORT         default 587 (STARTTLS)
  SES_SMTP_USER / SES_SMTP_PASSWORD
  SES_CONFIGURATION_SET the configuration set whose event destination is the SNS topic
  MAIL_FROM             default "African Free Fire Community <news@news.africanfreefirecommunity.com>":
                        the From must be ON news.africanfreefirecommunity.com, because the root
                        domain's DMARC is strict (adkim=s, aspf=s; HANDOVER-2026-10-12 s16)
  MAIL_REPLY_TO         default info@africanfreefirecommunity.com

CALLED BY: afc_mail.tasks (campaign chunks) and afc_mail.views.campaign_test_send.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from django.conf import settings

from afc_auth import outbox
from afc_auth.views import header_safe

logger = logging.getLogger(__name__)

DEFAULT_FROM = "African Free Fire Community <news@news.africanfreefirecommunity.com>"
DEFAULT_REPLY_TO = "info@africanfreefirecommunity.com"


def _setting(name, default=""):
    return (getattr(settings, name, "") or default)


def build_message(to_email, subject, html_body, text_body, *, one_click_url, page_url, tags):
    """The MIME message for one recipient, with the headers every bulk sender now needs.

    List-Unsubscribe + List-Unsubscribe-Post (RFC 8058) is what Gmail and Yahoo require of bulk
    senders since 2024 and what puts the "Unsubscribe" button in the mail app itself."""
    msg = EmailMessage()
    msg["Subject"] = header_safe(subject)
    msg["From"] = _setting("MAIL_FROM", DEFAULT_FROM)
    msg["To"] = to_email
    msg["Reply-To"] = _setting("MAIL_REPLY_TO", DEFAULT_REPLY_TO)
    msg["Message-ID"] = make_msgid(domain="news.africanfreefirecommunity.com")
    msg["List-Unsubscribe"] = f"<{one_click_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    config_set = _setting("SES_CONFIGURATION_SET")
    if config_set:
        msg["X-SES-CONFIGURATION-SET"] = config_set
    if tags:
        # SES tag values allow letters, digits, "_", "-", "." and "@"; our tokens are hex with "_".
        msg["X-SES-MESSAGE-TAGS"] = ", ".join(f"{k}={v}" for k, v in tags.items())
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


class Connection:
    """One SMTP session to SES for a batch of messages (a chunk), so 100 messages cost one TLS
    handshake rather than 100. Use as a context manager. In outbox mode nothing is opened."""

    def __init__(self):
        self.live = outbox.is_live()
        self.smtp = None

    def __enter__(self):
        if self.live:
            host = _setting("SES_SMTP_HOST", "email-smtp.eu-north-1.amazonaws.com")
            port = int(_setting("SES_SMTP_PORT", "587"))
            user = _setting("SES_SMTP_USER")
            password = _setting("SES_SMTP_PASSWORD")
            if not (user and password):
                raise RuntimeError("SES_SMTP_USER / SES_SMTP_PASSWORD are not set on this server")
            self.smtp = smtplib.SMTP(host, port, timeout=30)
            self.smtp.starttls(context=ssl.create_default_context())
            self.smtp.login(user, password)
        return self

    def send(self, msg):
        """Hand one message over. Returns True when SES (or the outbox) took it."""
        if not self.live:
            body = msg.get_body(preferencelist=("html",))
            return outbox.record("email", msg["To"], str(msg["Subject"]), body.get_content() if body else "")
        refused = self.smtp.send_message(msg)
        return not refused

    def __exit__(self, *exc):
        if self.smtp is not None:
            try:
                self.smtp.quit()
            except Exception:  # the session is over either way; a failed QUIT changes nothing sent
                logger.warning("afc_mail: SES SMTP quit failed", exc_info=True)
        return False


def send_one(to_email, subject, html_body, text_body, *, one_click_url, page_url, tags):
    """A single message in its own session (the admin's test send)."""
    msg = build_message(to_email, subject, html_body, text_body,
                        one_click_url=one_click_url, page_url=page_url, tags=tags)
    with Connection() as conn:
        return conn.send(msg)

