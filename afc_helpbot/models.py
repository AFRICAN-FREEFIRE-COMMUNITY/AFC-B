"""afc_helpbot.models - the website Help panel's conversations (inbox #109, owner approved 2026-10-04).

WHY THE CONVERSATION IS STORED HERE AND NOT ONLY IN THE BROWSER
---------------------------------------------------------------
1. "Talk to a person" attaches the chat to a support ticket. If the browser sent the transcript, a
   visitor could hand the support team a conversation the assistant never had. The ticket is built
   from these rows instead.
2. The daily allowance (R75) is COUNTED from these rows: questions asked today by this account, or
   by this browser and this network when signed out. No second counter to drift out of step.
3. The assistant needs the earlier turns to answer a follow-up, and the backend is what sends them.

PRIVACY
-------
Nothing here is a raw IP address or a raw browser id: both are stored as salted hashes (see
afc_helpbot.views._hash), which is enough to count and to match a browser to its own conversation,
and useless for anything else. Every conversation is deleted HELP_BOT_RETENTION_DAYS after its last
message by afc_helpbot.tasks.purge_old_help_chats (celery beat, afc/celery_config.py). A ticket
keeps its own copy of the transcript, so the purge never empties a ticket.

CONNECTS TO: afc_helpbot/views.py (the three endpoints), afc_support.models.SupportTicket (the
ticket a handoff opens), frontend components/help/HelpBot.tsx (the panel).
"""
import secrets

from django.conf import settings
from django.db import models


def new_conversation_token() -> str:
    """h_<24 hex>: the conversation's opaque address (R22). For a signed-out visitor it is half of
    the proof that the conversation is theirs; the other half is the browser id (visitor_hash)."""
    return "h_" + secrets.token_hex(12)


class HelpConversation(models.Model):
    public_token = models.CharField(max_length=32, unique=True, db_index=True,
                                    default=new_conversation_token)
    # Set when the visitor was signed in, or signed in later in the same conversation (claimed).
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.CASCADE, related_name="help_conversations")
    # Signed out: sha256 of the random id the browser keeps, salted (views._hash).
    visitor_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # sha256 of the caller's IP, salted. Counts a network's signed-out questions per day.
    ip_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    locale = models.CharField(max_length=8, blank=True, default="en")
    # The support ticket "Talk to a person" opened, if any. One per conversation (idempotent).
    ticket = models.ForeignKey("afc_support.SupportTicket", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="help_conversations")
    # When the transcript was last copied to the ticket: a second handoff adds only what came after.
    handed_off_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-last_message_at"]

    def __str__(self):
        return f"{self.public_token} {self.user_id or 'visitor'}"


class HelpMessage(models.Model):
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_CHOICES = [(ROLE_USER, "Visitor"), (ROLE_ASSISTANT, "AFC Help")]

    conversation = models.ForeignKey(HelpConversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    body = models.TextField()
    # The answer leaned on the person's own account facts (the panel's "Checked your account" tag).
    used_account = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.conversation.public_token} {self.role} {self.created_at:%Y-%m-%d %H:%M}"


class HelpInputLog(models.Model):
    """Every input to the Help panel, answered or not (inbox #156, owner 2026-10-05: "Please log all
    inputs to tthe help centre please and from what user, date, time, waht was inpoutted etc.").

    HelpMessage only holds questions that got an answer (a failed question must not count against
    the allowance), so it cannot answer "what did people type". This table records EVERY question
    and every "Talk to a person" request, with what came back: the answer, a refusal code (the daily
    limit, busy, the bot check) or the ticket number. One row per request, written by
    afc_helpbot.views._logged after the response is decided, so it never changes what the visitor gets.

    Who: the account (and its name at the time, so the row still reads after the account is renamed
    or deleted), or for a signed-out visitor the salted browser and network hashes (never a raw IP).
    Read by staff on the admin Help log (afc_helpbot.views.help_admin_log, frontend
    app/(a)/a/support/help-log). Deleted after HELP_BOT_LOG_RETENTION_DAYS (90 by default) by
    afc_helpbot.tasks.purge_old_help_chats."""
    KIND_QUESTION = "question"
    KIND_HANDOFF = "handoff"
    KIND_CHOICES = [(KIND_QUESTION, "Question"), (KIND_HANDOFF, "Talk to a person")]
    OUTCOME_ANSWERED = "answered"

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="help_inputs")
    username = models.CharField(max_length=150, blank=True, default="")
    visitor_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    ip_hash = models.CharField(max_length=64, blank=True, default="")
    # The conversation's token is copied so the row still names it after the 30-day chat purge.
    conversation_token = models.CharField(max_length=32, blank=True, default="", db_index=True)
    text = models.TextField(blank=True, default="")
    answer = models.TextField(blank=True, default="")
    # "answered", "ticket:<number>", or the refusal code the panel was given.
    outcome = models.CharField(max_length=60, db_index=True)
    http_status = models.PositiveSmallIntegerField(default=200)
    locale = models.CharField(max_length=8, blank=True, default="")
    page = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.username or 'visitor'} {self.kind} {self.outcome}"
