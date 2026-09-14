from django.apps import AppConfig


class AfcSupportConfig(AppConfig):
    """The support desk (owner 2026-09-14). See afc_support/models.py for why it exists."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_support"
