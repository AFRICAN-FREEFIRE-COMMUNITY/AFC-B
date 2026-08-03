# backend/afc_whatsapp/models.py
# ──────────────────────────────────────────────────────────────────────────────
# The WhatsApp message log and the approved-template registry.
#
# WHY A LOG AT ALL
#   Neither vendor integration we are replacing (Kapso, Zernio) recorded anything.
#   When a player said "I never got the room details" there was no way to answer:
#   no record of whether we sent it, what number we sent it to, whether Meta
#   accepted it, or whether it was delivered. WhatsAppMessage is that record, and
#   Meta's message id (wamid) is what ties our row to Meta's delivery receipts.
#
# HOW IT CONNECTS
#   WRITTEN BY : afc_whatsapp/tasks.py send_whatsapp_message writes the row BEFORE
#                the HTTP call (so a send that never leaves is still on record) and
#                stamps the wamid + status afterwards.
#   UPDATED BY : afc_whatsapp/webhooks.py, which matches Meta's status callbacks on
#                `wamid` and advances the row to delivered / read / failed.
#   READ BY    : ops and, later, the organizer-facing "who did we reach" surfaces.
#   FKs        : `user` is the AFC account we messaged (null for a vendor or any
#                non-account recipient). `event` / `match` say what the message was
#                about, declared as STRING references
#                ("afc_tournament_and_scrims.Event") so this app never imports the
#                tournament app and no import cycle can form.
# ──────────────────────────────────────────────────────────────────────────────
from django.conf import settings
from django.db import models
from django.utils import timezone


class WhatsAppMessage(models.Model):
    """One WhatsApp message, outbound or inbound, and everything we know about it."""

    DIRECTION_CHOICES = [
        ("outbound", "Outbound"),   # AFC to a person
        ("inbound", "Inbound"),     # a person to AFC (reply, button tap, STOP)
    ]

    # Lifecycle. queued -> sent -> delivered -> read is Meta's own progression; a
    # message can drop to failed from any point. `queued` means the row exists but
    # Meta has not accepted it yet, which is exactly the state a crashed send leaves
    # behind and the reason the row is written first.
    STATUS_CHOICES = [
        ("queued", "Queued"),
        ("sent", "Sent"),
        ("delivered", "Delivered"),
        ("read", "Read"),
        ("failed", "Failed"),
    ]

    # Rank used to stop a late-arriving callback from moving a row BACKWARDS. Meta
    # does not guarantee callback order, so a "sent" receipt can land after
    # "delivered"; without this the row would flip back and the log would lie.
    STATUS_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}

    # The AFC account this message is to or from. NULL for recipients who have no
    # account (marketplace vendors are the current case). SET_NULL, not CASCADE:
    # deleting a user must not erase the delivery history.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="whatsapp_messages",
    )

    # The number AS ACTUALLY SENT, in E.164 ("+2348051234567"), after
    # afc_whatsapp.phone.to_e164 normalised it. Storing the normalised form (not the
    # raw profile value) is what makes "we sent it to the wrong number" answerable.
    phone = models.CharField(max_length=20, db_index=True)

    direction = models.CharField(
        max_length=10, choices=DIRECTION_CHOICES, default="outbound", db_index=True
    )

    # Template identity. Blank for free-form text and for inbound messages. The
    # language is stored alongside the name because Meta treats "en" and "en_US" as
    # two different templates.
    template_name = models.CharField(max_length=120, blank=True, default="")
    template_language = models.CharField(max_length=10, blank=True, default="")

    # The variable VALUES this send used, as
    # {"body": [...], "buttons": [...]}. Kept so a message can be reconstructed
    # exactly as the recipient saw it, which a template name alone cannot do.
    variables = models.JSONField(default=dict, blank=True)

    # Free-form text body: what we sent (send_text) or what the person sent us
    # (inbound). Blank for template sends, whose content lives in `variables`.
    body = models.TextField(blank=True, default="")

    # Meta's message id, e.g. "wamid.HBgMMjM0ODA1...". THE join key: every status
    # callback identifies the message by this and nothing else. NULL while the row is
    # queued and for any send Meta rejected. unique=True gives the index as well as
    # the guarantee; MySQL permits many NULLs under a unique index, which is what
    # lets un-sent rows coexist.
    wamid = models.CharField(max_length=128, null=True, blank=True, unique=True)

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default="queued", db_index=True
    )

    # Meta's failure reason, preserved as Meta gave it. The code is the actionable
    # part (131047 = the 24 hour window closed, 132001 = template not approved,
    # 131026 = the recipient does not have WhatsApp), so it is a real column rather
    # than text buried in a log line.
    error_code = models.IntegerField(null=True, blank=True)
    error_title = models.CharField(max_length=255, blank=True, default="")

    # What the message was ABOUT. String references keep this app free of any import
    # from afc_tournament_and_scrims (which imports afc_auth, which is heavy).
    event = models.ForeignKey(
        "afc_tournament_and_scrims.Event",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="whatsapp_messages",
    )
    match = models.ForeignKey(
        "afc_tournament_and_scrims.Match",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="whatsapp_messages",
    )

    # What TRIGGERED this send, in plain words: "room_details", "vendor_new_order",
    # "checkin_reminder". Free text on purpose so a new caller needs no migration.
    context = models.CharField(max_length=120, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # "everything we sent this person" and "everything about this event",
            # the two lookups the ops surfaces need.
            models.Index(fields=["phone", "-created_at"]),
            models.Index(fields=["event", "-created_at"]),
        ]

    def __str__(self):
        label = self.template_name or (self.body[:30] if self.body else "message")
        return f"{self.direction} {label} to {self.phone} ({self.status})"

    # ── state transitions ─────────────────────────────────────────────────────
    def mark_sent(self, wamid, *, when=None):
        """Meta accepted the message. Records the wamid every later callback keys on."""
        self.wamid = wamid
        self.status = "sent"
        self.sent_at = when or timezone.now()
        self.save(update_fields=["wamid", "status", "sent_at"])

    def mark_failed(self, *, error_code=None, error_title="", when=None):
        """The send was rejected, by Meta or by us. Keeps Meta's code and title so
        the reason survives past the log file."""
        self.status = "failed"
        self.error_code = error_code
        self.error_title = (error_title or "")[:255]
        self.failed_at = when or timezone.now()
        self.save(update_fields=["status", "error_code", "error_title", "failed_at"])

    def apply_status_callback(self, status, *, when=None, error_code=None, error_title=""):
        """Advance the row from a Meta status callback (webhooks.py).

        Ignores a callback that would move the row BACKWARDS (see STATUS_RANK): Meta
        does not order its callbacks, so "sent" can arrive after "delivered" and must
        not undo it. Returns True when the row changed.

        `failed` always applies, whatever the current state, because a failure is
        terminal information the log must not swallow."""
        when = when or timezone.now()

        if status == "failed":
            self.mark_failed(error_code=error_code, error_title=error_title, when=when)
            return True

        if status not in self.STATUS_RANK:
            return False  # a status Meta added that we do not model yet
        if self.STATUS_RANK[status] <= self.STATUS_RANK.get(self.status, 0):
            return False  # out-of-order or duplicate callback: keep the furthest state

        stamp = {"sent": "sent_at", "delivered": "delivered_at", "read": "read_at"}[status]
        fields = ["status", stamp]
        self.status = status
        setattr(self, stamp, when)
        # A message that reached the handset was obviously sent, even if we never saw
        # the "sent" callback. Backfill so the timeline has no holes.
        if not self.sent_at:
            self.sent_at = when
            fields.append("sent_at")
        self.save(update_fields=fields)
        return True


