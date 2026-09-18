from django.apps import AppConfig


class AfcWagerConfig(AppConfig):
    """Pari-mutuel wagers on AFC matches, paid in naira at placement (owner 2026-09-18, inbox
    #33). See afc_wager/models.py for the money model and why there are no coins."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_wager"
