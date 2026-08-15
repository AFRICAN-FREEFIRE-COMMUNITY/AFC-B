from django.apps import AppConfig


class AfcPollsConfig(AppConfig):
    """AFC Polls: one poll engine, with award ballots as a preset of it.

    Spec: WEBSITE/tasks/polls-spec.md. Mounted at `polls/` in afc/urls.py.
    Replaces afc_awards (Section / Category / Nominee / CategoryNominee / Vote), which stays
    in place, untouched, until the historical Vote rows have been migrated in a later phase.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_polls"
