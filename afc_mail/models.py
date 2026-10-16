"""
afc_mail/models.py - bulk marketing email: who said yes, who must never be mailed again, what was
sent to whom, and what happened to each message.

WHY (owner 2026-10-13 and 2026-10-16, inbox #249 #251 #253 #259). AFC mails transactional messages
through Microsoft 365 (afc_auth.views.send_email), which caps at 30 a minute and 1,000 a day and is
not meant for bulk commercial mail. The owner set up Amazon SES (eu-north-1, identity
news.africanfreefirecommunity.com, 13 Oct) and asked for bulk email built into AFC, "plus a proper
tracker too". Marketing mail is a different thing from a broadcast to a tournament's players:
it needs the person's CONSENT, a one-click way out, and a record of what happened.

THE TABLES
  EmailSubscription  one row per user who has ever answered: consent is OFF until they turn it on
                     (Nigeria's data protection law treats marketing as needing a lawful basis).
  EmailSuppression   an address SES told us bounced hard or complained: never mailed again, by
                     anyone, whatever their consent row says.
  Campaign           one marketing email: subject and body in en (fr and pt optional), the audience
                     filters (afc_auth.audience spec), and its lifecycle.
  CampaignRecipient  one row per person a campaign was addressed to, frozen at send time, carrying
                     the opaque token used by the tracking pixel, the click redirect and the
                     unsubscribe link, and every status SES or the reader produced.
  CampaignClick      every click, so the tracker can say which link worked.

Addresses use opaque tokens, never ids (owner rule R22: nameless things get opaque tokens).
CONNECTS TO: afc_mail/views.py (every endpoint), afc_mail/tasks.py (sending), afc_mail/sns.py (SES
events), afc_mail/content.py (the email body), AFC_Frontend (consent toggle, unsubscribe page, admin
composer and tracker: mockup mockups/bulk-email/, built after the owner's approval).
"""
import secrets

from django.conf import settings
from django.db import models


def _token(prefix):
    return f"{prefix}_{secrets.token_hex(16)}"


def new_campaign_token():
    return _token("c")


def new_recipient_token():
    return _token("r")


class EmailSubscription(models.Model):
    """A user's answer to "may AFC send you news by email". Missing row = never asked = NO."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_subscription")
    marketing_opt_in = models.BooleanField(default=False)
    # When they said yes, and where (settings toggle, the one-off prompt, an email link). Kept even
    # after an opt-out, so "when did this person agree" has an answer.
    opted_in_at = models.DateTimeField(null=True, blank=True)
    opted_out_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=40, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user_id} {'in' if self.marketing_opt_in else 'out'}"


class EmailSuppression(models.Model):
    """An address that must never receive marketing mail again (hard bounce, complaint, or an
    unsubscribe from an address no longer tied to an account)."""

    REASONS = [("bounce", "Hard bounce"), ("complaint", "Complaint"), ("unsubscribe", "Unsubscribed")]
    # Lower-cased at write (see afc_mail.recipients.suppress). 191 characters keeps the unique index
    # under the utf8mb4 key limit on the production row format (afc_auth/models.py explains).
    email = models.CharField(max_length=191, unique=True)
    reason = models.CharField(max_length=20, choices=REASONS)
    detail = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.email} ({self.reason})"


class Campaign(models.Model):
    STATUS = [
        ("draft", "Draft"),          # being written; editable
        ("queued", "Queued"),        # recipients frozen, sending not started yet
        ("sending", "Sending"),      # chunks going out
        ("sent", "Sent"),            # every recipient attempted
        ("cancelled", "Cancelled"),  # stopped by an admin; the rest is never sent
    ]
    token = models.CharField(max_length=40, unique=True, default=new_campaign_token)
    name = models.CharField(max_length=120)  # internal label, never shown to readers
    # The email itself. English is required; French and Portuguese are optional, and a reader whose
    # language has no version gets the English one (no machine translation of marketing copy).
    subject_en = models.CharField(max_length=150)
    body_en = models.TextField()
    subject_fr = models.CharField(max_length=150, blank=True, default="")
    body_fr = models.TextField(blank=True, default="")
    subject_pt = models.CharField(max_length=150, blank=True, default="")
    body_pt = models.TextField(blank=True, default="")
    # The afc_auth.audience spec, as parse_audience_spec returned it (see afc_mail.recipients).
    audience = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=STATUS, default="draft")
    # Every http(s) link found in the bodies at send time, in order. The click redirect goes ONLY
    # to an entry of this list (no open redirect, owner rule R85).
    links = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="mail_campaigns")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.status})"


class CampaignRecipient(models.Model):
    STATUS = [
        ("queued", "Queued"),
        ("sent", "Sent"),              # SES accepted it
        ("delivered", "Delivered"),    # SES says the receiving server accepted it
        ("bounced", "Bounced"),
        ("complained", "Complained"),
        ("failed", "Failed"),          # our side could not hand it to SES
        ("skipped", "Skipped"),        # suppressed or opted out between freezing and sending
    ]
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="mail_receipts")
    # 180, not 191: the (campaign, email) unique index adds the 8-byte campaign key, and 180 x 4
    # bytes + 8 stays under the 767-byte utf8mb4 limit of the smallest production row format.
    email = models.CharField(max_length=180)
    language = models.CharField(max_length=8, default="en")
    token = models.CharField(max_length=40, unique=True, default=new_recipient_token)
    status = models.CharField(max_length=12, choices=STATUS, default="queued")
    error = models.CharField(max_length=255, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    bounced_at = models.DateTimeField(null=True, blank=True)
    complained_at = models.DateTimeField(null=True, blank=True)
    # Opens and clicks: the first time and a count. An open is a pixel load, which mail apps that
    # prefetch images (Apple Mail Privacy Protection) report for everyone, so opens are a soft
    # number; clicks are the honest one. The tracker says so.
    first_opened_at = models.DateTimeField(null=True, blank=True)
    open_count = models.PositiveIntegerField(default=0)
    first_clicked_at = models.DateTimeField(null=True, blank=True)
    click_count = models.PositiveIntegerField(default=0)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["campaign", "status"])]
        constraints = [models.UniqueConstraint(fields=["campaign", "email"], name="uniq_campaign_email")]

    def __str__(self):
        return f"{self.campaign_id} {self.email} {self.status}"


class CampaignClick(models.Model):
    recipient = models.ForeignKey(CampaignRecipient, on_delete=models.CASCADE, related_name="clicks")
    link_index = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
