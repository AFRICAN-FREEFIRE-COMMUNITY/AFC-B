from django.apps import AppConfig


class AfcPlayerMarketConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'afc_player_market'

    def ready(self):
        # Wire the RecruitmentPostImage post_delete -> on-disk file cleanup (afc_player_market/signals.py).
        from . import signals  # noqa: F401
