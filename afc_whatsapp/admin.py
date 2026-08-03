# backend/afc_whatsapp/admin.py
# Django-admin read surface for the WhatsApp log. This is how ops answers "did the
# player actually get the room details", which neither vendor integration could.
# The message log is READ ONLY on purpose: rows are written by
# afc_whatsapp/tasks.py and advanced by afc_whatsapp/webhooks.py, and a hand edit
# would make the delivery history a fiction. The template registry IS editable, so
# a template can be approved by hand when the sync command has not been run.
from django.contrib import admin

from .models import WhatsAppMessage, WhatsAppTemplate


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "direction", "phone", "template_name", "status",
                    "error_code", "context")
    list_filter = ("direction", "status", "template_name")
    search_fields = ("phone", "wamid", "template_name", "context", "body")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(WhatsAppTemplate)
class WhatsAppTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "language", "category", "approved", "variable_count", "synced_at")
    list_filter = ("approved", "category", "language")
    search_fields = ("name",)
