from django.apps import AppConfig


class AfcTeamConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'afc_team'

    def ready(self):
        # Wire the roster-change -> Team.country auto-derivation signals (afc_team/signals.py).
        from . import signals  # noqa: F401
