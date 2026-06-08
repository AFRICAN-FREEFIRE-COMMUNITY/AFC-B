from django.apps import AppConfig


class AfcRankingsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'afc_rankings'

    def ready(self):
        # Import signals on app load so the recalc triggers register (no other side effects).
        from . import signals  # noqa: F401  — registers §18 recalc triggers
