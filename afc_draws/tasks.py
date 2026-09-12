"""
afc_draws/tasks.py - the draw's emails, sent off the request thread.

WHY A TASK: afc_auth.views.send_email opens one SMTP session per recipient (a second or two each).
Opening a draw for a 48-team lobby would otherwise hold the organizer's request for a minute or
more, and a reminder the same. So services._email queues ONE task with the recipient ids and the
copy; the worker (Celery, eager in tests and on the scratch rig) sends them one by one, best
effort, and logs a count rather than any address (no PII in logs).

Connects to: services._email (the only caller), afc_auth.views.send_email (the single email
chokepoint, which localizes the subject and body per recipient language).
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def send_draw_emails(user_ids, subject, lead_html, tail_text, event_url):
    from afc_auth.models import User
    from afc_auth.views import _email_shell, send_email

    sent = 0
    users = list(User.objects.filter(user_id__in=user_ids))
    for u in users:
        if not u.email:
            continue
        inner = (
            f'<tr><td style="padding:0 40px 18px;font-size:16px;line-height:1.6;color:#e8e8e8;">{lead_html}</td></tr>'
            f'<tr><td style="padding:0 40px 18px;font-size:15px;line-height:1.6;color:#c9c9c9;">{tail_text}</td></tr>'
            f'<tr><td style="padding:6px 40px 40px;"><a href="{event_url}" '
            f'style="display:inline-block;padding:14px 26px;background-color:#16a34a;color:#ffffff;'
            f'text-decoration:none;font-weight:700;border-radius:8px;">Open the event page</a></td></tr>'
        )
        try:
            if send_email(u.email, subject, _email_shell(inner), language=getattr(u, "language", None) or "en"):
                sent += 1
        except Exception:  # noqa: BLE001 - one bad address must not stop the rest
            logger.exception("draw email failed for user %s", u.user_id)
    logger.info("draw emails: %s of %s sent, subject %r", sent, len(users), subject)
    return sent
