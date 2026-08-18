"""
afc_bot.urls - the Bot page's endpoints, mounted at `bot/` in afc/urls.py.

    GET    bot/status/       health, AI provider chain, loop heartbeats
    GET    bot/config/       the editable settings, with their defaults
    POST   bot/config/       save settings (the bot applies them live)
    DELETE bot/config/?name= reset one setting to its default
    GET    bot/knowledge/    the documents the bot answers from
    POST   bot/knowledge/    upload one (multipart, field `file`, optional `scope`)
    DELETE bot/knowledge/?name=&scope=   remove one
    POST   bot/rescrape/     re-read the website into the knowledge base now
    GET    bot/approvals/    scrim/tournament announcements awaiting a mod
    POST   bot/approvals/    {message_id, action} approve or reject one

Every one is head-admin only and proxies to the bot's control API. See afc_bot/views.py.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("status/", views.bot_status, name="bot_status"),
    path("config/", views.bot_config, name="bot_config"),
    path("knowledge/", views.bot_knowledge, name="bot_knowledge"),
    path("rescrape/", views.bot_rescrape, name="bot_rescrape"),
    path("approvals/", views.bot_approvals, name="bot_approvals"),
]
