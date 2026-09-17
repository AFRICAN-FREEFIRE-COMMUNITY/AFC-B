"""
afc_auth/outbox.py - where an outbound message goes when this process must not send for real.

WHY THIS EXISTS (owner, 2026-09-17, with a screenshot of their Outlook inbox). Seven bounces titled
"Your AFC sign-in code" to old@gmail.com landed in the owner's mailbox in one evening. No user and
no attacker: `afc_auth/tests_admin_identity.py` creates a fixture user with that address, and the
test suite run on the VPS rig, which carries the production .env, mailed it for real. Django's test
runner swaps its OWN mail backend for an in-memory one, but `send_email` (afc_auth/views.py) talks
to Office365 over smtplib directly, the WhatsApp client posts to Meta over requests, and the Discord
DM sender posts to Discord over requests, so that swap protected nothing. The two run times matched
the two bounce clusters exactly.

THE RULE. `settings.OUTBOUND_DELIVERY` is "live" or "outbox". Every chokepoint that hands a message
to a person (email, WhatsApp, Discord DM) asks `is_live()` first; when it is False the message is
RECORDED here and reported as sent. The test runner sets "outbox" by itself (afc/settings.py reads
`manage.py test` off argv), the scratch server sets it in afc/settings_scratch.py, and production
never sets it, so its default is "live". A test that wants to see what would have been sent reads
`SENT` (or `drain()`), the same way Django tests read `mail.outbox`.

`OUTBOUND_OUTBOX_FILE`, when set, also appends one line per message, so a walk on the scratch
server can read a sign-in code off the file instead of an inbox that does not exist.

WHO CALLS THIS: afc_auth.views.send_email, afc_whatsapp.client._post, afc_support.notify.send_discord_dm.
A NEW transport (SMS, push) goes behind the same switch; tools/known_bugs.json
(be-transport-outside-chokepoint) fails a smtplib / Graph / Discord call anywhere else.
"""
import io
import logging
import threading

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Every message recorded while OUTBOUND_DELIVERY is "outbox", oldest first. A dict per message:
# {"channel", "to", "subject", "body", "at"}. Tests read it; nothing in production writes it.
SENT = []
_lock = threading.Lock()


def is_live():
    """True when this process may hand messages to real people. Anything but "live" is not."""
    return getattr(settings, "OUTBOUND_DELIVERY", "live") == "live"


def record(channel, to, subject, body):
    """Record a message that was NOT sent. Returns True so a caller can use it as its own result."""
    entry = {
        "channel": channel,
        "to": to,
        "subject": subject,
        "body": body,
        "at": timezone.now(),
    }
    with _lock:
        SENT.append(entry)
    path = getattr(settings, "OUTBOUND_OUTBOX_FILE", "") or ""
    if path:
        try:
            with io.open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{entry['at'].isoformat()} {channel} to={to!r} subject={subject!r}\n{body}\n---\n")
        except OSError as exc:
            logger.warning("outbox file %s not writable: %s", path, exc)
    logger.info("outbound %s to %s held in the outbox (OUTBOUND_DELIVERY is not live)", channel, to)
    return True


def drain():
    """Return every recorded message and forget them: what a test reads between two actions."""
    with _lock:
        out = list(SENT)
        SENT.clear()
    return out
