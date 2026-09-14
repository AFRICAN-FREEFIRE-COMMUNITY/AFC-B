"""
afc_support/models.py - the support desk: every message anybody sends AFC, kept.

WHY THIS APP EXISTS (owner 2026-09-14)
    The Contact Us form emailed a support inbox and stored nothing. A single shadowed variable
    replaced every message with the words "Valid email." for months, and because nothing was stored
    there is no copy of what people wrote: the owner asked "how can i see all messages sent so far"
    and the honest answer was that they are gone. So the form now writes to a DATABASE first and
    emails second, and every reply in either direction is a row.

    The owner's ask, in their words: "i want there to now be a support dashboard and role that can
    access and reply to it, plus its own audit that shows every single message and item that was
    sent. Allow uploads of things documents, pictures videos through that contat form and then also
    let them get a reply in their email saying we have received it and will get back to them ... and
    a ticket number and if the person has discord, our discord bot should also send them a message,
    the suport page should see all contact us sent also and they should be able to reply from there
    and also see replies from people there also."

THE SHAPE
    SupportTicket      one conversation. Carries the ticket number people quote, and an opaque
                       token that addresses the thread page a requester opens from their email
                       (R22: nameless things get opaque tokens, never a guessable id).
    SupportMessage     one message in that conversation, in either direction. A staff reply and a
                       visitor's first message are the same row shape, which is what lets the
                       dashboard and the public thread render the same history.
    SupportAttachment  one file on a message: document, picture or video. Stored under MEDIA_ROOT
                       and served ONLY through afc_support.views.support_attachment, which checks
                       either a staff role or the ticket's own token. Support files routinely carry
                       ID cards and payment screenshots; they must not sit on a guessable URL.

WHO READS IT
    afc_support/views.py (the public form, the requester's thread, the staff dashboard, the
    head-admin audit), afc_support/notify.py (the emails + the Discord DM), and the frontend
    surfaces app/(a)/a/support, app/(a)/a/support/audit and app/(root)/support/t/[token].
"""
import secrets

from django.db import models

from afc_auth.models import User


def new_ticket_number() -> str:
    """The number a person quotes back at us: AFC-3F2A19.

    Short enough to read down a phone, random enough that nobody can enumerate other people's
    tickets by adding one. Uppercase hex avoids the letter/number confusions (O/0, l/1) that a
    support agent would otherwise have to resolve over chat.
    """
    return "AFC-" + secrets.token_hex(3).upper()


def new_ticket_token() -> str:
    """The opaque address of the thread page, sent in the acknowledgement email: t_<20 hex>.

    This is the whole authentication for a requester who has no AFC account, so it is 80 bits and
    never derived from anything guessable (R22 for the address, and the same rule the order tokens
    follow in afc_shop).
    """
    return "t_" + secrets.token_hex(10)


class SupportTicket(models.Model):
    """One conversation with one person.

    A ticket is created by the public contact form (afc_support.views.support_contact) and by staff
    when they start one. `user` is filled when the email matches an AFC account, which is what lets
    the Discord DM and the localized email find the right person; it stays null for a stranger, and
    the ticket still works.
    """

    STATUS_OPEN = "open"
    STATUS_WAITING = "waiting"      # answered by AFC, waiting on the person
    STATUS_RESOLVED = "resolved"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_WAITING, "Waiting on the sender"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_CLOSED, "Closed"),
    ]

    SOURCE_CONTACT_FORM = "contact_form"
    SOURCE_STAFF = "staff"
    SOURCE_CHOICES = [
        (SOURCE_CONTACT_FORM, "Contact form"),
        (SOURCE_STAFF, "Opened by staff"),
    ]

    ticket_number = models.CharField(max_length=16, unique=True, db_index=True,
                                     default=new_ticket_number)
    public_token = models.CharField(max_length=32, unique=True, db_index=True,
                                    default=new_ticket_token)

    # Who wrote. Name and email are what they typed; user is the account we matched, if any.
    name = models.CharField(max_length=120)
    email = models.EmailField(max_length=254, db_index=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="support_tickets")
    # Copied at create time so a DM still works if the account later unlinks Discord.
    discord_id = models.CharField(max_length=50, blank=True, default="")

    subject = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN,
                              db_index=True)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_CONTACT_FORM)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="support_tickets_assigned")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    # When the LAST message landed, whichever way it went. The dashboard sorts on this, because a
    # queue sorted by creation buries the conversation that just got a reply.
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-last_message_at", "-created_at"]
        indexes = [models.Index(fields=["status", "-last_message_at"])]

    def __str__(self):
        return f"{self.ticket_number} {self.email}"


class SupportMessage(models.Model):
    """One message on a ticket, in either direction.

    IN  = from the person who wrote to us (the contact form, or their reply on the thread page).
    OUT = from AFC (a staff reply in the dashboard).
    The automatic acknowledgement is recorded as an OUT message too, so the audit shows exactly what
    left the building, not only what a human typed.
    """

    DIRECTION_IN = "in"
    DIRECTION_OUT = "out"
    DIRECTION_CHOICES = [(DIRECTION_IN, "From the sender"), (DIRECTION_OUT, "From AFC")]

    CHANNEL_WEB = "web"
    CHANNEL_EMAIL = "email"
    CHANNEL_AUTO = "auto"
    CHANNEL_CHOICES = [
        (CHANNEL_WEB, "Website"),
        (CHANNEL_EMAIL, "Email"),
        (CHANNEL_AUTO, "Automatic"),
    ]

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    direction = models.CharField(max_length=4, choices=DIRECTION_CHOICES)
    channel = models.CharField(max_length=8, choices=CHANNEL_CHOICES, default=CHANNEL_WEB)

    # The staff member who wrote an OUT message, or the account behind an IN message when we know
    # it. Null for a stranger writing in, which is the normal case on the contact form.
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="support_messages")
    # What to show when there is no account: the name they typed, or "AFC Support".
    author_name = models.CharField(max_length=120, blank=True, default="")

    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.ticket.ticket_number} {self.direction} {self.created_at:%Y-%m-%d %H:%M}"


class SupportAttachment(models.Model):
    """One file somebody attached: a document, a picture or a video.

    The original filename is kept beside the stored one because Django slugifies and de-duplicates
    what it writes to disk, and a support agent needs to see the name the person actually sent.
    Size and content type are recorded at upload so the dashboard can render a row without touching
    the file, and so the audit can answer "what was attached" even after a file is removed.
    """

    message = models.ForeignKey(SupportMessage, on_delete=models.CASCADE,
                                related_name="attachments")
    file = models.FileField(upload_to="support/%Y/%m/")
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.original_name} ({self.size_bytes} bytes)"