class WhatsAppTemplate(models.Model):
    """The registry of message templates Meta has approved for our number.

    WHY: a template send that names an unapproved (or misspelled, or wrong-language)
    template is rejected by Meta with error 132001 AFTER we have burned a call and
    the recipient has been told nothing. This table lets tasks.py refuse locally, at
    once, with a reason a human can read.

    POPULATED BY: `python manage.py sync_whatsapp_templates`, which reads the live
    list from our WhatsApp Business Account (client.list_templates). Rows can also be
    added by hand in the Django admin.
    READ BY: afc_whatsapp/tasks.py is_send_allowed, on every template send.
    """

    name = models.CharField(max_length=120)
    language = models.CharField(max_length=10)
    # Meta's own category: MARKETING / UTILITY / AUTHENTICATION. It decides how the
    # message is priced and how tolerant Meta is of it, so it is worth keeping.
    category = models.CharField(max_length=30, blank=True, default="")
    approved = models.BooleanField(default=False)
    # How many {{n}} variables the approved BODY carries. Lets a caller check it is
    # passing the right number of body_params before Meta counts them for us.
    variable_count = models.PositiveIntegerField(default=0)
    synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One row per (name, language): the pair IS the identity of a template in
        # Meta, since the same name exists separately per approved language.
        unique_together = [("name", "language")]
        ordering = ["name", "language"]

    def __str__(self):
        state = "approved" if self.approved else "not approved"
        return f"{self.name} [{self.language}] ({state})"

    @classmethod
    def approval_state(cls, name, language):
        """(known, approved) for a template.

        `known` False means we have no row at all, which tasks.py treats differently
        from a row that exists and says not approved: an empty registry (a server
        that has never run the sync command) must not silently block every message."""
        row = cls.objects.filter(name=name, language=language).first()
        if row is None:
            return False, False
        return True, row.approved
