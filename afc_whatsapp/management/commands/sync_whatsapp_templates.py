# backend/afc_whatsapp/management/commands/sync_whatsapp_templates.py
# ──────────────────────────────────────────────────────────────────────────────
# Fill the WhatsAppTemplate registry from the live WhatsApp Business Account.
#
#   python manage.py sync_whatsapp_templates
#
# WHY IT EXISTS
#   afc_whatsapp/tasks.py is_send_allowed refuses to send a template the registry
#   says is not approved, so that a message never dies silently inside Meta with a
#   132001. This command is how the registry learns the truth, instead of an admin
#   typing template names in by hand and getting the language wrong ("en" is a
#   different template from "en_US" as far as Meta is concerned).
#
# RUN IT: after approving a new template in WhatsApp Manager, and after any deploy
# that adds a template send. It is idempotent, so running it on a schedule is fine.
#
# CONNECTS TO: afc_whatsapp/client.py list_templates (the Graph call, needs
# WHATSAPP_BUSINESS_ACCOUNT_ID) and afc_whatsapp/models.py WhatsAppTemplate.
# ──────────────────────────────────────────────────────────────────────────────
from django.core.management.base import BaseCommand
from django.utils import timezone

from afc_whatsapp import client
from afc_whatsapp.models import WhatsAppTemplate


def _body_variable_count(components):
    """How many {{n}} placeholders the approved BODY carries.

    Meta describes a template as a list of components; the BODY one holds the text.
    Counting "{{" is enough: Meta only allows positional placeholders there."""
    for component in components or []:
        if (component.get("type") or "").upper() == "BODY":
            return (component.get("text") or "").count("{{")
    return 0


class Command(BaseCommand):
    help = "Sync the approved WhatsApp message templates from Meta into WhatsAppTemplate."

    def handle(self, *args, **options):
        result = client.list_templates()
        if not result.get("ok"):
            self.stderr.write(self.style.ERROR(
                f"Could not read the templates: {result.get('error_title')} "
                f"({result.get('error_detail') or 'no detail'})"
            ))
            return

        now = timezone.now()
        created = updated = 0
        for template in result.get("templates") or []:
            name = template.get("name")
            language = template.get("language")
            if not name or not language:
                continue

            # Meta's status is APPROVED / PENDING / REJECTED / PAUSED / DISABLED.
            # Only APPROVED may be sent.
            approved = (template.get("status") or "").upper() == "APPROVED"
            _, was_created = WhatsAppTemplate.objects.update_or_create(
                name=name, language=language,
                defaults={
                    "category": (template.get("category") or "")[:30],
                    "approved": approved,
                    "variable_count": _body_variable_count(template.get("components")),
                    "synced_at": now,
                },
            )
            created += 1 if was_created else 0
            updated += 0 if was_created else 1

        self.stdout.write(self.style.SUCCESS(
            f"WhatsApp templates synced: {created} added, {updated} updated, "
            f"{WhatsAppTemplate.objects.filter(approved=True).count()} approved in total."
        ))
