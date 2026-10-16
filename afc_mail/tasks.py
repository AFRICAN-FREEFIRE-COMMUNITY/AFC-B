"""
afc_mail/tasks.py - a campaign going out, in small paced chunks.

WHY CHUNKS: 30,000 emails at SES's starting rate take most of an hour. One long task would hold a
worker of the shared default queue (celery-worker.service also runs WhatsApp and SSO webhooks) for
all of it. Instead each task sends CHUNK_SIZE messages over ONE SMTP session, paced to
SES_MAX_SEND_RATE per second (default 1, the SES sandbox rate; raise it in .env to the rate SES
grants with production access), then queues the next chunk. A cancelled campaign stops at the next
chunk. No new queue, so no server unit has to change.

PER RECIPIENT, AT SEND TIME: still opted in, not suppressed, then rendered in their language with
every link tracked (afc_mail.content.render_for_recipient) and handed to SES
(afc_mail.transport.Connection). A failure marks that one row "failed" and the chunk carries on.

CONNECTS TO: afc_mail.views.campaign_send (queues the first chunk), afc_mail.models,
celery_config.py (autodiscovered).
"""
import logging
import time

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .content import one_click_unsubscribe, render_for_recipient, unsubscribe_page
from .models import Campaign, CampaignRecipient, EmailSubscription
from .recipients import is_suppressed
from .transport import Connection, build_message

logger = logging.getLogger(__name__)

CHUNK_SIZE = 100


def send_rate():
    try:
        return max(0.1, float(getattr(settings, "SES_MAX_SEND_RATE", "") or 1))
    except (TypeError, ValueError):
        return 1.0


def send_chunk(campaign_id, *, sleep=time.sleep):
    """Send up to CHUNK_SIZE queued recipients. Returns how many rows it handled; 0 means done.
    Plain function so tests can run it without a broker; the Celery task wraps it."""
    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if campaign is None or campaign.status not in ("queued", "sending"):
        return 0
    if campaign.status == "queued":
        Campaign.objects.filter(pk=campaign.pk, status="queued").update(status="sending")

    batch = list(CampaignRecipient.objects.filter(campaign=campaign, status="queued").order_by("pk")[:CHUNK_SIZE])
    if not batch:
        Campaign.objects.filter(pk=campaign.pk, status="sending").update(status="sent", finished_at=timezone.now())
        return 0

    gap = 1.0 / send_rate()
    opted_in = set(
        EmailSubscription.objects.filter(user_id__in=[r.user_id for r in batch if r.user_id], marketing_opt_in=True)
        .values_list("user_id", flat=True)
    )
    with Connection() as conn:
        for row in batch:
            if row.user_id not in opted_in or is_suppressed(row.email):
                row.status = "skipped"
                row.save(update_fields=["status"])
                continue
            try:
                subject, html_body, text_body = render_for_recipient(campaign, row)
                msg = build_message(
                    row.email, subject, html_body, text_body,
                    one_click_url=one_click_unsubscribe(row.token),
                    page_url=unsubscribe_page(row.token),
                    tags={"campaign": campaign.token, "recipient": row.token},
                )
                ok = conn.send(msg)
                row.status = "sent" if ok else "failed"
                row.sent_at = timezone.now() if ok else None
                row.error = "" if ok else "refused by the mail server"
            except Exception as exc:  # one bad message never stops the chunk; the row says why
                logger.warning("afc_mail: send failed for recipient %s", row.pk, exc_info=True)
                row.status = "failed"
                row.error = str(exc)[:255]
            row.save(update_fields=["status", "sent_at", "error"])
            sleep(gap)
    return len(batch)


@shared_task(name="afc_mail.send_campaign_chunk")
def send_campaign_chunk(campaign_id):
    handled = send_chunk(campaign_id)
    if handled:
        send_campaign_chunk.apply_async(args=[campaign_id], countdown=1)
    return handled


def cancel(campaign):
    """Stop a campaign: rows not yet sent are marked skipped, so the tracker adds up."""
    Campaign.objects.filter(pk=campaign.pk).update(status="cancelled", finished_at=timezone.now())
    CampaignRecipient.objects.filter(campaign=campaign, status="queued").update(status="skipped", error="campaign cancelled")

