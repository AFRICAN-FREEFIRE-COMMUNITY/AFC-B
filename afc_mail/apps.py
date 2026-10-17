from django.apps import AppConfig


class AfcMailConfig(AppConfig):
    """Bulk marketing email through Amazon SES, with consent, one-click unsubscribe and a tracker
    (owner 2026-10-16, inbox #259). See afc_mail/models.py for what each table holds."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_mail"
