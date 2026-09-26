from django.apps import AppConfig


class AfcReferralsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_referrals"

    def ready(self):
        # The counting hooks: account verified, team joined, event registered, order paid.
        from . import signals  # noqa: F401
