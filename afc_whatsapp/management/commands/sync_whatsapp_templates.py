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
        # Collected so the command can PRINT what it found, not just how many. The exact language
        # code is the thing an owner cannot read reliably anywhere else: WhatsApp Manager shows a
        # friendly name ("English (US)"), while a send needs the literal code ("en_US"), and the
        # two are different templates to Meta. The variable count is here for the same reason: a
        # template approved with five variables and a sender passing six fails at send time, and
        # this is the only place the two are visible side by side.
        rows = []
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
            rows.append((
                name,
                language,
                (template.get("status") or "").upper(),
                _body_variable_count(template.get("components")),
            ))

        if rows:
            self.stdout.write("")
            self.stdout.write(f"{'NAME':<34} {'LANGUAGE':<10} {'STATUS':<10} VARIABLES")
            self.stdout.write("-" * 70)
            for name, language, status, variables in sorted(rows):
                line = f"{name:<34} {language:<10} {status:<10} {variables}"
                # Only APPROVED can be sent, so anything else is called out rather than listed
                # quietly among rows that look identical at a glance.
                self.stdout.write(
                    self.style.SUCCESS(line) if status == "APPROVED"
                    else self.style.WARNING(line))
            self.stdout.write("")
            self.stdout.write(
                "Put the LANGUAGE column into the settings, never the friendly name shown in "
                "WhatsApp Manager: WHATSAPP_ROOM_TEMPLATE_LANG and its siblings are compared "
                "literally by Meta."
            )
            self.stdout.write("")

        self.stdout.write(self.style.SUCCESS(
            f"WhatsApp templates synced: {created} added, {updated} updated, "
            f"{WhatsAppTemplate.objects.filter(approved=True).count()} approved in total."
        ))
