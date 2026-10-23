"""
afc_helpbot.urls - the website Help panel, mounted at `help-bot/` in afc/urls.py (inbox #109).

    GET  help-bot/status/    is the assistant on, questions left today
    POST help-bot/chat/      ask a question
    POST help-bot/handoff/   "Talk to a person": a support ticket with the chat attached

Open to everyone; a Bearer SessionToken adds the person's own account. See afc_helpbot/views.py.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("status/", views.help_status, name="help_bot_status"),
    path("chat/", views.help_chat, name="help_bot_chat"),
    path("handoff/", views.help_handoff, name="help_bot_handoff"),
    path("conversations/", views.help_conversations, name="help_bot_conversations"),
    path("conversations/<str:token>/", views.help_conversation, name="help_bot_conversation"),
    path("admin/log/", views.help_admin_log, name="help_bot_admin_log"),
]
