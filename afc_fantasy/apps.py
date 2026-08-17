from django.apps import AppConfig


class AfcFantasyConfig(AppConfig):
    """AFC Fantasy League: fans pick real players from a real AFC event and score on what those
    players actually do.

    Spec: WEBSITE/tasks/fantasy-league-spec.md (written in plain English for the owner, and the
    authority on every default in here). Mounted at `fantasy/` in afc/urls.py.

    Reuses rather than reinvents: eligibility comes from afc_auth.audience (the same engine polls
    and broadcasts use), scoring reads afc_tournament_and_scrims match stats that were being
    entered anyway, and prices are derived from those same stats. Nothing here asks an organizer
    to record anything new.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "afc_fantasy"
