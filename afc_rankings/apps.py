from django.apps import AppConfig


class AfcRankingsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'afc_rankings'

    def ready(self):
        from . import signals  # noqa: F401  — registers §18 recalc triggers
