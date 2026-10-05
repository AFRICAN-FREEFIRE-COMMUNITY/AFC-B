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
